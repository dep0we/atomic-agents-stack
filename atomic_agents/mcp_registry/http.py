"""HTTPMCPServerRegistryBackend -- HTTP-catalog reference implementation.

Implements the full MCPServerRegistryBackend Protocol (list, load, load_all,
validate, install, uninstall, capabilities, refresh_capabilities, close)
against a JSON-over-HTTPS catalog server conforming to spec/36 Decision 4's
three-tier wire contract.

Write paths (install, uninstall) ship at PR 5 with full tier-gating, mid-session
tier regression handling, 409 collision mapping, and 204 idempotent delete.

Wire format (spec/36 PR 4 amendments):
    GET /mcp-servers?agent_scope=<scope>
    GET /mcp-servers?agent_scope=<scope>&expand=spec
    GET /mcp-servers/<name>?agent_scope=<scope>
    GET /mcp-servers/<name>/validate?agent_scope=<scope>
    GET /capabilities  (optional; tier-3 probe)

HTTP optional extra: ``pip install 'atomic-agents-stack[http]'``.
The ``httpx`` library is NOT imported at module level -- it loads lazily
at first method call so operators using only the filesystem backend do
not pay the import cost.

Registration: importing this module registers ``"http"`` in the
``MCPServerRegistryBackend`` registry (per ``register_backend_placement_convention``
and the ``locks/redis.py`` precedent). The registration call at the bottom
of this file fires at import time.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
import time
from dataclasses import replace
from typing import Any, Literal, NoReturn
from urllib.parse import urlencode

from .backend import (
    MCPRegistryAuthRequired,
    MCPRegistryDescriptorInvalid,
    MCPRegistryUnavailable,
    MCPServerAlreadyInstalled,
    MCPServerNotInRegistry,
)
from .types import MCPServerRef, MCPServerRegistryCapabilities, ValidationResult
from ..mcp import MCPServerSpec, _resolve_env_vars

_logger = logging.getLogger(__name__)

# Charset rule from MUST 1; mirrors filesystem.py and CorpusBackend.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_.+@-]+$")
_PATH_TRAVERSAL_TOKENS = frozenset({"..", "."})

# Default command-basename allowlist for the spawn gate (MUST 12).
# Only bare basenames; path-qualified commands are never in the default set.
# Operators may extend this via '## Allowed commands' in mcp.md.
_DEFAULT_COMMAND_ALLOWLIST: frozenset[str] = frozenset(
    {"npx", "uvx", "python", "python3", "node", "docker"}
)

# Hard byte ceiling for catalog HTTP responses (CWE-400 / #403).
# A slow-streaming catalog that never exceeds the per-chunk read-timeout can
# balloon RSS without bound; this cap enforces termination regardless of how
# the body arrives.  8 MB is generous for any realistic MCP catalog; operators
# needing a higher limit can pass max_response_bytes= to the constructor.
_MAX_CATALOG_RESPONSE_BYTES: int = 8 * 1024 * 1024  # 8 MB

# Module-level lazy reference to the httpx package.
# Set to the actual ``httpx`` module on first successful import by ``_get_httpx()``.
_httpx: Any = None


def _get_httpx() -> Any:
    """Return the ``httpx`` module, importing it lazily on first call.

    Raises ``ImportError`` with an operator-readable install instruction when
    ``httpx`` is not installed. Mirrors the ``[redis]`` pattern in
    ``locks/redis.py:481-487``.
    """
    global _httpx
    if _httpx is None:
        try:
            import httpx as _httpx_pkg

            _httpx = _httpx_pkg
        except ImportError as exc:
            raise ImportError(
                "HTTPMCPServerRegistryBackend requires the 'httpx' extra. "
                "Install via: pip install 'atomic-agents-stack[http]'"
            ) from exc
    return _httpx


def _validate_server_name(name: str) -> None:
    """Raise ``ValueError`` when ``name`` fails the MUST 1 charset rule.

    Mirrors ``filesystem.py:_validate_server_name`` exactly.
    """
    if not name:
        raise ValueError("MCP server name must not be empty.")
    if name.startswith("."):
        raise ValueError(
            f"MCP server name {name!r} must not start with '.'; "
            f"leading-dot names are reserved for hidden files."
        )
    if not _NAME_RE.match(name):
        raise ValueError(
            f"MCP server name {name!r} contains invalid characters. "
            f"Allowed: [a-zA-Z0-9_.+@-]"
        )
    if name in _PATH_TRAVERSAL_TOKENS:
        raise ValueError(
            f"MCP server name {name!r} is a path-traversal token and is not allowed."
        )


def _redact_url_for_error(url: str) -> str:
    """Strip embedded credentials from a URL for safe operator-facing output.

    Handles ``https://user:pass@host/`` credential embedding by returning
    ``https://...`` when ``://`` is present in the URL. Uses the same
    ``_redact_for_error_message`` helper from ``mcp_registry/__init__.py``
    (per D-PR4-4 -- that module has the DSN heuristic the other modules lack).
    """
    from . import _redact_for_error_message

    return _redact_for_error_message(url)


# ──────────────────────────────────────────────────────────────────────────────
# Transport-security helpers (MUST 11)


def _is_loopback_host(hostname: str | None) -> bool:
    """Return True when ``hostname`` resolves to a loopback address.

    Decision tree (P0 prep finding — must use urlparse().hostname, not netloc):
    1. ``hostname is None``  → non-loopback (fall to default-deny).
    2. ``hostname.lower() == 'localhost'`` → loopback (literal string; no DNS
       resolution — MUST 2 compliant, Principle #12 compliant).
    3. ``ipaddress.ip_address(hostname).is_loopback`` → loopback if True.
       Covers full 127.0.0.0/8 block and ``::1`` correctly.
    4. ``ValueError`` from ipaddress (non-IP string like a bare hostname) →
       non-loopback (default-deny for unknown hostname forms).

    0.0.0.0 is explicitly NOT loopback (ipaddress classifies it correctly as
    non-loopback via is_loopback → False). Any hostname with a DNS component
    (e.g., "my-local-host.example.com") falls to step 4 → non-loopback.

    IMPORTANT: DNS resolution is NEVER performed here — callers who pass a
    non-literal hostname will get non-loopback. This is intentional; DNS
    resolution would violate MUST 2 (side-effect-free construction) and
    Principle #12 (verify before claim; don't speculate about network reachability).

    Args:
        hostname: The ``urlparse(url).hostname`` result (already lowercased by
                  urlparse; may be None for malformed URLs like ``http://``).

    Returns:
        True if the hostname is a loopback address; False otherwise.
    """
    if hostname is None:
        return False
    # Normalize to lowercase; urlparse already lowercases but be explicit.
    hostname_lower = hostname.lower()
    if hostname_lower == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname_lower).is_loopback
    except ValueError:
        # Non-IP hostname (e.g., "my.local.dev.example.com") — treat as remote.
        return False


def _assert_scheme_allowed(
    url: str,
    *,
    allow_http_non_loopback: bool = False,
) -> None:
    """Assert the catalog URL's scheme is safe for use.

    Three valid paths:
    1. ``https://`` — always allowed for any host.
    2. ``http://`` with a loopback host (127.0.0.0/8, ::1, or literal
       "localhost") — automatically allowed (LOOPBACK-AUTO).
    3. ``http://`` with a non-loopback host + ``allow_http_non_loopback=True``
       (ENV OPT-IN) — allowed only when explicitly unlocked.

    Everything else — including non-http/https schemes — raises ``ValueError``
    with a redacted URL so credentials don't surface in error messages.

    ``0.0.0.0`` is classified as non-loopback by ``_is_loopback_host`` (correct;
    ipaddress.ip_address('0.0.0.0').is_loopback is False).  Resolved hostnames
    (anything that requires DNS) are also non-loopback by design.

    Args:
        url:                   The catalog URL to check (may contain credentials;
                               they are redacted in any error message via
                               ``_redact_url_for_error``).
        allow_http_non_loopback: When True, http:// is permitted for non-loopback
                               hosts. Callers compute this once at construction
                               from the constructor kwarg + env var fallback.

    Raises:
        ValueError: when the URL has a genuinely unsupported scheme (ftp, file,
                    etc.), when the URL is a non-loopback http:// URL and
                    ``allow_http_non_loopback`` is False, OR when the URL is
                    too malformed to classify (fail-closed — see below).
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower() if parsed.scheme else ""
        # Access .hostname inside the try: a malformed netloc (e.g. "http://[")
        # raises ValueError on the .hostname property, not on urlparse() itself.
        hostname = parsed.hostname  # None for malformed URLs like "http://"
    except (ValueError, TypeError):
        # FAIL CLOSED (Principle #8 / MUST 11): a URL we cannot parse well
        # enough to classify its host must NOT be granted the non-loopback
        # exemption. Refuse with a redacted message rather than letting an
        # unparseable-but-http-looking URL (e.g. "http://[") slip past the
        # scheme gate to I/O. The spawn gate (MUST 12) still backstops RCE,
        # but a security scheme gate must default-deny on ambiguity.
        safe_url = _redact_url_for_error(url)
        raise ValueError(
            f"HTTPMCPServerRegistryBackend: catalog URL {safe_url!r} could not "
            f"be parsed well enough to classify its scheme/host; refusing "
            f"(fail-closed). Provide a well-formed https:// (or loopback "
            f"http://) catalog URL."
        ) from None

    # Path 1 — https always allowed.
    if scheme == "https":
        return

    # Paths 2 + 3 — http with host classification.
    if scheme == "http":
        # hostname is already lowercased by urlparse; use it for loopback check.
        if _is_loopback_host(hostname):
            # LOOPBACK-AUTO: always allowed regardless of opt-in.
            return
        if allow_http_non_loopback:
            # ENV OPT-IN: operator has explicitly unlocked non-loopback http.
            return
        # Non-loopback http without opt-in → refuse.
        safe_url = _redact_url_for_error(url)
        raise ValueError(
            f"HTTPMCPServerRegistryBackend: cleartext http:// is refused for "
            f"non-loopback catalog URL {safe_url!r}. "
            f"Use https:// for remote catalog servers (recommended), or set "
            f"ATOMIC_AGENTS_MCP_SERVER_REGISTRY_ALLOW_HTTP=1 to explicitly "
            f"allow non-loopback http:// (not recommended for production). "
            f"ATOMIC_AGENTS_MCP_SERVER_REGISTRY_ALLOW_HTTP=1 must be exactly "
            f"the string '1'; any other value (including 'true' or 'yes') keeps "
            f"the default-deny behavior."
        )

    # Unsupported scheme (ftp, file, ws, etc.).
    safe_url = _redact_url_for_error(url)
    raise ValueError(
        f"HTTPMCPServerRegistryBackend catalog URL must start with "
        f"http:// or https://, got {safe_url!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Wire-format parse helpers


def _parse_mcp_server_spec_from_dict(d: Any, *, url: str) -> MCPServerSpec:
    """Parse and validate a single MCPServerSpec from a wire-format dict.

    Full shape validation per C-F3 (prep notes). Raises
    ``MCPRegistryDescriptorInvalid`` on any malformed response: non-dict
    input, missing required fields, wrong field types, name failing the
    MUST 1 charset rule, or injection tokens in the ``name`` field.

    Args:
        d:   The value from the catalog server's JSON response expected to
             be a server spec dict.
        url: The redacted catalog URL to include in error messages.
    """
    if not isinstance(d, dict):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a server entry that is not a "
            f"dict (got {type(d).__name__!r})"
        )
    try:
        name = d["name"]
        command = d["command"]
    except KeyError as exc:
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a server entry missing required "
            f"field {exc.args[0]!r}"
        ) from exc

    if not isinstance(name, str) or not name:
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a server entry with invalid "
            f"'name' value: {name!r}"
        )
    # Charset + injection defense: refuse names with path-traversal / newlines.
    if not _NAME_RE.match(name) or name in _PATH_TRAVERSAL_TOKENS:
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a server entry with non-conformant "
            f"'name' field {name!r} (failed MUST 1 charset rule). "
            f"Possible catalog-server injection; refusing response."
        )

    if not isinstance(command, str):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a server entry with invalid "
            f"'command' value (must be a string, got {type(command).__name__!r})"
        )

    # Defense-in-depth early warning (P2): emit a WARNING when the command
    # basename is not in the default allowlist.  This is NOT a hard reject at
    # parse time because the operator may have extended the allowlist in mcp.md
    # (unavailable here).  The authoritative hard block fires at spawn time in
    # MCPClientPool._check_command_allowlist.
    _cmd_basename = os.path.basename(command)
    if _cmd_basename not in _DEFAULT_COMMAND_ALLOWLIST:
        _logger.warning(
            "catalog server at %s returned server entry %r with command "
            "basename %r which is not in the default spawn allowlist %s. "
            "The command will be blocked at spawn time unless you add it to "
            "'## Allowed commands' in mcp.md.",
            url,
            name,
            _cmd_basename,
            sorted(_DEFAULT_COMMAND_ALLOWLIST),
        )

    args_raw = d.get("args", [])
    if not isinstance(args_raw, list):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a server entry where 'args' is "
            f"not a list (got {type(args_raw).__name__!r})"
        )
    for i, a in enumerate(args_raw):
        if not isinstance(a, str):
            raise MCPRegistryDescriptorInvalid(
                f"catalog server at {url} returned a server entry where "
                f"'args[{i}]' is not a string (got {type(a).__name__!r})"
            )

    env_raw = d.get("env", {})
    if not isinstance(env_raw, dict):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a server entry where 'env' is "
            f"not a dict (got {type(env_raw).__name__!r})"
        )
    for k, v in env_raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise MCPRegistryDescriptorInvalid(
                f"catalog server at {url} returned a server entry where "
                f"'env' contains a non-string-to-string mapping"
            )

    try:
        return MCPServerSpec(
            name=name,
            command=command,
            args=list(args_raw),
            env=dict(env_raw),
            transport=str(d.get("transport", "stdio")),
            description=str(d.get("description", "")),
        )
    except (TypeError, ValueError) as exc:
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a malformed server entry: {exc}"
        ) from exc


