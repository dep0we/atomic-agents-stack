"""Canonical dataclasses for the MCPServerRegistryBackend Protocol (spec/36).

Three dataclasses define the MCP server registry substrate contract (issue #201,
spec/36):

- ``MCPServerRef`` -- lightweight listing token returned by ``list_mcp_servers()``;
  cheap to enumerate without loading full specs.
- ``MCPServerRegistryCapabilities`` -- frozen capability advertisement for a backend
  instance; conformance tests assert claim-vs-behavior parity.
- ``ValidationResult`` -- result of a static ``validate(name)`` check (same shape
  as ``ToolRegistryBackend`` spec/25).

No exceptions live here. MCPServerRegistry exceptions live in
``atomic_agents/mcp_registry/backend.py`` to avoid circular imports.

No Protocol definition lives here. The ``MCPServerRegistryBackend`` Protocol is in
``atomic_agents/mcp_registry/backend.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ──────────────────────────────────────────────────────────────────────────────
# MCPServerRef


@dataclass(frozen=True)
class MCPServerRef:
    """Lightweight listing token returned by ``list_mcp_servers()``.

    Carries metadata only. Does NOT include ``command`` / ``args`` / ``env``
    (those are part of the materialized ``MCPServerSpec`` from spec/19, returned
    by ``load_mcp_server(name)``). This lazy/eager distinction matches
    ``ToolRegistryBackend`` Decision 5.

    Fields:

    ``name``: operator-chosen short name; matches ``MCPServerSpec.name``.

    ``description``: operator-readable note; defaults to empty string, matching
        ``MCPServerSpec.description`` parity (prep-pass finding A-F5).

    ``transport``: catalog filtering by transport; defaults to ``"stdio"``
        (only supported transport in v1 per spec/19).

    ``version``: reserved for future use; matches ToolRegistry Decision 4.
        Always ``None`` at v1.0.

    ``source``: backend-specific origin marker. Filesystem backend sets
        ``source="mcp.md#section:<name>"``; HTTP backend sets
        ``source="<catalog_url>/mcp-servers/<name>"``.

        **Operator security note (HTTP backend).** The ``source`` field may
        contain the raw catalog URL including any embedded credentials (e.g.,
        ``https://user:pass@catalog/...``). Operators logging, persisting, or
        displaying MCPServerRef objects MUST redact this field before output.
        Use ``atomic_agents.mcp_registry._redact_for_error_message(ref.source)``
        to strip credentials. The raw URL is preserved per spec/36 line 228 to
        support downstream navigation use cases that need to fetch the resource.
    """

    name: str
    description: str = ""
    transport: str = "stdio"
    version: str | None = None
    source: str = ""

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON round-trip.

        All fields included. ``None`` values are preserved as ``null`` in JSON
        so the round-trip is lossless for the ``version`` field.
        """
        return {
            "name": self.name,
            "description": self.description,
            "transport": self.transport,
            "version": self.version,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> MCPServerRef:
        """Deserialize from a plain dict produced by ``to_dict()``.

        Extra keys in ``d`` are silently ignored for forward-compatibility.
        Missing optional keys fall back to field defaults.

        Version normalization (A-F5): the wire format treats empty string and
        absent key as equivalent to ``None`` for the ``version`` field. This
        keeps round-trips byte-identical when catalog servers omit the field
        or send an explicit ``""``.
        """
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            transport=d.get("transport", "stdio"),
            version=d.get("version") or None,
            source=d.get("source", ""),
        )


# ──────────────────────────────────────────────────────────────────────────────
# MCPServerRegistryCapabilities


@dataclass(frozen=True)
class MCPServerRegistryCapabilities:
    """Capability advertisement for a ``MCPServerRegistryBackend`` instance.

    Conformance tests assert claim-vs-behavior parity (MUST 3). Backends that
    misreport capabilities produce silent failures rather than loud refusals.

    Fields:

    ``supports_install``: True if ``install(spec)`` does not raise
        ``NotImplementedError``. ``FilesystemMCPServerRegistryBackend`` reports
        False at PR 1 (method ships in PR 3). ``HTTPMCPServerRegistryBackend``
        reports False at PR 4 (static class default); dynamic per tier at PR 5.

    ``supports_uninstall``: True if ``uninstall(name)`` does not raise
        ``NotImplementedError``. Same PR cadence as ``supports_install``.

    ``supports_capability_handshake``: True only on ``HTTPMCPServerRegistryBackend``
        (Decision 4). Filesystem backend always reports False (no remote dependency).

    ``supports_audit``: Reserved. Tier-3 HTTP catalog servers may flip this True.
        Both v1.0 reference implementations return False.

    ``durable``: True if backend state persists across process restarts.
        Both v1.0 reference implementations return True. ``durable=False`` is
        reserved for future in-memory test-fixture backends.
    """

    supports_install: bool
    supports_uninstall: bool
    supports_capability_handshake: bool
    supports_audit: bool
    durable: bool


# ──────────────────────────────────────────────────────────────────────────────
# ValidationResult


@dataclass(frozen=True)
class ValidationResult:
    """Result of a static ``validate(name)`` check.

    Same shape as ``ToolRegistryBackend`` (spec/25 canonical types section).

    ``ok``: equivalent to ``not errors``. Callers may branch on ``ok`` for the
        common case; ``errors`` and ``warnings`` provide detail.

    ``errors``: list of error strings. Non-empty means the server is unusable.
        Examples: descriptor does not parse; command not found on PATH; required
        env var not set.

    ``warnings``: list of warning strings. Non-empty means the server is usable
        but flagged. Examples: command not found on PATH at validation time (may
        exist at runtime); env var not set in current process (may be set at
        agent run time); transport value unrecognized but non-empty.
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
