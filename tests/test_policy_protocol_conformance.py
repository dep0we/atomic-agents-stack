"""Conformance test suite for the PolicyBackend Protocol (spec/32).

Parametrized over a ``backend_factory`` fixture that constructs both the
``FilesystemPolicyBackend`` (the filesystem reference implementation) and an
in-memory ``MockPolicyBackend`` registered under ``"mock"`` for the duration
of each test.  Every conformance test runs against BOTH backends so the
Protocol contract is verified independently of the storage substrate.

What this suite asserts (28 tests, PR 3a of #89 — updated from PR 1's 25):

1.  ``FilesystemPolicyBackend(non_existent_path)`` succeeds — construction is
    side-effect-free (F4 / spec/32 MUST #4).
2.  No ``policy.md`` → ``CostCaps()`` returned by ``get_effective_caps``
    (daily + monthly only; cumulative deferred to v1.1 per D1).
3.  No ``policy.md`` → ``is_tool_allowed`` returns ``True``.
4.  No ``policy.md`` → ``is_mcp_server_allowed`` returns ``True``.
5.  No ``policy.md`` → ``get_effective_model`` returns ``None``.
6.  ``agent_name=".."`` raises ``ValueError`` across all 4 query methods.
7.  ``agent_name="foo/bar"`` raises ``ValueError`` across all 4 query methods.
8.  ``agent_name="foo\\\\bar"`` raises ``ValueError`` across all 4 query methods.
9.  ``agent_name="foo\\x00bar"`` raises ``ValueError`` across all 4 query methods.
10. ``agent_name="foo\\nbar"`` raises ``ValueError`` across all 4 query methods.
11. ``agent_name=""`` raises ``ValueError`` across all 4 query methods.
12. ``agent_name=".foo"`` (leading dot) raises ``ValueError`` across all 4 methods.
13. ``agent_name="foo_bar-123"`` is accepted across all 4 methods.
14. Control character in ``tool_name`` raises ``ValueError`` from
    ``is_tool_allowed``.
15. ``tool_name="mcp:server:tool.name"`` (dots + colons) is accepted.
16. ``capabilities()`` returns a ``PolicyCapabilities`` instance.
17. Filesystem backend reports ``cache_ttl_s=0`` in capabilities.
18. Fleet ``cost_caps.daily_usd=50`` applies to an unmentioned agent.
19. Per-agent cap MIN-composes with fleet caps.
20. Fleet ``tools.allow`` restricts unknown tools.
21. Fleet ``tools.deny`` overrides allow in the same layer.
22. Per-agent ``tools.allow`` merges (union) with fleet allow.
23. Per-agent ``tools.deny`` unions with fleet deny.
24. Per-agent model override replaces fleet model.
25. Empty ``agents: foo: {}`` body means no override — fleet defaults apply.
26. D2 loosened charset — "caldwell.research" (dots) accepted.
27. D2 loosened charset — "team-2024+ops" (plus, hyphen) accepted.
28. D2 loosened charset — "ops@fleet" (at-sign) accepted.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from atomic_agents.policy.backend import (
    register_policy_backend,
    unregister_policy_backend,
)
from atomic_agents.policy.types import (
    CostCaps,
    PolicyCapabilities,
)

# FilesystemPolicyBackend is imported inside fixtures / tests so that the test
# file can be collected even before the sibling-lane filesystem.py is written.
# The ``ImportError`` path is exercised only at runtime, not at collection.


# ─────────────────────────────────────────────────────────────────────────────
# Validation helpers — intentional duplication from MockPolicyBackend so mock
# is self-contained and does NOT import from the implementation under test.

# D2 — loosened to match the charset the filesystem backend accepts.
_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.+@-]+$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _validate_agent_name_for_mock(name: str) -> None:  # noqa: D401
    """Raise ``ValueError`` for any agent_name that violates spec/32 MUST #1 (D2)."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"agent_name must be non-empty; got {name!r}")
    if name.startswith(".") or ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"agent_name has path-traversal token; got {name!r}")
    if _CONTROL_CHARS.search(name):
        raise ValueError(f"agent_name has control/newline char; got {name!r}")
    if not _AGENT_NAME_PATTERN.match(name):
        raise ValueError(f"agent_name must match [a-zA-Z0-9_.+@-]+; got {name!r}")