def _parse_servers_list(data: Any, *, url: str) -> list[MCPServerSpec]:
    """Parse a bulk server list (spec form) from a catalog response.

    Expects ``data`` to be a dict with a ``"servers"`` key whose value is a
    list of MCPServerSpec-shaped dicts. Each entry is validated via
    ``_parse_mcp_server_spec_from_dict``.
    """
    if not isinstance(data, dict):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a non-dict response "
            f"(got {type(data).__name__!r})"
        )
    servers_raw = data.get("servers")
    if servers_raw is None:
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} response is missing 'servers' key"
        )
    if not isinstance(servers_raw, list):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned 'servers' that is not a list "
            f"(got {type(servers_raw).__name__!r})"
        )
    return [_parse_mcp_server_spec_from_dict(entry, url=url) for entry in servers_raw]


def _parse_servers_list_to_refs(
    data: Any, *, url: str, source_url: str
) -> list[MCPServerRef]:
    """Parse a server listing response into lightweight ``MCPServerRef`` objects.

    Expects the same ``{"servers": [...]}`` envelope as ``_parse_servers_list``,
    but only extracts metadata fields (name, description, transport, version,
    source).

    Args:
        data: The catalog response body parsed as a Python dict.
        url:  The redacted catalog URL for inclusion in error messages
              (operator-facing; credentials stripped).
        source_url: The catalog URL used to build ``MCPServerRef.source``
              per spec/36 line 228 (raw URL). The spec requires the source
              field be a usable URL for downstream navigation. If the
              operator embeds credentials in ``catalog_url`` (instead of
              using the auth_token env var), the credentials surface here.
              The recommended operator pattern is the auth_token env var
              (spec/36 §Operator surface).
    """
    if not isinstance(data, dict):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a non-dict response "
            f"(got {type(data).__name__!r})"
        )
    servers_raw = data.get("servers")
    if servers_raw is None:
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} response is missing 'servers' key"
        )
    if not isinstance(servers_raw, list):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned 'servers' that is not a list "
            f"(got {type(servers_raw).__name__!r})"
        )
    refs = []
    for entry in servers_raw:
        if not isinstance(entry, dict):
            raise MCPRegistryDescriptorInvalid(
                f"catalog server at {url} returned a server list entry that is "
                f"not a dict (got {type(entry).__name__!r})"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise MCPRegistryDescriptorInvalid(
                f"catalog server at {url} returned a server list entry with "
                f"invalid 'name' value: {name!r}"
            )
        # Charset + injection defense.
        if not _NAME_RE.match(name) or name in _PATH_TRAVERSAL_TOKENS:
            raise MCPRegistryDescriptorInvalid(
                f"catalog server at {url} returned a server list entry with "
                f"non-conformant 'name' field {name!r} (failed MUST 1 charset). "
                f"Possible catalog-server injection; refusing response."
            )
        refs.append(
            MCPServerRef(
                name=name,
                description=str(entry.get("description", "")),
                transport=str(entry.get("transport", "stdio")),
                version=entry.get("version") or None,
                source=f"{source_url}/mcp-servers/{name}",
            )
        )
    return refs


def _parse_validation_result(data: Any, *, url: str) -> ValidationResult:
    """Parse a ``/validate`` endpoint response into a ``ValidationResult``.

    Expects ``{"ok": bool, "errors": [...], "warnings": [...]}``.
    """
    if not isinstance(data, dict):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a non-dict /validate response "
            f"(got {type(data).__name__!r})"
        )
    ok = data.get("ok")
    if not isinstance(ok, bool):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} /validate response missing or non-bool 'ok' field"
        )
    errors_raw = data.get("errors", [])
    warnings_raw = data.get("warnings", [])
    if not isinstance(errors_raw, list) or not isinstance(warnings_raw, list):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} /validate response has non-list 'errors' or "
            f"'warnings' field"
        )
    errors = [str(e) for e in errors_raw]
    warnings = [str(w) for w in warnings_raw]
    return ValidationResult(ok=ok, errors=errors, warnings=warnings)


