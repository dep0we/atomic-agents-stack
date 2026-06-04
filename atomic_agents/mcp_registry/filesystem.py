"""FilesystemMCPServerRegistryBackend -- mcp.md-backed reference implementation.

Wraps the existing ``parse_mcp_md_text()`` semantics from ``atomic_agents/mcp.py``
behind the ``MCPServerRegistryBackend`` Protocol contract (spec/36). Each agent
has its own ``<agent_root>/mcp.md`` file; the backend is scoped per-agent via
``agent_root``.

Implements the full MCPServerRegistryBackend Protocol including atomic
install/uninstall via LockBackend (spec/36 MUST 9). The lock is acquired
before any read-modify-write on mcp.md, and stale tempfiles from crashed
prior installs are cleaned up inside the lock before reading.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .._io import atomic_write, cleanup_stale_tempfiles_for_file
from ..exceptions import LockBusy, LockLost
from ..locks import check_lock_lost, get_default_lock_backend
from ..mcp import (
    MCPServerSpec,
    _resolve_env_vars,
    parse_mcp_md_text,
    render_mcp_md_full,
    validate_mcp_server_args,
)
from .backend import (
    MCPRegistryDescriptorInvalid,
    MCPRegistryUnavailable,
    MCPServerAlreadyInstalled,
    MCPServerNotInRegistry,
)
from .types import MCPServerRef, MCPServerRegistryCapabilities, ValidationResult

if TYPE_CHECKING:
    from ..locks.backend import LockBackend

_logger = logging.getLogger(__name__)

# Charset rule from MUST 1. Matches CorpusBackend, PersonaBackend, PolicyBackend.
# Path-traversal tokens (``..``, ``/``, ``\\``), control chars, newlines,
# leading dots, and empty strings all fail this check.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_.+@-]+$")

# Additional explicit refusal of path-traversal tokens that might still
# technically match the charset (e.g., a name that is purely dots).
_PATH_TRAVERSAL_TOKENS = frozenset({"..", "."})


def _validate_server_name(name: str) -> None:
    """Raise ``ValueError`` when ``name`` fails the MUST 1 charset rule.

    Checks at the API boundary BEFORE any backend access (spec/36 MUST 1).
    Validation logic mirrors ``CorpusBackend`` and ``PersonaBackend`` per
    prep-pass finding C-F2.

    Rejects: empty string, leading dot, path-traversal tokens (.., /, \\),
    control characters, newlines, and any character outside [a-zA-Z0-9_.+@-].
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