def _validate_tool_or_server_for_mock(name: str) -> None:  # noqa: D401
    """Raise ``ValueError`` for tool/server names with control characters."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"name must be non-empty; got {name!r}")
    if _CONTROL_CHARS.search(name):
        raise ValueError(f"name has control/newline char; got {name!r}")


def _min_or_other(a: float | None, b: float | None) -> float | None:
    """Return MIN of two optional floats; ``None`` means no-opinion."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


# ─────────────────────────────────────────────────────────────────────────────
# MockPolicyBackend — in-memory PolicyBackend for conformance testing only.
#
# This class exists solely to exercise the Protocol seam from a non-filesystem
# vantage point.  It proves that any class satisfying the structural Protocol
# can be registered and exercised by the same test suite.
#
# The mock implements the SAME validation rules as the real backends (agent_name
# pattern, tool/server name control-char check) so validation tests pass on
# both backends without special-casing.


class MockPolicyBackend:
    """In-memory ``PolicyBackend`` for conformance testing.

    Registered under ``"mock"`` via the ``mock_registered`` pytest fixture and
    unregistered in teardown (F3 registry hygiene).  Holds dict-shaped fleet
    defaults and per-agent overrides supplied at construction time, making it
    straightforward to express any policy scenario without touching a file.
    """

    backend_id = "mock"

    def __init__(
        self,
        *,
        fleet_caps: CostCaps | None = None,
        fleet_tools_allow: frozenset[str] = frozenset(),
        fleet_tools_deny: frozenset[str] = frozenset(),
        fleet_mcp_allow: frozenset[str] = frozenset(),
        fleet_mcp_deny: frozenset[str] = frozenset(),
        fleet_model: str | None = None,
        per_agent: dict[str, dict] | None = None,
        cache_ttl_s: int | None = 60,
    ) -> None:
        self._fleet_caps = fleet_caps or CostCaps()
        self._fleet_tools_allow = fleet_tools_allow
        self._fleet_tools_deny = fleet_tools_deny
        self._fleet_mcp_allow = fleet_mcp_allow
        self._fleet_mcp_deny = fleet_mcp_deny
        self._fleet_model = fleet_model
        self._per_agent: dict[str, dict] = per_agent or {}
        self._cache_ttl_s = cache_ttl_s

    # ── PolicyBackend Protocol surface ────────────────────────────────────────

    def get_effective_caps(self, agent_name: str) -> CostCaps:
        _validate_agent_name_for_mock(agent_name)
        agent = self._per_agent.get(agent_name, {})
        caps_override: CostCaps | None = agent.get("cost_caps")
        fleet = self._fleet_caps
        if caps_override is None:
            return fleet
        return CostCaps(
            daily_usd=_min_or_other(fleet.daily_usd, caps_override.daily_usd),
            monthly_usd=_min_or_other(fleet.monthly_usd, caps_override.monthly_usd),
        )

    def is_tool_allowed(self, agent_name: str, tool_name: str) -> bool:
        _validate_agent_name_for_mock(agent_name)
        _validate_tool_or_server_for_mock(tool_name)
        agent = self._per_agent.get(agent_name, {})
        agent_allow: frozenset[str] = agent.get("tools_allow") or frozenset()
        agent_deny: frozenset[str] = agent.get("tools_deny") or frozenset()
        effective_allow = self._fleet_tools_allow | agent_allow
        effective_deny = self._fleet_tools_deny | agent_deny
        if tool_name in effective_deny:
            return False
        return (not effective_allow) or (tool_name in effective_allow)

    def is_mcp_server_allowed(self, agent_name: str, server_name: str) -> bool:
        _validate_agent_name_for_mock(agent_name)
        _validate_tool_or_server_for_mock(server_name)
        agent = self._per_agent.get(agent_name, {})
        agent_allow: frozenset[str] = agent.get("mcp_allow") or frozenset()
        agent_deny: frozenset[str] = agent.get("mcp_deny") or frozenset()
        effective_allow = self._fleet_mcp_allow | agent_allow
        effective_deny = self._fleet_mcp_deny | agent_deny
        if server_name in effective_deny:
            return False
        return (not effective_allow) or (server_name in effective_allow)

    def get_effective_model(self, agent_name: str) -> str | None:
        _validate_agent_name_for_mock(agent_name)
        agent = self._per_agent.get(agent_name, {})
        agent_model: str | None = agent.get("model")
        if agent_model is not None:
            return agent_model
        return self._fleet_model

    def capabilities(self) -> PolicyCapabilities:
        return PolicyCapabilities(cache_ttl_s=self._cache_ttl_s, durable=False)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture
def mock_registered():
    """Register ``MockPolicyBackend`` and unregister in teardown (F3 hygiene)."""
    register_policy_backend("mock", MockPolicyBackend)
    try:
        yield
    finally:
        unregister_policy_backend("mock")


# Policy-md snippets used by multiple tests. Kept at module scope so they can
# be referenced by test functions regardless of backend variant.

_POLICY_MD_FLEET_CAPS_ONLY = """\
cost_caps:
  daily_usd: 50.0
  monthly_usd: 200.0
"""

_POLICY_MD_FLEET_AND_AGENT_CAPS = """\
cost_caps:
  daily_usd: 50.0
  monthly_usd: 200.0

agents:
  my_agent:
    cost_caps:
      daily_usd: 30.0
      monthly_usd: 400.0
"""

_POLICY_MD_FLEET_STRICTER_THAN_AGENT_CAPS = """\
cost_caps:
  daily_usd: 30.0
  monthly_usd: 200.0

agents:
  my_agent:
    cost_caps:
      daily_usd: 50.0
      monthly_usd: 100.0
"""

_POLICY_MD_FLEET_TOOLS_ALLOW = """\
tools:
  allow:
    - tool_a
    - tool_b
"""

_POLICY_MD_FLEET_TOOLS_DENY_OVERRIDES = """\
tools:
  allow:
    - tool_a
    - tool_b
  deny:
    - tool_b
"""

_POLICY_MD_PER_AGENT_TOOL_ALLOW_MERGE = """\
tools:
  allow:
    - tool_a

agents:
  my_agent:
    tools:
      allow:
        - tool_b
"""

_POLICY_MD_PER_AGENT_TOOL_DENY_UNION = """\
tools:
  deny:
    - tool_a

agents:
  my_agent:
    tools:
      deny:
        - tool_b
"""

_POLICY_MD_MODEL_OVERRIDE = """\
model: claude-opus-4-5

agents:
  my_agent:
    model: gpt-4o
"""

_POLICY_MD_EMPTY_AGENT_BODY = """\
cost_caps:
  daily_usd: 50.0

agents:
  foo: {}
"""