def _parse_capabilities_response(
    data: Any, *, url: str
) -> MCPServerRegistryCapabilities:
    """Parse a ``GET /capabilities`` response into ``MCPServerRegistryCapabilities``.

    At PR 4, only read-path capabilities matter (``supports_install`` and
    ``supports_uninstall`` may be True if the server reports tier 2+, but the
    HTTP backend stubs those methods with ``NotImplementedError`` at PR 4).
    The parsed values are stored for reporting; callers still get
    ``NotImplementedError`` on the write paths until PR 5.
    """
    if not isinstance(data, dict):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a non-dict /capabilities response"
        )
    # Per spec/36 Decision 4 step 1: authoritative if 200.
    # Parse with best-effort defaults; missing fields fall back to tier-1 safe values.
    supports_install = bool(data.get("supports_install", False))
    supports_uninstall = bool(data.get("supports_uninstall", False))
    supports_audit = bool(data.get("supports_audit", False))
    return MCPServerRegistryCapabilities(
        supports_install=supports_install,
        supports_uninstall=supports_uninstall,
        supports_capability_handshake=True,
        supports_audit=supports_audit,
        durable=True,
    )


def _materialize_spec(raw: MCPServerSpec, url: str) -> MCPServerSpec:
    """Apply env-var resolution to a parsed spec (MUST 8).

    Returns a new MCPServerSpec with ``$VAR`` references in ``env`` resolved
    against the client process environment. Both ``load_mcp_server`` and
    ``load_all_mcp_servers`` route through this helper to guarantee structural
    equivalence (MUST 10 / C-F4).

    Args:
        raw: MCPServerSpec as parsed from wire; may contain unresolved ``$VAR``
             strings in ``env``.
        url: Redacted catalog URL used only in exception messages from
             ``_resolve_env_vars``.
    """
    if not raw.env:
        return raw
    resolved_env = _resolve_env_vars(raw.env, raw.name)
    return replace(raw, env=resolved_env)