class FilesystemMCPServerRegistryBackend:
    """``mcp.md``-backed implementation of ``MCPServerRegistryBackend`` (spec/36).

    Reads and writes ``<agent_root>/mcp.md`` for server declarations. Supports
    the full MCPServerRegistryBackend Protocol: list, load, load_all, validate,
    install, uninstall, capabilities, refresh_capabilities, close.

    Install and uninstall use LockBackend lease acquisition around every
    read-modify-write on mcp.md (spec/36 MUST 9). Stale tempfiles from
    prior crashed installs accumulate until the next install or uninstall;
    cleanup is performed inside the lock before reading mcp.md.

    Constructor parameters:

    ``agent_root``: the agent's directory. ``mcp.md`` lives at
        ``<agent_root>/mcp.md``. MAY not exist at construction -- MUST 2
        (side-effect-free construction): no file is opened here.

    ``read_paths``: list of ``Path`` objects the agent declares it may read
        (from ``tools.md``). Used by ``load_mcp_server(name)`` to apply the
        path-traversal check (Decision 8 of spec/36). Captured at construction;
        NOT applied at ``list_mcp_servers()`` time (list is metadata-only).

    ``lock_backend``: the LockBackend to use for install/uninstall atomicity.
        When None (the default), the backend is resolved lazily at first use
        via ``get_default_lock_backend(agent_root)``, which honors the
        ``ATOMIC_AGENTS_LOCK_BACKEND`` env var. Operators passing a custom
        ``lock_backend`` MUST scope it to a registry-specific resource (e.g.,
        ``.mcp_registry.lock``), NOT the agent's main ``.lock`` file.

    ``install_lock_timeout``: seconds to wait for the mcp_registry lock before
        raising MCPRegistryUnavailable. Defaults to 30.0.
    """

    def __init__(
        self,
        agent_root: Path,
        read_paths: list,
        *,
        lock_backend: "LockBackend | None" = None,
        install_lock_timeout: float = 30.0,
    ) -> None:
        # MUST 2: side-effect-free construction. No file opens, no subprocess
        # spawns, no network calls here.
        self._agent_root = agent_root
        self._read_paths = read_paths
        self._lock_backend = lock_backend  # may be None; resolved lazily at first use
        self._install_lock_timeout = install_lock_timeout

    # ─── Capability advertisement ─────────────────────────────────────────

    @property
    def capabilities(self) -> MCPServerRegistryCapabilities:
        """Static capability advertisement. Constant for the lifetime of this instance.

        ``supports_install`` and ``supports_uninstall`` flip to True at PR 3
        when the install/uninstall methods land (per spec/36 Decision 5 +
        MUST 3 capability honesty). The conformance suite asserts that
        True-reported capabilities do NOT raise ``NotImplementedError``.
        """
        return MCPServerRegistryCapabilities(
            supports_install=True,
            supports_uninstall=True,
            supports_capability_handshake=False,
            supports_audit=False,
            durable=True,
        )

    # ─── Backend identity ─────────────────────────────────────────────────

    @property
    def backend_id(self) -> str:
        """Stable identifier matching the registry key (MUST 6a)."""
        return "filesystem"

    # ─── Core discovery ───────────────────────────────────────────────────

    def list_mcp_servers(self) -> list[MCPServerRef]:
        """Return lightweight server refs from ``<agent_root>/mcp.md``.

        Returns ``[]`` when ``mcp.md`` is absent or empty (MUST 7 -- absent
        file is not an error). Lexicographic order by ``name`` (spec/36 §list
        semantics).

        CRITICAL: calls ``parse_mcp_md_text(content, resolve_env=False,
        read_paths=None)``. The ``read_paths=None`` is intentional per
        prep-pass Theme 3: passing ``self._read_paths`` here would trigger
        path-traversal validation at list time, violating Decision 8's
        "validation at load_mcp_server boundary" invariant. The
        ``resolve_env=False`` keeps ``$VAR`` references raw (they will be
        resolved at ``load_mcp_server`` time per MUST 8 / Decision 7).
        """
        mcp_md = self._agent_root / "mcp.md"
        if not mcp_md.exists():
            return []

        try:
            content = mcp_md.read_text(encoding="utf-8")
        except FileNotFoundError:
            # ENOENT: race between exists() check above and read; treat as absent.
            return []
        except OSError as exc:
            # Non-ENOENT (PermissionError, IsADirectoryError, etc.): transient
            # configuration error. Mirror load_mcp_server's MCPRegistryUnavailable
            # path (filesystem.py lines for load_mcp_server) for symmetry and to
            # surface the failure to PR 2's fail-closed wiring in agent.py:__init__.
            raise MCPRegistryUnavailable(f"cannot read {mcp_md}: {exc}") from exc

        try:
            specs = parse_mcp_md_text(
                content,
                mcp_md_path=mcp_md,
                read_paths=None,
                resolve_env=False,
            )
        except Exception as exc:
            _logger.warning(
                "FilesystemMCPServerRegistryBackend: mcp.md parse error: %s",
                exc,
            )
            return []

        refs = []
        for spec in specs:
            # P2 #1: Validate section names; skip and warn on invalid names to
            # prevent load_all_mcp_servers from raising uncaught ValueError on
            # a tampered or manually-edited mcp.md.
            try:
                _validate_server_name(spec.name)
            except ValueError:
                _logger.warning(
                    "FilesystemMCPServerRegistryBackend: skipping malformed section "
                    "name %r in mcp.md (failed charset validation)",
                    spec.name,
                )
                continue
            refs.append(
                MCPServerRef(
                    name=spec.name,
                    description=spec.description,
                    transport=spec.transport,
                    version=None,
                    source=f"mcp.md#section:{spec.name}",
                )
            )
        return sorted(refs, key=lambda r: r.name)

    def load_mcp_server(self, name: str) -> MCPServerSpec:
        """Return the fully-populated ``MCPServerSpec`` for the named server.

        Validates ``name`` charset at the API boundary BEFORE any disk access
        (MUST 1). Raises ``MCPServerNotInRegistry`` when the name is absent.
        Resolves ``$VAR`` env-var references at call time (MUST 8 / Decision 7).
        Applies path-traversal validation via ``validate_mcp_server_args``
        AFTER materialization (Decision 8 -- validation at this boundary).

        Implementation steps:
        1. Validate name charset -- raise ``ValueError`` on invalid.
        2. Re-parse ``mcp.md`` with ``resolve_env=False`` to get raw specs.
        3. Find the spec matching ``name`` -- raise ``MCPServerNotInRegistry``
           if absent.
        4. Resolve ``$VAR`` refs via ``_resolve_env_vars`` -- raises
           ``MCPServerConnectFailed`` on unresolvable refs (spec/19 exception).
        5. Apply path-traversal validation via ``validate_mcp_server_args``.
        6. Return the materialized spec.
        """
        _validate_server_name(name)

        mcp_md = self._agent_root / "mcp.md"
        if not mcp_md.exists():
            raise MCPServerNotInRegistry(
                f"MCP server {name!r} not found: mcp.md does not exist at {mcp_md}."
            )

        try:
            content = mcp_md.read_text(encoding="utf-8")
        except OSError as exc:
            # MUST 7: distinguish permanent absence (FileNotFoundError / ENOENT)
            # from transient failures (PermissionError, IsADirectoryError, etc.).
            # FileNotFoundError means the file does not exist -- permanent.
            # All other OSError subclasses indicate a transient configuration
            # problem (wrong permissions, EISDIR) -- raise MCPRegistryUnavailable
            # so callers know to retry rather than treating this as a catalog miss.
            if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
                raise MCPServerNotInRegistry(
                    f"MCP server {name!r} not found: mcp.md does not exist at {mcp_md}."
                ) from exc
            raise MCPRegistryUnavailable(
                f"MCP server {name!r} cannot be loaded: cannot read {mcp_md}: {exc}"
            ) from exc

        try:
            specs = parse_mcp_md_text(
                content,
                mcp_md_path=mcp_md,
                read_paths=None,
                resolve_env=False,
            )
        except Exception as exc:
            raise MCPRegistryDescriptorInvalid(
                f"mcp.md at {mcp_md} could not be parsed: {exc}"
            ) from exc

        # P1 #2: Detect malformed sections (H2 header exists but parse failed).
        # If the name appears in an H2 header but not in the parsed spec list,
        # the section is malformed (e.g., missing command:). Raise a more
        # informative MCPRegistryDescriptorInvalid instead of MCPServerNotInRegistry.
        h2_names = set(re.findall(r"^## (\S+)", content, re.MULTILINE))

        # Find the matching spec by name.
        matched: MCPServerSpec | None = None
        for spec in specs:
            if spec.name == name:
                matched = spec
                break

        if matched is None:
            if name in h2_names:
                raise MCPRegistryDescriptorInvalid(
                    f"MCP server {name!r} has a malformed descriptor in mcp.md "
                    f"(section exists but parse failed; check that 'command:' is present)"
                )
            raise MCPServerNotInRegistry(
                f"MCP server {name!r} not found in {mcp_md}. "
                f"Available: {sorted(s.name for s in specs)}"
            )

        # Resolve $VAR env refs at load time (MUST 8 / Decision 7).
        # _resolve_env_vars raises MCPServerConnectFailed on unresolvable refs,
        # matching the spec/19 error shape exactly.
        resolved_env = _resolve_env_vars(matched.env, name)

        # Reconstruct the spec with resolved env. MCPServerSpec is a plain
        # dataclass (not frozen), so we create a fresh instance.
        materialized = replace(matched, env=resolved_env)

        # Apply path-traversal check AFTER materialization (Decision 8).
        if self._read_paths:
            validate_mcp_server_args(materialized, self._read_paths)

        return materialized

    def load_all_mcp_servers(self) -> list[MCPServerSpec]:
        """Return all mounted ``MCPServerSpec`` instances in bulk.

        Reads mcp.md once and parses, then resolves env vars per spec
        (Decision 7). Distinct from the default ``_default_load_all`` (which
        iterates ``list_mcp_servers`` then calls ``load_mcp_server`` per ref)
        because that pattern masks transient and parse failures:
        ``list_mcp_servers`` used to catch all OSError before the PR 2 fix.
        The single read-parse here maps:
          - ENOENT: empty list (correct -- "no mcp.md is not a probe failure")
          - Other OSError: MCPRegistryUnavailable (transient)
          - Parse error: MCPRegistryDescriptorInvalid (permanent descriptor problem)
          - Env-var unresolvable: MCPServerConnectFailed per spec/19

        PR 2 framework-level fail-closed semantic (spec/36) depends on these
        distinct exceptions surfacing correctly.
        """
        mcp_md = self._agent_root / "mcp.md"
        if not mcp_md.exists():
            return []

        try:
            content = mcp_md.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise MCPRegistryUnavailable(f"cannot read {mcp_md}: {exc}") from exc

        try:
            specs = parse_mcp_md_text(
                content,
                mcp_md_path=mcp_md,
                read_paths=None,
                resolve_env=False,
            )
        except Exception as exc:
            raise MCPRegistryDescriptorInvalid(
                f"mcp.md at {mcp_md} could not be parsed: {exc}"
            ) from exc

        materialized = []
        for spec in specs:
            try:
                _validate_server_name(spec.name)
            except ValueError:
                _logger.warning(
                    "FilesystemMCPServerRegistryBackend: skipping malformed "
                    "section name %r in mcp.md (failed charset validation)",
                    spec.name,
                )
                continue
            resolved_env = _resolve_env_vars(spec.env, spec.name)
            materialized_spec = replace(spec, env=resolved_env)
            if self._read_paths:
                validate_mcp_server_args(materialized_spec, self._read_paths)
            materialized.append(materialized_spec)

        # Sort lexicographically per MUST 5 (consistent with list_mcp_servers).
        materialized.sort(key=lambda s: s.name)
        return materialized

    def validate(self, name: str) -> ValidationResult:
        """Static check of the named server descriptor.

        Does NOT spawn the MCP subprocess (MUST 2 analog -- static only).
        Returns ``ValidationResult(ok=False, errors=[...])`` when the server
        is absent; does NOT raise ``MCPServerNotInRegistry``.

        Checks:
        - Descriptor parses from mcp.md.
        - ``command`` exists on PATH (warn if absent -- do not fail; PATH at
          run time may differ).
        - ``transport`` value is recognized (``"stdio"`` is the only supported
          value in v1).
        - ``$VAR`` refs resolve against current ``os.environ`` (warn if not --
          do not fail; env may be set at agent run time).

        Validates ``name`` charset at the API boundary (MUST 1).
        """
        try:
            _validate_server_name(name)
        except ValueError as exc:
            return ValidationResult(ok=False, errors=[str(exc)], warnings=[])

        mcp_md = self._agent_root / "mcp.md"
        if not mcp_md.exists():
            return ValidationResult(
                ok=False,
                errors=[
                    f"MCP server {name!r} not found: mcp.md does not exist at {mcp_md}."
                ],
                warnings=[],
            )

        try:
            content = mcp_md.read_text(encoding="utf-8")
            specs = parse_mcp_md_text(
                content,
                mcp_md_path=mcp_md,
                read_paths=None,
                resolve_env=False,
            )
        except Exception as exc:
            return ValidationResult(
                ok=False,
                errors=[f"mcp.md parse error: {exc}"],
                warnings=[],
            )

        matched: MCPServerSpec | None = None
        for spec in specs:
            if spec.name == name:
                matched = spec
                break

        if matched is None:
            available = sorted(s.name for s in specs)
            return ValidationResult(
                ok=False,
                errors=[f"MCP server {name!r} not in registry. Available: {available}"],
                warnings=[],
            )

        errors: list[str] = []
        warnings: list[str] = []

        # Check command exists on PATH (warn, not error -- PATH may differ at runtime).
        if matched.command and shutil.which(matched.command) is None:
            warnings.append(
                f"command {matched.command!r} not found on PATH; "
                f"may be available at agent run time."
            )

        # Check transport is recognized.
        if matched.transport not in ("stdio",):
            errors.append(
                f"transport {matched.transport!r} is not recognized. "
                f"Only 'stdio' is supported in v1."
            )

        # Check $VAR refs resolve (warn, not error -- env may be set at run time).
        for env_key, env_val in matched.env.items():
            if env_val.startswith("$"):
                var_name = env_val[1:]
                if os.environ.get(var_name) is None:
                    warnings.append(
                        f"env var ${var_name} (for {env_key!r}) not set in current "
                        f"process; must be set at agent run time."
                    )

        ok = len(errors) == 0
        return ValidationResult(ok=ok, errors=errors, warnings=warnings)

    # ─── Lock backend resolution ──────────────────────────────────────────

    def _resolve_lock_backend(self) -> "LockBackend":
        """Lazily resolve the default lock backend on first use.

        Routes through ``get_default_lock_backend(agent_root)`` so operators
        who have set ``ATOMIC_AGENTS_LOCK_BACKEND=redis`` automatically get
        ``RedisLockBackend`` without any extra configuration here. Single-host
        operators get ``FilesystemLockBackend(agent_root)`` which stores the
        lock at ``<agent_root>/.mcp_registry.lock`` (distinct from the agent's
        main ``.lock`` so the two locks never deadlock per spec/36 constructor
        docstring).

        Callers that pass ``lock_backend=`` at construction bypass this
        entirely and always get their custom backend.

        Raises:
            MCPRegistryUnavailable: if the lock backend cannot be resolved
                (e.g., ATOMIC_AGENTS_LOCK_BACKEND is set to an unknown value,
                a required optional dependency is missing, or the URL is
                malformed). Wraps any exception from get_default_lock_backend
                so the CLI's existing MCPRegistryError catch-all handles it
                cleanly instead of surfacing a raw Python traceback.
        """
        if self._lock_backend is None:
            try:
                self._lock_backend = get_default_lock_backend(self._agent_root)
            except Exception as exc:
                raise MCPRegistryUnavailable(
                    f"could not resolve lock backend: {exc}"
                ) from exc
        return self._lock_backend

    # ─── Capability-gated lifecycle (PR 3) ───────────────────────────────

    def install(self, spec: MCPServerSpec) -> MCPServerRef:
        """Install a new MCP server into mcp.md atomically.

        Uses a LockBackend lease around the read-modify-write critical
        section per spec/36 MUST 9 and the Install / uninstall semantics
        section added at PR 3.

        Raises:
            ValueError: invalid spec.name (charset or path-traversal).
            ValueError: spec.command is empty or None.
            MCPServerAlreadyInstalled: name already exists in mcp.md.
            MCPRegistryUnavailable: lock contention timeout, lock lease
                expired mid-install, or filesystem I/O error.
            MCPRegistryDescriptorInvalid: existing mcp.md cannot be parsed.

        Returns an MCPServerRef projecting name/description/transport from
        the input spec. version is always None; source is set to
        ``mcp.md#section:<name>``. The Ref carries no env/command/args so
        the caller can safely echo it without leaking secrets.
        """
        # MUST 1: charset validation at the API boundary, BEFORE any I/O.
        _validate_server_name(spec.name)

        # spec.command is required for rendering.
        if not spec.command:
            raise ValueError(
                f"MCP server {spec.name!r}: install requires a non-empty command."
            )

        lock_backend = self._resolve_lock_backend()

        # MUST 9: acquire the registry lock. LockBusy on timeout maps to
        # MCPRegistryUnavailable so callers see a consistent transient-failure
        # exception rather than a LockBusy leaking through the abstraction.
        try:
            handle = lock_backend.acquire(
                "mcp_registry", timeout=self._install_lock_timeout
            )
        except LockBusy as exc:
            raise MCPRegistryUnavailable(
                f"mcp_registry lock contention: another install or uninstall "
                f"may be in progress ({exc})"
            ) from exc

        with handle:
            mcp_md = self._agent_root / "mcp.md"

            # Cleanup stale tempfiles from prior crashed installs inside the
            # lock so cleanup is serialized with this install operation. Scoped
            # to siblings of mcp.md (not recursive) to avoid touching unrelated
            # files elsewhere under agent_root (MUST 2 spirit).
            try:
                cleanup_stale_tempfiles_for_file(mcp_md)
            except OSError:
                _logger.warning(
                    "cleanup_stale_tempfiles_for_file failed in install; continuing",
                    exc_info=True,
                )

            # Read current mcp.md content. FileNotFoundError means no servers
            # installed yet; treat as empty for the cold-start case.
            try:
                content = mcp_md.read_text(encoding="utf-8")
            except FileNotFoundError:
                content = ""
            except OSError as exc:
                raise MCPRegistryUnavailable(f"cannot read {mcp_md}: {exc}") from exc

            # Parse with resolve_env=False to keep $VAR refs raw in memory.
            try:
                specs = parse_mcp_md_text(
                    content,
                    mcp_md_path=mcp_md,
                    read_paths=None,
                    resolve_env=False,
                )
            except Exception as exc:
                raise MCPRegistryDescriptorInvalid(
                    f"mcp.md at {mcp_md} could not be parsed: {exc}"
                ) from exc

            # Dual-probe collision detection (Stream B finding B-4).
            # Check both the raw H2 header text AND the parsed spec list so a
            # malformed section (no command:) still blocks a name collision.
            h2_names = set(re.findall(r"^## (\S+)", content, re.MULTILINE))
            parsed_names = {s.name for s in specs}
            if spec.name in h2_names or spec.name in parsed_names:
                raise MCPServerAlreadyInstalled(
                    f"MCP server {spec.name!r} is already in mcp.md. "
                    f"Uninstall it first or choose a different name."
                )

            # Lease check before write: no-op for filesystem (no heartbeat);
            # raises LockLost for Redis when the lease has expired mid-install.
            try:
                check_lock_lost(handle)
            except LockLost as exc:
                raise MCPRegistryUnavailable(
                    f"mcp_registry lock lease expired mid-install: {exc}"
                ) from exc
            except Exception as exc:
                raise MCPRegistryUnavailable(
                    f"mcp_registry lock state check failed mid-install: {exc}"
                ) from exc

            # Render the updated file (existing specs + new spec).
            updated = list(specs) + [spec]
            rendered = render_mcp_md_full(updated)

            try:
                atomic_write(mcp_md, rendered)
            except OSError as exc:
                raise MCPRegistryUnavailable(f"cannot write {mcp_md}: {exc}") from exc

        # with-block exits here; handle.__exit__ releases the lock.

        # Project MCPServerRef from spec. No env/command/args in the Ref
        # so the caller can safely echo it without leaking secrets.
        description_first_line = (
            spec.description.splitlines()[0].strip() if spec.description else ""
        )
        return MCPServerRef(
            name=spec.name,
            description=description_first_line,
            transport=spec.transport,
            version=None,
            source=f"mcp.md#section:{spec.name}",
        )

    def uninstall(self, name: str) -> None:
        """Remove an MCP server from mcp.md atomically. Idempotent.

        Missing name is a no-op -- no exception raised, matches the
        SQLiteToolRegistryBackend.uninstall precedent (spec/25).

        No pre-lock fast-path: the lock is always acquired before reading
        mcp.md because a concurrent install could add the name between an
        unlocked check and the subsequent read-modify-write. Per spec/36
        MUST 9 (no fast-path shortcut).

        Raises:
            ValueError: invalid name (charset or path-traversal).
            MCPRegistryUnavailable: lock contention timeout, lock lease
                expired mid-uninstall, or filesystem I/O error.
            MCPRegistryDescriptorInvalid: existing mcp.md cannot be parsed.

        Returns None on both the present-and-removed path AND the
        absent-no-op path.
        """
        # MUST 1: charset validation at the API boundary BEFORE any I/O.
        _validate_server_name(name)

        lock_backend = self._resolve_lock_backend()

        try:
            handle = lock_backend.acquire(
                "mcp_registry", timeout=self._install_lock_timeout
            )
        except LockBusy as exc:
            raise MCPRegistryUnavailable(
                f"mcp_registry lock contention: another install or uninstall "
                f"may be in progress ({exc})"
            ) from exc

        with handle:
            mcp_md = self._agent_root / "mcp.md"

            # Cleanup stale tempfiles from prior crashed installs inside the
            # lock so cleanup is serialized with this uninstall operation.
            try:
                cleanup_stale_tempfiles_for_file(mcp_md)
            except OSError:
                _logger.warning(
                    "cleanup_stale_tempfiles_for_file failed in uninstall; continuing",
                    exc_info=True,
                )

            try:
                content = mcp_md.read_text(encoding="utf-8")
            except FileNotFoundError:
                # Absent mcp.md means no servers; nothing to uninstall.
                _logger.debug(
                    "uninstall: mcp.md does not exist at %s; no-op for %r",
                    mcp_md,
                    name,
                )
                return
            except OSError as exc:
                raise MCPRegistryUnavailable(f"cannot read {mcp_md}: {exc}") from exc

            try:
                specs = parse_mcp_md_text(
                    content,
                    mcp_md_path=mcp_md,
                    read_paths=None,
                    resolve_env=False,
                )
            except Exception as exc:
                raise MCPRegistryDescriptorInvalid(
                    f"mcp.md at {mcp_md} could not be parsed: {exc}"
                ) from exc

            # Dual-probe absent check (mirrors install's collision check).
            h2_names = set(re.findall(r"^## (\S+)", content, re.MULTILINE))
            parsed_names = {s.name for s in specs}
            if name not in h2_names and name not in parsed_names:
                # Idempotent no-op: name is not present, nothing to remove.
                _logger.debug("uninstall: server %r not in registry; no-op", name)
                return

            # Lease check before write.
            try:
                check_lock_lost(handle)
            except LockLost as exc:
                raise MCPRegistryUnavailable(
                    f"mcp_registry lock lease expired mid-uninstall: {exc}"
                ) from exc
            except Exception as exc:
                raise MCPRegistryUnavailable(
                    f"mcp_registry lock state check failed mid-uninstall: {exc}"
                ) from exc

            # Render the file with the named spec removed.
            updated = [s for s in specs if s.name != name]
            rendered = render_mcp_md_full(updated)

            try:
                atomic_write(mcp_md, rendered)
            except OSError as exc:
                raise MCPRegistryUnavailable(f"cannot write {mcp_md}: {exc}") from exc

        return None

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def refresh_capabilities(self) -> MCPServerRegistryCapabilities:
        """Filesystem capabilities are static -- returns the cached constant.

        No-op refresh because filesystem has no remote dependency. The
        ``capabilities`` property returns the same frozen instance every time.
        """
        return self.capabilities

    def close(self) -> None:
        """No-op. Filesystem backend holds no open resources.

        Idempotent (MUST 6b): calling ``close()`` twice does not raise.
        """
