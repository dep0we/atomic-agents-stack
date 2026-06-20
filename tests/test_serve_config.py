"""Tests for atomic_agents.serve._config — ServeConfig and serve.md parser.

These tests run without the serve extra (no starlette required).
spec/37 §"Config file — serve.md" and §"serve.md parser".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.serve._config import (
    _parse_serve_md,
    is_loopback,
    load_serve_config,
)


# ── _parse_serve_md ──────────────────────────────────────────────────────────


def test_parse_serve_md_defaults_on_empty():
    """Empty text returns all defaults."""
    cfg = _parse_serve_md("")
    assert cfg.identity_header == "X-Goog-IAP-JWT-Assertion"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.allow_no_auth is False


def test_parse_serve_md_custom_identity_header():
    text = "## Identity Header\nX-Forwarded-User\n"
    cfg = _parse_serve_md(text)
    assert cfg.identity_header == "X-Forwarded-User"


def test_parse_serve_md_custom_host_and_port():
    text = "## Bind Host\n0.0.0.0\n\n## Bind Port\n9000\n"
    cfg = _parse_serve_md(text)
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000


def test_parse_serve_md_allow_no_auth_presence_enables():
    """Presence of ## Allow No Auth (any body) sets allow_no_auth=True."""
    text = "## Allow No Auth\n"
    cfg = _parse_serve_md(text)
    assert cfg.allow_no_auth is True


def test_parse_serve_md_allow_no_auth_empty_body_also_enables():
    """## Allow No Auth with no body still sets allow_no_auth=True."""
    text = "## Allow No Auth\n\n## Bind Port\n8001\n"
    cfg = _parse_serve_md(text)
    assert cfg.allow_no_auth is True
    assert cfg.port == 8001


def test_parse_serve_md_identity_perimeter_verified_default_false():
    """spec/48: identity_is_perimeter_verified defaults to False (fail-closed)."""
    cfg = _parse_serve_md("")
    assert cfg.identity_is_perimeter_verified is False


def test_parse_serve_md_identity_perimeter_verified_presence_enables():
    """## Identity Is Perimeter Verified (any body) opts in."""
    cfg = _parse_serve_md("## Identity Is Perimeter Verified\n")
    assert cfg.identity_is_perimeter_verified is True


def test_parse_serve_md_identity_perimeter_verified_env_override(monkeypatch):
    """ATOMIC_AGENTS_SERVE_IDENTITY_PERIMETER_VERIFIED truthy values opt in."""
    monkeypatch.setenv("ATOMIC_AGENTS_SERVE_IDENTITY_PERIMETER_VERIFIED", "true")
    assert _parse_serve_md("").identity_is_perimeter_verified is True
    # Negative control: a non-truthy value leaves it False.
    monkeypatch.setenv("ATOMIC_AGENTS_SERVE_IDENTITY_PERIMETER_VERIFIED", "no")
    assert _parse_serve_md("").identity_is_perimeter_verified is False


def test_parse_serve_md_unknown_section_ignored():
    """Unknown ## sections are silently ignored."""
    text = "## Unknown Section\nsome value\n\n## Bind Port\n8500\n"
    cfg = _parse_serve_md(text)
    assert cfg.port == 8500
    # defaults intact
    assert cfg.host == "127.0.0.1"


def test_parse_serve_md_bad_port_raises():
    """Non-integer port value raises ValueError (MUST 2: no silent fallback)."""
    text = "## Bind Port\nnot-a-number\n"
    with pytest.raises(ValueError, match="Bind Port"):
        _parse_serve_md(text)


def test_parse_serve_md_full():
    """Full serve.md with all sections parsed correctly."""
    text = (
        "## Identity Header\n"
        "X-Amzn-Oidc-Identity\n\n"
        "## Bind Host\n"
        "0.0.0.0\n\n"
        "## Bind Port\n"
        "9090\n\n"
        "## Allow No Auth\n"
    )
    cfg = _parse_serve_md(text)
    assert cfg.identity_header == "X-Amzn-Oidc-Identity"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9090
    assert cfg.allow_no_auth is True


# ── env var overrides ────────────────────────────────────────────────────────