@pytest.fixture(params=["filesystem", "mock"])
def backend_factory(request, tmp_path, mock_registered):  # noqa: ARG001
    """Yields a callable that constructs the parametrized backend.

    The ``mock_registered`` fixture is included unconditionally so that the
    mock backend is always registered before a test runs (even when the
    parametrized variant is ``"filesystem"`` — the registration is harmless
    and keeps fixture dependency explicit).

    The yielded factory is a context-manager factory accepting optional
    ``policy_md_content: str`` (seeds a fleet-level ``policy.md`` on the
    filesystem variant) and ``**mock_kwargs`` (forwarded to
    ``MockPolicyBackend`` on the mock variant).  Tests call it as::

        with backend_factory(policy_md_content=...) as backend:
            ...
    """
    backend_id = request.param

    @contextmanager
    def factory(policy_md_content: str | None = None, **mock_kwargs) -> Iterator:
        if backend_id == "filesystem":
            from atomic_agents.policy.filesystem import FilesystemPolicyBackend

            project_root = tmp_path / f"proj-{request.node.name[:32]}"
            project_root.mkdir(exist_ok=True)
            if policy_md_content is not None:
                (project_root / "policy.md").write_text(
                    policy_md_content, encoding="utf-8"
                )
            yield FilesystemPolicyBackend(project_root)
        else:
            yield MockPolicyBackend(**mock_kwargs)

    yield factory


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — Construction is side-effect-free (F4 / spec/32 MUST #4)


def test_construction_is_side_effect_free(tmp_path: Path) -> None:
    """``FilesystemPolicyBackend(non_existent_path)`` succeeds.

    The backend MUST NOT stat, open, or mkdir during construction.  A path
    that does not exist is valid until the first method call triggers lazy
    parsing.  This test is filesystem-only because MockPolicyBackend trivially
    satisfies the property (it has no I/O at all).
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    non_existent = tmp_path / "does-not-exist-yet"
    assert not non_existent.exists()
    backend = FilesystemPolicyBackend(non_existent)
    assert backend is not None


# ─────────────────────────────────────────────────────────────────────────────
# Tests 2-5 — Absent policy.md / no-opinion defaults


def test_no_policy_md_returns_no_opinion_caps(backend_factory) -> None:
    """Absent ``policy.md`` (or empty mock) → ``get_effective_caps`` returns
    a ``CostCaps()`` with all-``None`` fields (no opinion on any dimension).

    v1 ships daily + monthly only (cumulative deferred to v1.1 per D1).
    """
    with backend_factory() as backend:
        caps = backend.get_effective_caps("some_agent")
    assert isinstance(caps, CostCaps)
    assert caps.daily_usd is None
    assert caps.monthly_usd is None


def test_no_policy_md_returns_true_for_tool(backend_factory) -> None:
    """Absent ``policy.md`` → ``is_tool_allowed`` returns ``True``."""
    with backend_factory() as backend:
        allowed = backend.is_tool_allowed("some_agent", "any_tool")
    assert allowed is True


def test_no_policy_md_returns_true_for_mcp(backend_factory) -> None:
    """Absent ``policy.md`` → ``is_mcp_server_allowed`` returns ``True``."""
    with backend_factory() as backend:
        allowed = backend.is_mcp_server_allowed("some_agent", "any_server")
    assert allowed is True


def test_no_policy_md_returns_none_for_model(backend_factory) -> None:
    """Absent ``policy.md`` → ``get_effective_model`` returns ``None``."""
    with backend_factory() as backend:
        model = backend.get_effective_model("some_agent")
    assert model is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests 6-12 — agent_name validation at API boundary (spec/32 MUST #1)
#
# Each invalid name is tried across all four query methods.  A single
# ``ValueError`` from any method satisfies the test; the test verifies ALL
# four methods raise to ensure the validation gate is not missing from any
# code path.

_INVALID_AGENT_NAMES = [
    pytest.param("..", id="dotdot"),
    pytest.param("foo/bar", id="slash"),
    pytest.param("foo\\bar", id="backslash"),
    pytest.param("foo\x00bar", id="control_null"),
    pytest.param("foo\nbar", id="newline"),
    pytest.param("", id="empty"),
    pytest.param(".foo", id="leading_dot"),
]


def _assert_all_four_methods_raise(backend, bad_name: str) -> None:
    """Assert that all four query methods raise ``ValueError`` for ``bad_name``."""
    with pytest.raises(ValueError):
        backend.get_effective_caps(bad_name)
    with pytest.raises(ValueError):
        backend.is_tool_allowed(bad_name, "some_tool")
    with pytest.raises(ValueError):
        backend.is_mcp_server_allowed(bad_name, "some_server")
    with pytest.raises(ValueError):
        backend.get_effective_model(bad_name)


@pytest.mark.parametrize("bad_name", _INVALID_AGENT_NAMES)
def test_agent_name_invalid_refused(backend_factory, bad_name: str) -> None:
    """All 4 query methods raise ``ValueError`` for invalid ``agent_name``."""
    with backend_factory() as backend:
        _assert_all_four_methods_raise(backend, bad_name)


# ─────────────────────────────────────────────────────────────────────────────
# Test 13 — Valid agent_name charset


def test_agent_name_valid_charset_passes(backend_factory) -> None:
    """``agent_name="foo_bar-123"`` (letters, digits, dash, underscore) is
    accepted by all four query methods without raising."""
    with backend_factory() as backend:
        backend.get_effective_caps("foo_bar-123")
        backend.is_tool_allowed("foo_bar-123", "some_tool")
        backend.is_mcp_server_allowed("foo_bar-123", "some_server")
        backend.get_effective_model("foo_bar-123")


# ─────────────────────────────────────────────────────────────────────────────
# Test 14 — Control character in tool_name raises


def test_tool_name_control_char_refused(backend_factory) -> None:
    """``is_tool_allowed`` raises ``ValueError`` when ``tool_name`` contains
    a control character."""
    with backend_factory() as backend:
        with pytest.raises(ValueError):
            backend.is_tool_allowed("agent1", "bad\x00tool")


# ─────────────────────────────────────────────────────────────────────────────
# Test 15 — Dots and colons in tool_name are accepted


def test_tool_name_with_dots_and_colons_accepted(backend_factory) -> None:
    """``tool_name="mcp:server:tool.name"`` (dots + colons, no control chars)
    is accepted by ``is_tool_allowed`` — this is the standard MCP tool-name
    shape (spec/32 MUST #1 lighter check for tool/server names)."""
    with backend_factory() as backend:
        result = backend.is_tool_allowed("some_agent", "mcp:server:tool.name")
    # With no policy.md / no-deny, this must return True (open by default)
    assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# Test 16 — capabilities() returns PolicyCapabilities


def test_capabilities_returns_PolicyCapabilities(backend_factory) -> None:
    """``capabilities()`` returns a ``PolicyCapabilities`` instance."""
    with backend_factory() as backend:
        caps = backend.capabilities()
    assert isinstance(caps, PolicyCapabilities)


# ─────────────────────────────────────────────────────────────────────────────
# Test 17 — Filesystem backend reports cache_ttl_s=0


def test_capabilities_filesystem_cache_ttl_zero(tmp_path: Path) -> None:
    """The filesystem backend declares ``cache_ttl_s=0`` — operators observe
    edits within 0 seconds of mtime change because the backend performs an
    mtime+size check on every method call.

    This test is filesystem-specific and is NOT parametrized over the mock
    backend (the mock reports a configurable TTL per its own contract).
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    backend = FilesystemPolicyBackend(tmp_path)
    caps = backend.capabilities()
    assert caps.cache_ttl_s == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 18 — Fleet cost_caps apply to an unmentioned agent


def test_fleet_cost_caps_apply_to_unmentioned_agent(backend_factory) -> None:
    """Fleet ``cost_caps.daily_usd=50`` applies when the agent is not in the
    ``agents:`` section."""
    fleet_caps = CostCaps(daily_usd=50.0)
    with backend_factory(
        policy_md_content=_POLICY_MD_FLEET_CAPS_ONLY,
        fleet_caps=fleet_caps,
    ) as backend:
        caps = backend.get_effective_caps("unmentioned_agent")
    assert caps.daily_usd == 50.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 19 — Per-agent cap MIN-composes with fleet


def test_per_agent_cap_min_composes_with_fleet(backend_factory) -> None:
    """Per-agent cap uses MIN per dimension with fleet cap.

    Two sub-cases:
    (a) fleet daily=50, agent daily=30 → effective=30 (agent is stricter).
    (b) fleet daily=30, agent daily=50 → effective=30 (fleet is stricter).
    """
    # Sub-case (a): agent is stricter
    per_agent_a = {
        "my_agent": {"cost_caps": CostCaps(daily_usd=30.0, monthly_usd=400.0)}
    }
    with backend_factory(
        policy_md_content=_POLICY_MD_FLEET_AND_AGENT_CAPS,
        fleet_caps=CostCaps(daily_usd=50.0, monthly_usd=200.0),
        per_agent=per_agent_a,
    ) as backend:
        caps_a = backend.get_effective_caps("my_agent")
    assert caps_a.daily_usd == 30.0
    assert caps_a.monthly_usd == 200.0  # fleet is stricter on monthly

    # Sub-case (b): fleet is stricter (agent daily=50 vs fleet daily=30)
    per_agent_b = {
        "my_agent": {"cost_caps": CostCaps(daily_usd=50.0, monthly_usd=100.0)}
    }
    with backend_factory(
        policy_md_content=_POLICY_MD_FLEET_STRICTER_THAN_AGENT_CAPS,
        fleet_caps=CostCaps(daily_usd=30.0, monthly_usd=200.0),
        per_agent=per_agent_b,
    ) as backend:
        caps_b = backend.get_effective_caps("my_agent")
    assert caps_b.daily_usd == 30.0  # fleet is stricter
    assert caps_b.monthly_usd == 100.0  # agent is stricter


# ─────────────────────────────────────────────────────────────────────────────
# Test 20 — Fleet tools.allow restricts unknown tools


def test_fleet_tools_allow_applies(backend_factory) -> None:
    """Fleet ``tools.allow: [tool_a, tool_b]`` allows listed tools and denies
    unlisted ones.  An empty allow-list means "allow everything" — a non-empty
    allow-list means "allow ONLY what's listed."
    """
    with backend_factory(
        policy_md_content=_POLICY_MD_FLEET_TOOLS_ALLOW,
        fleet_tools_allow=frozenset({"tool_a", "tool_b"}),
    ) as backend:
        assert backend.is_tool_allowed("agent1", "tool_a") is True
        assert backend.is_tool_allowed("agent1", "tool_b") is True
        assert backend.is_tool_allowed("agent1", "tool_c") is False


# ─────────────────────────────────────────────────────────────────────────────
# Test 21 — Fleet tools.deny overrides allow in the same layer


def test_fleet_tools_deny_overrides_allow(backend_factory) -> None:
    """Fleet ``tools.allow=[a,b]`` + ``tools.deny=[b]`` → ``a`` is allowed,
    ``b`` is denied (deny-takes-precedence per F7 resolution)."""
    with backend_factory(
        policy_md_content=_POLICY_MD_FLEET_TOOLS_DENY_OVERRIDES,
        fleet_tools_allow=frozenset({"tool_a", "tool_b"}),
        fleet_tools_deny=frozenset({"tool_b"}),
    ) as backend:
        assert backend.is_tool_allowed("agent1", "tool_a") is True
        assert backend.is_tool_allowed("agent1", "tool_b") is False


# ─────────────────────────────────────────────────────────────────────────────
# Test 22 — Per-agent tools.allow merges (union) with fleet allow


def test_per_agent_tool_allow_merges_with_fleet(backend_factory) -> None:
    """Fleet ``allow=[tool_a]`` + agent ``allow=[tool_b]`` → both tools
    allowed for the agent; ``tool_c`` is still denied (merged allow-list is
    non-empty).
    """
    per_agent = {"my_agent": {"tools_allow": frozenset({"tool_b"})}}
    with backend_factory(
        policy_md_content=_POLICY_MD_PER_AGENT_TOOL_ALLOW_MERGE,
        fleet_tools_allow=frozenset({"tool_a"}),
        per_agent=per_agent,
    ) as backend:
        assert backend.is_tool_allowed("my_agent", "tool_a") is True
        assert backend.is_tool_allowed("my_agent", "tool_b") is True
        assert backend.is_tool_allowed("my_agent", "tool_c") is False


# ─────────────────────────────────────────────────────────────────────────────
# Test 23 — Per-agent tools.deny unions with fleet deny


def test_per_agent_tool_deny_unions_with_fleet(backend_factory) -> None:
    """Fleet ``deny=[tool_a]`` + agent ``deny=[tool_b]`` → both denied for the
    agent; additional tools outside the deny list remain allowed (open by
    default because effective_allow is empty).
    """
    per_agent = {"my_agent": {"tools_deny": frozenset({"tool_b"})}}
    with backend_factory(
        policy_md_content=_POLICY_MD_PER_AGENT_TOOL_DENY_UNION,
        fleet_tools_deny=frozenset({"tool_a"}),
        per_agent=per_agent,
    ) as backend:
        assert backend.is_tool_allowed("my_agent", "tool_a") is False
        assert backend.is_tool_allowed("my_agent", "tool_b") is False
        assert backend.is_tool_allowed("my_agent", "tool_c") is True


# ─────────────────────────────────────────────────────────────────────────────
# Test 24 — Per-agent model override replaces fleet model


def test_model_override_replaces_fleet(backend_factory) -> None:
    """Fleet ``model=claude-opus-4-5``, agent ``model=gpt-4o`` → the agent
    gets ``gpt-4o``; an unmentioned agent gets the fleet model.

    Model selection is REPLACE (not MERGE) — only one model can be active.
    The per-agent override wins when present.
    """
    per_agent = {"my_agent": {"model": "gpt-4o"}}
    with backend_factory(
        policy_md_content=_POLICY_MD_MODEL_OVERRIDE,
        fleet_model="claude-opus-4-5",
        per_agent=per_agent,
    ) as backend:
        assert backend.get_effective_model("my_agent") == "gpt-4o"
        assert backend.get_effective_model("other_agent") == "claude-opus-4-5"


# ─────────────────────────────────────────────────────────────────────────────
# Test 25 — Empty agents body means no override (F12)


def test_empty_agents_body_means_no_override(backend_factory) -> None:
    """``agents: { foo: {} }`` — empty per-agent section — means no override
    for agent ``foo``; fleet defaults apply unchanged (F12).

    This pins the parser's behavior: an empty ``{}`` must be treated as "no
    overrides present", NOT as "override with empty caps (which would mean
    effectively zero caps)".
    """
    fleet_caps = CostCaps(daily_usd=50.0)
    with backend_factory(
        policy_md_content=_POLICY_MD_EMPTY_AGENT_BODY,
        fleet_caps=fleet_caps,
        per_agent={"foo": {}},  # empty per-agent dict → no overrides
    ) as backend:
        caps = backend.get_effective_caps("foo")
    # Must equal fleet_caps — no override applied
    assert caps.daily_usd == 50.0


# ─────────────────────────────────────────────────────────────────────────────
# Tests 26-28 — D2 loosened agent_name charset


def test_agent_name_with_dot_accepted(backend_factory) -> None:
    """``agent_name="caldwell.research"`` (internal dot) is accepted by all
    four query methods after D2 charset loosening.

    Dots were previously rejected but appear in real operator deployments
    (e.g. role-scoped names, project-qualified names).  The loosened pattern
    ``[a-zA-Z0-9_.+@-]+`` permits them while still rejecting leading-dot
    (filesystem hidden-file trick) and ``..`` (directory traversal).
    """
    with backend_factory() as backend:
        backend.get_effective_caps("caldwell.research")
        backend.is_tool_allowed("caldwell.research", "some_tool")
        backend.is_mcp_server_allowed("caldwell.research", "some_server")
        backend.get_effective_model("caldwell.research")


def test_agent_name_with_plus_and_hyphen_accepted(backend_factory) -> None:
    """``agent_name="team-2024+ops"`` (plus + hyphen) is accepted by all four
    query methods after D2 charset loosening."""
    with backend_factory() as backend:
        backend.get_effective_caps("team-2024+ops")
        backend.is_tool_allowed("team-2024+ops", "some_tool")
        backend.is_mcp_server_allowed("team-2024+ops", "some_server")
        backend.get_effective_model("team-2024+ops")


def test_agent_name_with_at_sign_accepted(backend_factory) -> None:
    """``agent_name="ops@fleet"`` (at-sign) is accepted by all four query
    methods after D2 charset loosening.

    Path-traversal tokens (.., /, \\), leading dot, control chars, and
    newlines are STILL rejected — the loosening only adds the characters
    operators actually use in practice.
    """
    with backend_factory() as backend:
        backend.get_effective_caps("ops@fleet")
        backend.is_tool_allowed("ops@fleet", "some_tool")
        backend.is_mcp_server_allowed("ops@fleet", "some_server")
        backend.get_effective_model("ops@fleet")
