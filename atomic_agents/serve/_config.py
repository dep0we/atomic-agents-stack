"""serve/_config.py — ServeConfig dataclass and serve.md parser.

Uses the same ``## Section`` header convention as model.md / mcp.md, with a
generic section-iteration regex (``^## <name>$``) rather than the targeted
named-section patterns in _model.py. The shared convention is the ##-header
config aesthetic (spec design principle 7); the regex shapes differ.
See spec/37 §"Config file — serve.md".
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ServeConfig:
    """Resolved serve configuration for one agent.

    Defaults match spec/37 §"Config file — serve.md".
    """

    identity_header: str = "X-Goog-IAP-JWT-Assertion"
    host: str = "127.0.0.1"
    port: int = 8000
    allow_no_auth: bool = False
    # Maximum bytes accepted for a single POST body. Requests that exceed this
    # limit return HTTP 413 before any JSON parsing. The default (1 MiB) is
    # generous for any realistic work_item payload and protects the single
    # Cloud Run instance from OOM DoS via an unbounded body stream.
    # CWE-770 / Finding #401.
    max_body_bytes: int = 1 * 1024 * 1024  # 1 MiB


# Explicit loopback hostnames recognised in addition to the 127.0.0.0/8 range.
_LOOPBACK_HOSTNAMES = {"localhost"}

# Pattern: ## Section Name (captures the section header; body follows until
# the next ## header or end of file). Same shape as _model.py section regex.
_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)


def _parse_serve_md(text: str) -> ServeConfig:
    """Parse serve.md text into a ServeConfig.

    Section bodies are stripped of whitespace. A missing section uses the
    default for that field. ``## Allow No Auth`` presence (any body or empty)
    sets allow_no_auth=True.

    Env vars override after parsing (resolution order: env > file > default).
    """
    cfg = ServeConfig()

    # Split text into (section_name, section_body) pairs.
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        section_name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()

        sn_lower = section_name.lower()
        if sn_lower == "identity header":
            if body:
                cfg.identity_header = body
        elif sn_lower == "bind host":
            if body:
                cfg.host = body
        elif sn_lower == "bind port":
            if body:
                try:
                    cfg.port = int(body)
                except ValueError:
                    # MUST 2: malformed port must not silently fall back to default.
                    raise ValueError(
                        f"serve.md '## Bind Port' value is not a valid integer: {body!r}"
                    ) from None
        elif sn_lower == "allow no auth":
            # Presence of this section (any value or empty) enables no-auth.
            cfg.allow_no_auth = True
        elif sn_lower == "max body bytes":
            if body:
                try:
                    cfg.max_body_bytes = int(body)
                except ValueError:
                    raise ValueError(
                        f"serve.md '## Max Body Bytes' value is not a valid integer: {body!r}"
                    ) from None

    # Apply env var overrides (highest priority). spec/37 resolution order.
    env_host = os.environ.get("ATOMIC_AGENTS_SERVE_HOST")
    if env_host:
        cfg.host = env_host

    env_port = os.environ.get("ATOMIC_AGENTS_SERVE_PORT")
    if env_port:
        try:
            cfg.port = int(env_port)
        except ValueError:
            # MUST 2: malformed env override must not silently fall back to default.
            raise ValueError(
                f"ATOMIC_AGENTS_SERVE_PORT is not a valid integer: {env_port!r}"
            ) from None

    env_header = os.environ.get("ATOMIC_AGENTS_SERVE_IDENTITY_HEADER")
    if env_header:
        cfg.identity_header = env_header

    return cfg


def load_serve_config(agent_root: Path) -> ServeConfig:
    """Load and parse serve.md from the agent folder.

    A missing serve.md is not an error — returns defaults. A present but
    unreadable serve.md raises OSError (caller handles startup refusal).
    """
    serve_md = agent_root / "serve.md"
    if not serve_md.exists():
        # No file: start from defaults, still apply env vars.
        return _parse_serve_md("")
    text = serve_md.read_text(encoding="utf-8")
    return _parse_serve_md(text)


def is_loopback(host: str) -> bool:
    """Return True when the bind address is a loopback address.

    Treats the full 127.0.0.0/8 range and ::1 as loopback (matching the OS
    definition), plus the 'localhost' hostname. spec/37 §"No-auth default".
    """
    h = host.strip().lower()
    if h in _LOOPBACK_HOSTNAMES:
        return True
    # Strip surrounding brackets so that IPv6 literals written as `[::1]`
    # (common in host config strings) are recognised as loopback.
    # `ipaddress.ip_address('[::1]')` raises ValueError; the bracket form
    # is URL-syntax sugar, not a valid address string for the stdlib parser.
    h = h.removeprefix("[").removesuffix("]")
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False