def test_env_host_overrides_serve_md(monkeypatch):
    """ATOMIC_AGENTS_SERVE_HOST overrides the file value."""
    monkeypatch.setenv("ATOMIC_AGENTS_SERVE_HOST", "192.168.1.5")
    text = "## Bind Host\n127.0.0.1\n"
    cfg = _parse_serve_md(text)
    assert cfg.host == "192.168.1.5"


def test_env_port_overrides_serve_md(monkeypatch):
    """ATOMIC_AGENTS_SERVE_PORT overrides the file value."""
    monkeypatch.setenv("ATOMIC_AGENTS_SERVE_PORT", "7777")
    text = "## Bind Port\n8000\n"
    cfg = _parse_serve_md(text)
    assert cfg.port == 7777


def test_env_identity_header_overrides_serve_md(monkeypatch):
    """ATOMIC_AGENTS_SERVE_IDENTITY_HEADER overrides the file value."""
    monkeypatch.setenv("ATOMIC_AGENTS_SERVE_IDENTITY_HEADER", "X-Custom-Auth")
    text = "## Identity Header\nX-Goog-IAP-JWT-Assertion\n"
    cfg = _parse_serve_md(text)
    assert cfg.identity_header == "X-Custom-Auth"


def test_env_bad_port_raises(monkeypatch):
    """Non-integer ATOMIC_AGENTS_SERVE_PORT raises ValueError (MUST 2: no silent fallback)."""
    monkeypatch.setenv("ATOMIC_AGENTS_SERVE_PORT", "not-a-number")
    text = "## Bind Port\n8888\n"
    with pytest.raises(ValueError, match="ATOMIC_AGENTS_SERVE_PORT"):
        _parse_serve_md(text)


# ── load_serve_config ────────────────────────────────────────────────────────


def test_load_serve_config_missing_file_returns_defaults(tmp_path: Path):
    """Missing serve.md returns defaults (no error)."""
    agent_root = tmp_path / "myagent"
    agent_root.mkdir()
    cfg = load_serve_config(agent_root)
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.allow_no_auth is False


def test_load_serve_config_reads_file(tmp_path: Path):
    """Present serve.md is read and parsed."""
    agent_root = tmp_path / "myagent"
    agent_root.mkdir()
    (agent_root / "serve.md").write_text("## Bind Port\n9999\n", encoding="utf-8")
    cfg = load_serve_config(agent_root)
    assert cfg.port == 9999


# ── is_loopback ──────────────────────────────────────────────────────────────


def test_is_loopback_127():
    assert is_loopback("127.0.0.1") is True


def test_is_loopback_localhost():
    assert is_loopback("localhost") is True


def test_is_loopback_ipv6():
    assert is_loopback("::1") is True


def test_is_loopback_all_interfaces():
    assert is_loopback("0.0.0.0") is False


def test_is_loopback_private_network():
    assert is_loopback("192.168.1.5") is False


def test_is_loopback_case_insensitive():
    assert is_loopback("LOCALHOST") is True


def test_is_loopback_127_alt():
    """127.0.0.2 is in the 127.0.0.0/8 range — treated as loopback."""
    assert is_loopback("127.0.0.2") is True


def test_is_loopback_127_255():
    """127.255.255.255 is in the 127.0.0.0/8 range — treated as loopback."""
    assert is_loopback("127.255.255.255") is True


def test_is_loopback_invalid_host():
    """An unparseable string is not loopback — returns False, does not raise."""
    assert is_loopback("not-a-host") is False


def test_is_loopback_bracketed_ipv6():
    """Bracketed IPv6 loopback literal '[::1]' must be recognised as loopback.

    Operators sometimes write IPv6 addresses with surrounding brackets in host
    config strings (URL-syntax convention). ``ipaddress.ip_address('[::1]')``
    raises ValueError, so without the bracket-strip fix is_loopback returns
    False and the server refuses to bind unless --allow-no-auth is passed —
    treating a loopback bind as if it were an unsafe network bind.
    spec/37 §"No-auth default". CLAUDE.md principle 13.
    """
    assert is_loopback("[::1]") is True