def _handle_http_error(
    exc: Any,
    *,
    url: str,
    expect_404_means_not_found_for_name: str | None = None,
    expect_409_means_collision: bool = False,
    installed_server_name: str | None = None,
) -> None:
    """Translate an ``httpx`` exception into the appropriate MCPRegistry exception.

    Central exception mapper per the prep notes table (D-PR4-5). Raises the
    mapped exception; never returns normally.

    Args:
        exc:  The caught exception.
        url:  The redacted catalog URL for operator-facing messages.
        expect_404_means_not_found_for_name: When set to a server name string,
            a 404 ``HTTPStatusError`` raises ``MCPServerNotInRegistry`` for that
            name instead of ``MCPRegistryUnavailable``.
        expect_409_means_collision: When True, a 409 ``HTTPStatusError`` raises
            ``MCPServerAlreadyInstalled`` instead of ``MCPRegistryUnavailable``.
            Pass True only from ``install()`` paths where the catalog server's 409
            semantics mean "this name already exists in this scope".
        installed_server_name: When set alongside ``expect_409_means_collision``,
            the server name is included in the ``MCPServerAlreadyInstalled``
            message per spec/36 §Install/uninstall semantics (HTTP).
    """
    httpx = _get_httpx()

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            raise MCPRegistryAuthRequired(
                f"catalog server at {url} returned 401 Unauthorized. "
                f"Set ATOMIC_AGENTS_MCP_SERVER_REGISTRY_AUTH_TOKEN or pass "
                f"auth_token= to the constructor."
            ) from exc
        if status == 404:
            if expect_404_means_not_found_for_name is not None:
                raise MCPServerNotInRegistry(
                    f"MCP server {expect_404_means_not_found_for_name!r} not "
                    f"found in catalog at {url} (HTTP 404)."
                ) from exc
            raise MCPRegistryUnavailable(
                f"catalog server at {url} returned unexpected 404; "
                f"the catalog server may be misconfigured (status={status})."
            ) from exc
        if status == 409 and expect_409_means_collision:
            # 409 on POST /mcp-servers means name collision (MUST 9 atomicity).
            # The catalog server already has an entry for this name+scope pair.
            raise MCPServerAlreadyInstalled(
                f"MCP server {installed_server_name!r} is already installed at catalog {url} (HTTP 409). "
                f"Uninstall it first or choose a different name."
            ) from exc
        if status >= 500:
            raise MCPRegistryUnavailable(
                f"catalog server at {url} returned HTTP {status} (server error)."
            ) from exc
        # Other 4xx (400, 403, 409 without the collision flag, 422, etc.)
        # surface as Unavailable per prep notes B-F8: do NOT silently fall back
        # on non-404 4xx.
        raise MCPRegistryUnavailable(
            f"catalog server at {url} returned unexpected HTTP {status}."
        ) from exc

    if isinstance(exc, httpx.LocalProtocolError):
        raise MCPRegistryDescriptorInvalid(
            f"HTTP client sent an invalid request to catalog at {url}: {exc}"
        ) from exc

    if isinstance(exc, httpx.DecodingError):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} response could not be decoded: {exc}"
        ) from exc

    if isinstance(exc, httpx.TimeoutException):
        raise MCPRegistryUnavailable(
            f"request to catalog server at {url} timed out: {exc}"
        ) from exc

    if isinstance(exc, httpx.NetworkError):
        raise MCPRegistryUnavailable(
            f"network error reaching catalog server at {url}: {exc}"
        ) from exc

    if isinstance(exc, httpx.ProtocolError):
        raise MCPRegistryUnavailable(
            f"HTTP protocol error communicating with catalog at {url}: {exc}"
        ) from exc

    # Final catch-all: any remaining httpx.HTTPError subclass.
    if isinstance(exc, httpx.HTTPError):
        raise MCPRegistryUnavailable(
            f"HTTP error communicating with catalog at {url}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # httpx.InvalidURL does NOT inherit from HTTPError.
    if isinstance(exc, httpx.InvalidURL):
        raise ValueError(f"Invalid catalog URL {url!r}: {exc}") from exc

    # RuntimeError: raised by httpx.Client.send() when the client has been
    # closed. Adversarial F2: a thread reading self._real_client outside the
    # client_lock can hold a reference that gets closed by a concurrent
    # close() call. Map to MCPRegistryUnavailable so the framework's
    # MCPRegistry* catch-alls see a coherent exception.
    if isinstance(exc, RuntimeError):
        raise MCPRegistryUnavailable(
            f"HTTP client communicating with catalog at {url} was closed "
            f"mid-request: {exc}"
        ) from exc

    # json.JSONDecodeError
    if isinstance(exc, json.JSONDecodeError):
        raise MCPRegistryDescriptorInvalid(
            f"catalog server at {url} returned a response that is not valid JSON: {exc}"
        ) from exc

    # Fallback: re-raise unknown exceptions unchanged.
    raise exc


# ──────────────────────────────────────────────────────────────────────────────
# HTTPMCPServerRegistryBackend


class HTTPMCPServerRegistryBackend:
    """HTTP-catalog implementation of ``MCPServerRegistryBackend`` (spec/36).

    Implements the full MCPServerRegistryBackend Protocol against a
    JSON-over-HTTPS catalog server conforming to spec/36 Decision 4's
    three-tier wire contract. Supports: ``list_mcp_servers``,
    ``load_mcp_server``, ``load_all_mcp_servers``, ``validate``,
    ``install``, ``uninstall``, ``capabilities``, ``refresh_capabilities``,
    ``close``.

    Tier negotiation (lazy probe on first non-construction call):
        Step 1: ``GET /capabilities`` -> 200 parses tier from body.
        Step 2: ``GET /capabilities`` -> 404 falls through to OPTIONS.
        Step 3: ``OPTIONS /mcp-servers`` -> parses Allow header for tier.
        Step 4: ``OPTIONS /mcp-servers`` -> 404 or 405, defaults to tier 1.
        Step 5: Any 401/5xx/network error -> ``MCPRegistryUnavailable``
                or ``MCPRegistryAuthRequired``.

    Constructor parameters:

    ``catalog_url``: base URL of the catalog server, e.g.
        ``https://catalog.example.com``. MUST NOT have embedded credentials
        in the stored ``_catalog_url`` attribute (redacted version stored
        in ``_safe_catalog_url``).

    ``agent_scope``: per-agent scope string appended as ``?agent_scope=<scope>``
        to every request. The catalog server filters its response to servers
        mounted for this scope.

    ``auth_token``: optional bearer token. When set, every request includes
        ``Authorization: Bearer <token>``. Catalog servers requiring auth respond
        401 if absent; backend raises ``MCPRegistryAuthRequired``.

    ``request_timeout_s``: per-request HTTP timeout in seconds (default 10.0).
        Distinct from ``probe_failure_cache_s`` (failure cache window).

    ``probe_failure_cache_s``: seconds to cache a probe failure before re-probing
        (default 60.0). Prevents thrashing when the catalog server is unreachable.

    ``allow_http_non_loopback``: when ``True``, allow cleartext ``http://`` for
        non-loopback catalog hosts (default ``False``).  This value is resolved
        ONCE at construction time: if ``None``, reads
        ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_ALLOW_HTTP`` from ``os.environ``
        (``"1"`` → True, anything else → False).  The kwarg wins over the env var
        when provided explicitly (per the env-or-kwarg precedent from
        ``auth_token``).  Scheme validation fires lazily at first method call (not
        at construction), preserving MUST 2.  Production deployments MUST NOT set
        this to ``True`` for remote catalog servers.

    ``max_response_bytes``: hard byte ceiling applied to every catalog HTTP
        response body before JSON parsing (default 8 MB; see
        ``_MAX_CATALOG_RESPONSE_BYTES``). A catalog that streams a body larger
        than this limit raises ``MCPRegistryDescriptorInvalid`` immediately,
        preventing unbounded RSS growth from slow-drip oversized responses
        (CWE-400 / finding #403). A Content-Length check alone is not
        sufficient because the header is omittable and spoofable.

    ``_http_client``: test-only injectable seam (D-PR4-1). When set, the backend
        uses this client instead of constructing a real ``httpx.Client``. Production
        callers MUST NOT pass this parameter.
    """

    def __init__(
        self,
        catalog_url: str,
        agent_scope: str,
        *,
        auth_token: str | None = None,
        request_timeout_s: float = 10.0,
        probe_failure_cache_s: float = 60.0,
        allow_http_non_loopback: bool | None = None,
        max_response_bytes: int = _MAX_CATALOG_RESPONSE_BYTES,
        _http_client: Any = None,
    ) -> None:
        # MUST 2: side-effect-free construction. No httpx import, no network
        # call, no file open. All initialization is pure attribute assignment.
        #
        # Normalize catalog_url ONCE: strip trailing slashes AND any query
        # string the operator may have appended (e.g., "https://host/?foo=bar"
        # or "https://host/api?debug=1"). Without this, every URL-building site
        # would produce malformed paths like
        # "https://host/api?debug=1/mcp-servers?agent_scope=..." which httpx
        # rejects or misroutes (Adversarial F4). The query-string-strip also
        # prevents operator-provided URL params from leaking into the
        # framework's wire format requests. Maintainability M4: single
        # normalization site eliminates the six-site `.rstrip('/')` pattern.
        #
        # URL parse failures (e.g., invalid IPv6 brackets) MUST NOT raise at
        # construction time per MUST 2. Fall back to storing the raw URL; the
        # malformed URL will surface as a ValueError from httpx.InvalidURL at
        # first network use, mapped through ``_handle_http_error``.
        from urllib.parse import urlparse, urlunparse

        try:
            _parsed = urlparse(catalog_url)
            self._catalog_url = urlunparse(
                (_parsed.scheme, _parsed.netloc, _parsed.path.rstrip("/"), "", "", "")
            )
        except (ValueError, TypeError):
            # Defer URL validation to first method call; preserve MUST 2.
            self._catalog_url = catalog_url
        self._safe_catalog_url = _redact_url_for_error(self._catalog_url)
        self._agent_scope = agent_scope
        self._auth_token = auth_token
        self._request_timeout_s = request_timeout_s
        self._probe_failure_cache_s = probe_failure_cache_s
        self._max_response_bytes = max_response_bytes
        self._http_client_override = _http_client

        # Resolve allow_http_non_loopback once at construction (P1: TOCTOU).
        # kwarg wins over env var when provided (env-or-kwarg precedent).
        if allow_http_non_loopback is not None:
            self._allow_http_non_loopback: bool = bool(allow_http_non_loopback)
        else:
            self._allow_http_non_loopback = (
                os.environ.get("ATOMIC_AGENTS_MCP_SERVER_REGISTRY_ALLOW_HTTP") == "1"
            )

        # Capability cache state.
        self._cached_capabilities: MCPServerRegistryCapabilities | None = None
        self._probe_failure_cached_until: float = 0.0

        # Lock guards the capability cache check+write. HTTP probe (slow I/O)
        # runs OUTSIDE the lock per D-PR4-3 (avoid serializing concurrent
        # callers against network latency).
        self._capabilities_lock = threading.Lock()

        # Lock guards lazy httpx.Client construction (race-free on first call).
        self._client_lock = threading.Lock()

        # The lazily-constructed real httpx.Client (None until first use).
        self._real_client: Any = None

    # ─── Backend identity ─────────────────────────────────────────────────

    @property
    def backend_id(self) -> str:
        """Stable identifier matching the registry key (MUST 6a)."""
        return "http"

    # ─── HTTP client management ───────────────────────────────────────────

    def _get_client(self) -> Any:
        """Return the ``httpx.Client`` to use for requests.

        If a test-only override was injected at construction, returns it.
        Otherwise, lazily constructs a real ``httpx.Client`` on first call.
        The construction is guarded by ``_client_lock`` to prevent a race
        when multiple threads call an HTTP method concurrently for the first
        time.
        """
        if self._http_client_override is not None:
            return self._http_client_override

        if self._real_client is not None:
            return self._real_client

        with self._client_lock:
            if self._real_client is not None:
                return self._real_client
            httpx = _get_httpx()
            # Auth headers are applied per-request via ``_auth_headers`` so a
            # test-injected ``_http_client`` (vanilla httpx.Client without the
            # default Authorization header) still carries auth on every call.
            self._real_client = httpx.Client(timeout=self._request_timeout_s)
            return self._real_client

    def _auth_headers(self) -> dict[str, str]:
        """Return per-request auth headers when ``auth_token`` is set.

        Returns ``{"Authorization": "Bearer <token>"}`` when an auth_token is
        configured; otherwise returns an empty dict. Applied on every HTTP
        call rather than as ``httpx.Client`` default headers so that test
        fixtures injecting a vanilla ``_http_client`` still carry auth
        (Stream D D-F2; per spec MUST 4 the token never appears in error
        messages or repr, only in outgoing request headers).
        """
        if self._auth_token:
            return {"Authorization": f"Bearer {self._auth_token}"}
        return {}

    def _read_bounded_json(self, resp: Any) -> Any:
        """Read response body up to ``_max_response_bytes`` and parse as JSON.

        Reads the raw response content from ``resp.content`` (already buffered
        by httpx) and raises ``MCPRegistryDescriptorInvalid`` before JSON
        parsing when the byte count exceeds ``self._max_response_bytes``.

        A Content-Length header check is NOT sufficient because the header is
        optional and can be spoofed by a compromised or MITM'd catalog server.
        This method enforces the ceiling on ACTUAL bytes read, closing the
        slow-drip memory-exhaustion vector (CWE-400 / finding #403).

        Args:
            resp: An httpx Response object.  Must have already received a
                  successful status code; the caller is responsible for calling
                  ``resp.raise_for_status()`` first.

        Returns:
            The JSON-decoded response body.

        Raises:
            MCPRegistryDescriptorInvalid: body exceeds ``_max_response_bytes``
                or the body is not valid JSON.
        """
        url = self._safe_catalog_url
        raw = resp.content  # bytes already buffered by httpx
        if len(raw) > self._max_response_bytes:
            raise MCPRegistryDescriptorInvalid(
                f"catalog server at {url} returned a response body of "
                f"{len(raw):,} bytes, exceeding the {self._max_response_bytes:,}-byte "
                f"limit. Refusing to parse. "
                f"Possible slow-drip memory-exhaustion attack or misconfigured catalog."
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPRegistryDescriptorInvalid(
                f"catalog server at {url} returned a response that is not valid JSON: {exc}"
            ) from exc

    # ─── Capability negotiation ────────────────────────────────────────────

    def _assert_scheme(self) -> None:
        """Re-gate the scheme at resolution time (MUST 11 defense-in-depth).

        Calls ``_assert_scheme_allowed`` with the stored ``_catalog_url`` and
        the resolved ``_allow_http_non_loopback`` flag.  Called at the top of
        EVERY public method that issues network I/O — the read paths
        (``list_mcp_servers``, ``load_mcp_server``, ``load_all_mcp_servers``,
        ``validate``) AND the write paths (``install``, ``uninstall``) — BEFORE
        ``_ensure_probed()`` and before any HTTP call, so no cleartext request
        leaves the process on a non-loopback http:// URL without the explicit
        opt-in even when the capability cache is already warm.

        Re-validates the catalog URL scheme before making the HTTP call
        (defense-in-depth; loopback-auto / non-loopback requires HTTPS or
        explicit opt-in). ValueError on refused schemes uses _redact_url_for_error.
        """
        _assert_scheme_allowed(
            self._catalog_url,
            allow_http_non_loopback=self._allow_http_non_loopback,
        )

    def _probe_capabilities(self) -> MCPServerRegistryCapabilities:
        """Run the tier-negotiation probe sequence (Decision 4 steps 1-5).

        This method performs network I/O. It is called OUTSIDE any lock;
        the caller acquires ``_capabilities_lock`` to update the cache after
        this returns.

        Returns ``MCPServerRegistryCapabilities`` reflecting the catalog
        server's tier. Raises ``MCPRegistryUnavailable`` or
        ``MCPRegistryAuthRequired`` on failure.
        """
        # Re-gate scheme before making any HTTP call (MUST 11 defense-in-depth).
        self._assert_scheme()
        httpx = _get_httpx()
        client = self._get_client()
        url = self._safe_catalog_url
        caps_url = f"{self._catalog_url}/capabilities"

        # Step 1: GET /capabilities (authoritative if 200).
        try:
            resp = client.get(caps_url, headers=self._auth_headers())
            if resp.status_code == 200:
                data = self._read_bounded_json(resp)
                return _parse_capabilities_response(data, url=url)

            # Step 2: 404 on /capabilities means server doesn't implement it;
            # fall through to OPTIONS probe. Other non-200 codes are errors.
            if resp.status_code != 404:
                # B-F8: non-404 4xx and 5xx must NOT silently fall back.
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    _handle_http_error(exc, url=url)

        except (
            httpx.LocalProtocolError,
            httpx.DecodingError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.HTTPError,
            httpx.InvalidURL,
            RuntimeError,
        ) as exc:
            _handle_http_error(exc, url=url)

        # Step 3: OPTIONS /mcp-servers to infer tier from Allow header.
        servers_url = f"{self._catalog_url}/mcp-servers"
        try:
            resp = client.request("OPTIONS", servers_url, headers=self._auth_headers())

            if resp.status_code in (404, 405):
                # Step 4: fallback to tier 1.
                return MCPServerRegistryCapabilities(
                    supports_install=False,
                    supports_uninstall=False,
                    supports_capability_handshake=True,
                    supports_audit=False,
                    durable=True,
                )

            # B-F8 / Adversarial F3 / Testing T-F1: non-200/non-404/non-405
            # responses on the OPTIONS step must NOT silently fall back to
            # tier 1. A misconfigured token returning 401 or a server-side
            # outage returning 5xx would otherwise produce a silent capability
            # downgrade: a tier-3 server appearing as tier 1 with no audit
            # signal. Mirror the GET /capabilities branch's discipline.
            if resp.status_code == 401:
                raise MCPRegistryAuthRequired(
                    f"catalog server at {url} returned 401 Unauthorized on "
                    f"OPTIONS /mcp-servers. Set "
                    f"ATOMIC_AGENTS_MCP_SERVER_REGISTRY_AUTH_TOKEN or pass "
                    f"auth_token= to the constructor."
                )
            if resp.status_code >= 300 and resp.status_code != 200:
                # 3xx redirects (with follow_redirects=False, httpx returns the
                # redirect response directly; Allow header would be absent and
                # tier inference would silently return tier 1). 4xx other than
                # 401/404/405. 5xx server errors. All must surface.
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    _handle_http_error(exc, url=url)
                # Defensive: if status >= 300 but raise_for_status doesn't
                # raise (e.g., 3xx is treated as success by httpx in some
                # configurations), still refuse the tier inference.
                raise MCPRegistryUnavailable(
                    f"catalog server at {url} returned unexpected status "
                    f"{resp.status_code} on OPTIONS /mcp-servers; cannot "
                    f"infer tier."
                )

            # Parse Allow header per B-F6 (set-membership, not string equality).
            allow_header = resp.headers.get("allow", "")
            allowed = {m.strip().upper() for m in allow_header.split(",") if m.strip()}

            # Tier 2 if both POST and DELETE are allowed.
            if {"GET", "POST", "DELETE"}.issubset(allowed):
                supports_install = True
                supports_uninstall = True
            else:
                supports_install = False
                supports_uninstall = False

            return MCPServerRegistryCapabilities(
                supports_install=supports_install,
                supports_uninstall=supports_uninstall,
                supports_capability_handshake=True,
                supports_audit=False,
                durable=True,
            )

        except (
            httpx.LocalProtocolError,
            httpx.DecodingError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.HTTPError,
            httpx.InvalidURL,
            RuntimeError,
        ) as exc:
            _handle_http_error(exc, url=url)

        # Should be unreachable; satisfy the type checker.
        raise MCPRegistryUnavailable(  # pragma: no cover
            f"capability probe for catalog at {url} ended unexpectedly"
        )

    def _ensure_probed(self) -> None:
        """Ensure the capability cache is populated, probing if necessary.

        Thread-safe: acquires ``_capabilities_lock`` to check the cache and
        to write after a successful probe. The probe itself runs outside the
        lock (per D-PR4-3) so concurrent callers don't serialize against
        network latency.

        **Concurrent first-call contention.** Two threads that both observe
        ``_cached_capabilities is None`` may both run ``_probe_capabilities``
        in parallel and each make one HTTP round trip. This is intentional:
        serializing first-callers behind the lock would tie every concurrent
        request to the latency of one probe. Last writer wins; the cache
        stabilizes after one successful probe lands. The trade-off accepts
        a small burst of N probes at startup in exchange for never blocking
        on network I/O while holding the lock.

        After a probe failure, caches the failure for ``probe_failure_cache_s``
        seconds. Explicit ``refresh_capabilities()`` always bypasses the cache.
        """
        with self._capabilities_lock:
            if self._cached_capabilities is not None:
                return
            now = time.monotonic()
            if now < self._probe_failure_cached_until:
                raise MCPRegistryUnavailable(
                    f"catalog server at {self._safe_catalog_url} probe failed; "
                    f"failure cached for {self._probe_failure_cache_s:.0f}s. "
                    f"Call refresh_capabilities() to retry immediately."
                )
        # Probe runs outside the lock (D-PR4-3).
        try:
            new_caps = self._probe_capabilities()
        except (MCPRegistryUnavailable, MCPRegistryAuthRequired):
            with self._capabilities_lock:
                self._probe_failure_cached_until = (
                    time.monotonic() + self._probe_failure_cache_s
                )
            raise

        with self._capabilities_lock:
            self._cached_capabilities = new_caps
            self._probe_failure_cached_until = 0.0

    # ─── Capability advertisement ─────────────────────────────────────────

    @property
    def capabilities(self) -> MCPServerRegistryCapabilities:
        """Return the current capability view.

        Before the first successful probe, returns a conservative default
        (all write/audit capabilities False) per B-F11. This allows callers
        to introspect before the first real method call without triggering a
        network probe. The probe fires lazily on the first ``list``,
        ``load``, ``load_all``, or ``validate`` call.

        After a successful probe, returns the runtime-negotiated view
        reflecting the catalog server's actual tier.
        """
        with self._capabilities_lock:
            if self._cached_capabilities is not None:
                return self._cached_capabilities
        # Conservative pre-probe default per B-F11.
        return MCPServerRegistryCapabilities(
            supports_install=False,
            supports_uninstall=False,
            supports_capability_handshake=True,
            supports_audit=False,
            durable=True,
        )

    def refresh_capabilities(self) -> MCPServerRegistryCapabilities:
        """Re-probe the catalog server and update the capability cache.

        Bypasses the failure cache. Operators call this after upgrading a
        catalog server to a higher tier or after a transient outage clears.

        Per B-F5: the new capability construction runs outside the lock; the
        cache assignment happens inside.
        """
        new_caps = self._probe_capabilities()
        with self._capabilities_lock:
            self._cached_capabilities = new_caps
            self._probe_failure_cached_until = 0.0
        return new_caps

    # ─── Core discovery ───────────────────────────────────────────────────

    def list_mcp_servers(self) -> list[MCPServerRef]:
        """Return lightweight server refs for this agent scope.

        Calls ``GET /mcp-servers?agent_scope=<scope>``. The catalog server
        MUST filter to the mounted subset for this scope server-side
        (spec/36 MUST 5; returning the org-wide catalog is non-conformant).

        Returns lexicographically sorted list (MUST 5).
        """
        # Re-gate scheme before any I/O (MUST 11 defense-in-depth).
        self._assert_scheme()
        self._ensure_probed()
        httpx = _get_httpx()
        client = self._get_client()
        url = self._safe_catalog_url

        query = urlencode({"agent_scope": self._agent_scope})
        request_url = f"{self._catalog_url}/mcp-servers?{query}"

        try:
            resp = client.get(request_url, headers=self._auth_headers())
            resp.raise_for_status()
            data = self._read_bounded_json(resp)
        except httpx.HTTPStatusError as exc:
            _handle_http_error(exc, url=url)
        except (
            httpx.LocalProtocolError,
            httpx.DecodingError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.HTTPError,
            httpx.InvalidURL,
            RuntimeError,
        ) as exc:
            _handle_http_error(exc, url=url)

        refs = _parse_servers_list_to_refs(data, url=url, source_url=self._catalog_url)
        return sorted(refs, key=lambda r: r.name)

    def load_mcp_server(self, name: str) -> MCPServerSpec:
        """Return the fully-populated ``MCPServerSpec`` for the named server.

        Validates ``name`` charset BEFORE any network call (MUST 1).
        Raises ``MCPServerNotInRegistry`` on HTTP 404.
        Resolves ``$VAR`` env-var references at call time (MUST 8).
        Re-validates the catalog URL scheme before making the HTTP call
        (defense-in-depth; loopback-auto / non-loopback requires HTTPS or
        explicit opt-in). ValueError on refused schemes uses _redact_url_for_error.
        """
        _validate_server_name(name)
        # Re-gate scheme at resolution time (MUST 11 defense-in-depth).
        # Fires before _ensure_probed() so no network I/O occurs on a refused scheme.
        self._assert_scheme()
        self._ensure_probed()
        httpx = _get_httpx()
        client = self._get_client()
        url = self._safe_catalog_url

        query = urlencode({"agent_scope": self._agent_scope})
        request_url = f"{self._catalog_url}/mcp-servers/{name}?{query}"

        try:
            resp = client.get(request_url, headers=self._auth_headers())
            resp.raise_for_status()
            data = self._read_bounded_json(resp)
        except httpx.HTTPStatusError as exc:
            _handle_http_error(
                exc,
                url=url,
                expect_404_means_not_found_for_name=name,
            )
        except (
            httpx.LocalProtocolError,
            httpx.DecodingError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.HTTPError,
            httpx.InvalidURL,
            RuntimeError,
        ) as exc:
            _handle_http_error(exc, url=url)

        raw = _parse_mcp_server_spec_from_dict(data, url=url)
        return _materialize_spec(raw, url)

    def load_all_mcp_servers(self) -> list[MCPServerSpec]:
        """Return all mounted MCPServerSpec instances in bulk.

        Uses the ``?expand=spec`` bulk endpoint (single round trip; eliminates
        N+1 HTTP cost at agent construction). Routes every entry through
        ``_materialize_spec`` to apply env-var resolution, guaranteeing MUST 10
        structural equivalence with repeated ``load_mcp_server`` calls (C-F4).

        Returns lexicographically sorted list (MUST 5).
        """
        # Re-gate scheme before any I/O (MUST 11 defense-in-depth).
        self._assert_scheme()
        self._ensure_probed()
        httpx = _get_httpx()
        client = self._get_client()
        url = self._safe_catalog_url

        query = urlencode({"agent_scope": self._agent_scope, "expand": "spec"})
        request_url = f"{self._catalog_url}/mcp-servers?{query}"

        try:
            resp = client.get(request_url, headers=self._auth_headers())
            resp.raise_for_status()
            data = self._read_bounded_json(resp)
        except httpx.HTTPStatusError as exc:
            _handle_http_error(exc, url=url)
        except (
            httpx.LocalProtocolError,
            httpx.DecodingError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.HTTPError,
            httpx.InvalidURL,
            RuntimeError,
        ) as exc:
            _handle_http_error(exc, url=url)

        raw_specs = _parse_servers_list(data, url=url)
        materialized = [_materialize_spec(s, url) for s in raw_specs]
        return sorted(materialized, key=lambda s: s.name)

    def validate(self, name: str) -> ValidationResult:
        """Static check of the named server descriptor via the catalog server.

        Validates ``name`` charset BEFORE any network call (MUST 1).

        If the catalog server returns 404 on ``/validate`` (tier-1 servers
        may not implement this endpoint), returns a
        ``ValidationResult(ok=False, ...)`` that names the limitation rather
        than raising an exception.
        """
        try:
            _validate_server_name(name)
        except ValueError as exc:
            return ValidationResult(ok=False, errors=[str(exc)], warnings=[])

        # Re-gate scheme before any I/O (MUST 11 defense-in-depth).
        self._assert_scheme()
        self._ensure_probed()
        httpx = _get_httpx()
        client = self._get_client()
        url = self._safe_catalog_url

        query = urlencode({"agent_scope": self._agent_scope})
        request_url = f"{self._catalog_url}/mcp-servers/{name}/validate?{query}"

        try:
            resp = client.get(request_url, headers=self._auth_headers())
            if resp.status_code == 404:
                # A-F2: 404 on /validate is ambiguous. The spec marks /validate
                # OPTIONAL across all tiers, so the catalog may not implement
                # it, OR the server name may simply be absent from the catalog.
                # Honest message rather than asserting one interpretation.
                return ValidationResult(
                    ok=False,
                    errors=[
                        f"catalog server at {url} returned 404 for "
                        f"/mcp-servers/{name}/validate; either {name!r} is not "
                        f"in the catalog or the server does not implement "
                        f"/validate (the endpoint is OPTIONAL per spec/36)."
                    ],
                    warnings=[],
                )
            resp.raise_for_status()
            data = self._read_bounded_json(resp)
        except httpx.HTTPStatusError as exc:
            _handle_http_error(exc, url=url)
        except (
            httpx.LocalProtocolError,
            httpx.DecodingError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.HTTPError,
            httpx.InvalidURL,
            RuntimeError,
        ) as exc:
            _handle_http_error(exc, url=url)

        return _parse_validation_result(data, url=url)

    # ─── Capability-gated write paths (PR 5) ─────────────────────────────

    def _handle_tier_regression(
        self, operation: Literal["install", "uninstall"]
    ) -> NoReturn:
        """Handle a mid-session tier regression (405 on POST or DELETE).

        Called when a previously-tier-2 catalog server returns 405 on a
        write operation, indicating it has regressed to tier 1 (read-only).

        Steps:
        1. Re-probe the catalog server's capabilities (outside any lock, per
           D-PR4-3 thundering-herd discipline).
        2. On re-probe failure: re-raise as MCPRegistryUnavailable with a
           "capability cache may be stale" message (D-PR5-4).
        3. If re-probe still returns tier 2 despite the 405: raise
           NotImplementedError with an "inconsistent server" message (spec/36
           edge case clarification, pre-dispatch correction #6).
        4. Otherwise: raise NotImplementedError naming the tier transition and
           the operation (D-PR5-3).

        Concurrent thundering-herd 405 behavior (B-F5): multiple concurrent
        callers may each observe a 405 on the same operation after a
        regression. Each triggers this helper independently. Each re-probe is
        a separate network round trip (probes run outside the lock). Each
        caller raises NotImplementedError independently. Last-writer-wins on
        the cache update inside _capabilities_lock; all callers converge to
        the correct tier after the first re-probe lands.

        Fail-late carve-out (B-F9): this helper raises NotImplementedError
        DYNAMICALLY after a 405, even when capabilities.supports_install was
        True at method-call-entry. This is compatible with MUST 3 because the
        capability was True at introspection time. The dynamic downgrade is a
        mid-session server state change, not a capability lie.

        MUST: ALL operator-facing messages use self._safe_catalog_url, NEVER
        self._catalog_url (MUST 4 URL credential redaction).
        """
        url = self._safe_catalog_url
        try:
            new_caps = self.refresh_capabilities()
        except (
            MCPRegistryUnavailable,
            MCPRegistryAuthRequired,
            MCPRegistryDescriptorInvalid,
        ) as original_exc:
            raise MCPRegistryUnavailable(
                f"catalog server at {url} returned 405 on {operation} "
                f"and re-probe failed: {original_exc}. "
                f"Capability cache may be stale."
            ) from original_exc

        # Check the relevant flag after re-probe.
        still_supports = (
            new_caps.supports_install
            if operation == "install"
            else new_caps.supports_uninstall
        )

        if still_supports:
            # Contradictory: server claims tier 2 but returned 405. Fail loud.
            raise NotImplementedError(
                f"catalog server at {url} returned 405 on {operation} "
                f"but re-probe still reports {operation} supported (tier 2). "
                f"Inconsistent catalog server state; operator investigation required. "
                f"Do NOT retry; investigate the catalog server."
            )

        # Normal regression: server is now tier 1.
        raise NotImplementedError(
            f"catalog server at {url} previously reported tier 2 "
            f"({operation} supported) but is now reporting tier 1 (read-only). "
            f"The {operation} capability cache has been refreshed; "
            f"{operation} is no longer available on this catalog. "
            f"Operator action required."
        )

    def install(self, spec: MCPServerSpec) -> MCPServerRef:
        """Install a new MCP server into the catalog via HTTP POST.

        Requires a tier-2+ catalog server (``capabilities.supports_install``
        must be True after probing). The capability gate fires after
        ``_ensure_probed()``; the pre-probe conservative default is False so
        the order is: probe first, THEN check the gate (D-PR5-1).

        install() requires MCPServerSpec.env to contain ONLY unresolved
        ``$VAR`` references. Literal values are rejected with ``ValueError``.
        This prevents accidentally exfiltrating secrets via
        ``load_mcp_server -> install`` pipelines.

        Args:
            spec: MCPServerSpec to install. MUST contain unresolved ``$VAR``
                env references (the "as typed by the operator" form, e.g.
                ``env={'API_KEY': '$YOUR_API_KEY_ENV_VAR'}``). If you loaded
                the spec from ``load_mcp_server()``, the env values are already
                resolved to literal strings; passing such a spec to install()
                raises ``ValueError`` at the API boundary to prevent the
                resolved secrets from reaching the catalog server's request
                body. Pass a spec with raw ``$VAR`` references instead (D-PR5-5
                upgraded from warn to refuse at v1.0 Decision A).

        Returns:
            MCPServerRef projected from the input spec (name, description,
            transport). The 201 response body is informational only and is NOT
            parsed for the Ref (D-PR5-6). The returned
            ``MCPServerRef.source`` field uses the raw catalog URL (not
            credential-redacted) so the Ref is usable as a navigation URL per
            spec/36 line 228. Operators logging or persisting the Ref MUST
            redact ``source`` before output to avoid leaking embedded
            credentials.

        Raises:
            ValueError: env contains literal values (likely resolved secrets);
                callers MUST pass unresolved $VAR refs. Also raised for invalid
                spec.name charset.
            NotImplementedError: catalog server is tier 1 (does not support
                install) either statically or after a mid-session tier
                regression (405 + re-probe).
            MCPServerAlreadyInstalled: HTTP 409 from catalog server (name
                collision for this scope).
            MCPRegistryUnavailable: network error, server 5xx, or re-probe
                failure during tier regression recovery.
            MCPRegistryAuthRequired: HTTP 401 (token missing or invalid).
        """
        # MUST 1: charset validation before any I/O.
        _validate_server_name(spec.name)

        # MUST 11 scheme gate — re-gate BEFORE _ensure_probed() so a refused
        # scheme blocks even when the capability cache is already warm (a prior
        # read on a loopback/https URL, or any future code path that warms the
        # cache without re-probing, would otherwise let the POST proceed
        # un-gated). install() round-trips with the catalog, so the same MITM
        # threat that motivates MUST 11 on the read paths applies here.
        self._assert_scheme()

        # D-PR5-5 (v1.0 Decision A: upgraded from warn to refuse).
        # Input validation: reject literal env values BEFORE any I/O.
        # This is the earliest possible gate -- before _ensure_probed(), before
        # the capability check, before any network call.
        for key, value in spec.env.items():
            if value and not value.startswith("$"):
                raise ValueError(
                    f"install() requires unresolved $VAR references in MCPServerSpec.env, "
                    f"but env value for {key!r} on server {spec.name!r} is the literal {value!r}. "
                    f"If you loaded this spec from load_mcp_server(), env values are resolved "
                    f"client-side per spec/36 Decision 7. Pass a spec with raw $VAR refs "
                    f"(e.g., env={{'API_KEY': '$YOUR_API_KEY_ENV_VAR'}}) so the catalog server "
                    f"never sees real secret values. See spec/36 §'Install / uninstall semantics (HTTP)'."
                )

        # D-PR5-1: probe first, THEN check capability gate.
        self._ensure_probed()

        # Capability gate (D-PR5-1, D-PR5-8).
        # NOTE: do NOT modify the pre-probe conservative default (lines 833-839)
        # or the tier-1 fallback constants (lines 692-700). The "flag flip"
        # applies only to the runtime view after successful tier-2+ negotiation.
        if not self.capabilities.supports_install:
            raise NotImplementedError(
                f"catalog server at {self._safe_catalog_url} does not support "
                f"install (tier 1 read-only catalog). "
                f"Use a tier-2+ catalog or the filesystem backend."
            )

        httpx = _get_httpx()
        client = self._get_client()
        url = self._safe_catalog_url

        query = urlencode({"agent_scope": self._agent_scope})
        request_url = f"{self._catalog_url}/mcp-servers?{query}"

        # Note on exception ordering: NotImplementedError is a subclass of
        # RuntimeError in Python. Tier-regression handling raises
        # NotImplementedError; calling it from inside a try/except that catches
        # RuntimeError would silently swallow the NotImplementedError into the
        # MCPRegistryUnavailable path. The 405 check and regression handler are
        # therefore outside the httpx exception block.
        try:
            resp = client.post(
                request_url,
                json=spec.to_dict(),
                headers=self._auth_headers(),
            )
        except (
            httpx.LocalProtocolError,
            httpx.DecodingError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.HTTPError,
            httpx.InvalidURL,
            RuntimeError,
        ) as exc:
            _handle_http_error(exc, url=url)

        # 405 check OUTSIDE the httpx except block to prevent NotImplementedError
        # from being re-caught by the RuntimeError branch above.
        if resp.status_code == 405:
            # Mid-session tier regression: server was tier 2 but returned 405.
            self._handle_tier_regression("install")  # raises NoReturn

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _handle_http_error(
                exc,
                url=url,
                expect_409_means_collision=True,
                installed_server_name=spec.name,
            )

        # Fix 9 (P2): reject non-201 2xx success codes (202/200/203 etc. are
        # not the wire contract for install; raise rather than silently succeed).
        if resp.status_code != 201:
            raise MCPRegistryUnavailable(
                f"catalog server at {url} returned unexpected HTTP "
                f"{resp.status_code} on install (expected 201)."
            )

        # D-PR5-6: project MCPServerRef from input spec (NOT from 201 body).
        # The 201 response body is informational; parsing it would create a
        # defense-in-depth gap when a malformed body causes KeyError/TypeError.
        # client CONSTRUCTS the Ref; it does not parse it from the server.
        description_first_line = (
            spec.description.splitlines()[0].strip() if spec.description else ""
        )
        return MCPServerRef(
            name=spec.name,
            description=description_first_line,
            transport=spec.transport,
            version=None,
            source=f"{self._catalog_url}/mcp-servers/{spec.name}",
        )

    def uninstall(self, name: str) -> None:
        """Remove an MCP server from the catalog via HTTP DELETE. Idempotent.

        The catalog server returns 204 whether the name exists or not (per
        MUST 9 idempotency). No special handling for absent names; 204 is 204.

        Args:
            name: MCP server name to uninstall.

        Returns:
            None on both the present-and-removed path AND the absent-no-op path.

        Raises:
            ValueError: invalid name charset.
            NotImplementedError: catalog server is tier 1 (does not support
                uninstall) either statically or after a mid-session tier
                regression (405 + re-probe).
            MCPRegistryUnavailable: network error, server 5xx, or re-probe
                failure during tier regression recovery.
            MCPRegistryAuthRequired: HTTP 401 (token missing or invalid).
        """
        # MUST 1: charset validation before any I/O.
        _validate_server_name(name)

        # MUST 11 scheme gate — re-gate BEFORE _ensure_probed() so a refused
        # scheme blocks even when the capability cache is already warm. Matches
        # the install()/read-path pattern; uninstall() round-trips with the
        # catalog (DELETE), so the scheme gate applies here too.
        self._assert_scheme()

        # D-PR5-1: probe first, THEN check capability gate.
        self._ensure_probed()

        # Capability gate (D-PR5-1, D-PR5-8).
        if not self.capabilities.supports_uninstall:
            raise NotImplementedError(
                f"catalog server at {self._safe_catalog_url} does not support "
                f"uninstall (tier 1 read-only catalog). "
                f"Use a tier-2+ catalog or the filesystem backend."
            )

        httpx = _get_httpx()
        client = self._get_client()
        url = self._safe_catalog_url

        query = urlencode({"agent_scope": self._agent_scope})
        request_url = f"{self._catalog_url}/mcp-servers/{name}?{query}"

        # Note on exception ordering: NotImplementedError is a subclass of
        # RuntimeError in Python. Tier-regression handling raises
        # NotImplementedError; calling it from inside a try/except that catches
        # RuntimeError would silently swallow the NotImplementedError into the
        # MCPRegistryUnavailable path. The 405 check and regression handler are
        # therefore outside the httpx exception block.
        try:
            resp = client.delete(
                request_url,
                headers=self._auth_headers(),
            )
        except (
            httpx.LocalProtocolError,
            httpx.DecodingError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.HTTPError,
            httpx.InvalidURL,
            RuntimeError,
        ) as exc:
            _handle_http_error(exc, url=url)

        # 405 check OUTSIDE the httpx except block to prevent NotImplementedError
        # from being re-caught by the RuntimeError branch above.
        if resp.status_code == 405:
            # Mid-session tier regression: server was tier 2 but returned 405.
            self._handle_tier_regression("uninstall")  # raises NoReturn

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _handle_http_error(exc, url=url)

        # Fix 9 (P2): reject non-204 2xx success codes (202/200/203 etc. are
        # not the wire contract for uninstall; raise rather than silently succeed).
        if resp.status_code != 204:
            raise MCPRegistryUnavailable(
                f"catalog server at {url} returned unexpected HTTP "
                f"{resp.status_code} on uninstall (expected 204)."
            )

        # D-PR5-7: 204 response has no body; do not call resp.json().
        # resp.raise_for_status() is a no-op for 204.
        return None

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying ``httpx.Client`` and release its resources.

        Idempotent (MUST 6b): calling ``close()`` twice does not raise.
        Does NOT close the injected ``_http_client_override`` (the test caller
        owns its lifecycle).
        """
        if self._http_client_override is not None:
            # Test-injected client; caller manages lifecycle.
            return
        with self._client_lock:
            if self._real_client is not None:
                try:
                    self._real_client.close()
                except Exception:
                    _logger.debug(
                        "HTTPMCPServerRegistryBackend.close(): error closing "
                        "httpx.Client (ignored)",
                        exc_info=True,
                    )
                self._real_client = None


# ──────────────────────────────────────────────────────────────────────────────
# Factory function


def make_http_mcp_server_registry_backend_from_url(
    url: str,
) -> HTTPMCPServerRegistryBackend:
    """Construct an ``HTTPMCPServerRegistryBackend`` from a catalog URL.

    URL family: ``https://<host>[:port]/?agent_scope=<name>``
    (or ``http://`` for loopback hosts — see scheme gate below).

    Reads ``agent_scope`` from the URL query parameter ``agent_scope``
    (default ``"default"`` when absent). Reads the optional bearer token
    from ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_AUTH_TOKEN`` in the environment.
    Reads ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_ALLOW_HTTP`` (``"1"`` → True,
    anything else → False) to unlock non-loopback http:// for dev scenarios.

    Scheme gate (MUST 11):
    - ``https://`` → always allowed.
    - ``http://`` with loopback host (127.0.0.0/8, ::1, literal "localhost")
      → automatically allowed (LOOPBACK-AUTO; no opt-in needed).
    - ``http://`` with non-loopback host without opt-in → ``ValueError`` naming
      ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_ALLOW_HTTP``.
    - Any other scheme (ftp, file, …) → ``ValueError``.

    Args:
        url: Catalog server base URL. The ``agent_scope`` query parameter, if
             present, is extracted and stripped from the base URL before passing
             to the constructor.

    Returns:
        Configured ``HTTPMCPServerRegistryBackend`` instance.

    Raises:
        ValueError: if the URL is empty, has an unsupported scheme, or is a
                    non-loopback http:// URL without
                    ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_ALLOW_HTTP=1``.
    """
    from urllib.parse import urlparse, urlunparse, parse_qs

    if not url or not url.strip():
        raise ValueError("HTTPMCPServerRegistryBackend catalog URL must not be empty.")

    # Resolve the env opt-in once so factory-constructed backends inherit
    # the same behavior as programmatically-constructed ones.
    allow_http_non_loopback = (
        os.environ.get("ATOMIC_AGENTS_MCP_SERVER_REGISTRY_ALLOW_HTTP") == "1"
    )

    # Apply the scheme gate early in the factory so errors surface at
    # construction time for factory users (programmatic callers get the gate
    # again at first method call via _assert_scheme, providing defense-in-depth).
    _assert_scheme_allowed(url, allow_http_non_loopback=allow_http_non_loopback)

    parsed = urlparse(url)

    # Extract agent_scope from query params.
    query_params = parse_qs(parsed.query, keep_blank_values=False)
    agent_scope_values = query_params.pop("agent_scope", None)
    agent_scope = agent_scope_values[0] if agent_scope_values else "default"

    # Rebuild the base URL without the agent_scope query param.
    remaining_query = urlencode(
        {k: v[0] for k, v in query_params.items()},
        doseq=False,
    )
    catalog_url = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            remaining_query,
            "",  # strip fragment
        )
    )

    auth_token = os.environ.get("ATOMIC_AGENTS_MCP_SERVER_REGISTRY_AUTH_TOKEN") or None

    return HTTPMCPServerRegistryBackend(
        catalog_url=catalog_url,
        agent_scope=agent_scope,
        auth_token=auth_token,
        allow_http_non_loopback=allow_http_non_loopback,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Self-registration at import time.
# Mirrors ``FilesystemMCPServerRegistryBackend`` registration in ``__init__.py``.

from . import register_mcp_server_registry_backend  # noqa: E402

register_mcp_server_registry_backend("http", HTTPMCPServerRegistryBackend)
