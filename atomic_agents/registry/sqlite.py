"""SQLiteToolRegistryBackend — stdlib ``sqlite3`` reference implementation.

This is the second reference impl in the ToolRegistryBackend protocol-
pattern series (alongside ``FilesystemToolRegistryBackend``). No optional
dependency required — ``sqlite3`` ships with CPython.

Storage shape (plan-subagent Risk A fix — hybrid metadata-in-SQL +
handler-bodies-on-disk):

* One SQLite file at the path passed to the constructor (or
  ``":memory:"`` for tests). URL form:
  ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL=sqlite:///path/to/tools.db?agent_scope=<name>``.
* ``tools`` table holds **metadata**: ``agent_scope``, ``name``,
  ``descriptor_json`` (the operator's descriptor frontmatter as JSON),
  ``handler_path`` (filesystem path to the .py file on disk),
  ``version``, ``classification``, ``created_at``, ``updated_at``.
  Composite PRIMARY KEY ``(agent_scope, name)`` so two scopes can both
  have a tool of the same name.
* Handler **bodies** live on disk under
  ``<handlers_root>/<agent_scope>/<name>.py`` — same shape Python's
  import machinery expects. The base64-encoded-source + ``exec()``
  approach was rejected at plan-subagent time because closures over
  module-globals, top-level ``import`` statements, and module-level
  resource setup (``session = requests.Session()`` patterns) would
  silently break at first dispatch. Filepath storage uses the same
  ``importlib.util.spec_from_file_location`` path the filesystem
  reference uses — handler ergonomics are identical.
* ``meta`` table holds ``schema_version`` (with ``INSERT OR IGNORE``
  cold-start race mitigation per #61 PR 3 + #63 PR 3 lesson).
* WAL journal mode + ``synchronous=NORMAL`` for concurrent
  reader/writer interleaving on local filesystems.

Cross-scope isolation: ``list_tools()`` / ``load_tool()`` /
``uninstall()`` ALL filter ``WHERE agent_scope = ?``. The scope is
hardcoded from the constructor; ``install()`` never accepts a scope
parameter. Per-scope handler subdirectory is defense-in-depth at the
filesystem layer — even if a future migration drops the SQL filter,
``<handlers_root>/<agent_scope>/`` keeps handler files separated.

Trust model (spec/25 + plan-subagent Risk K): the SQLite backend is
process-shared CATALOG, not process-shared TRUST. Multi-tenant
deployments MUST scope at the process level (one framework process
per tenant). The framework is NOT a sandbox; install() chooses what
code executes in the framework's process. The judge layer (spec/28)
is the runtime defense, not the registry layer.

Thread-safety: ``threading.local`` connection pool gives each thread
its own ``sqlite3.Connection`` for file-backed deployments. sqlite3
connections aren't shared across threads by default. WAL mode + per-
thread connections is the standard pattern; same shape as
``SQLiteLogBackend`` / ``SQLiteAgentProfileBackend``.

The ``:memory:`` mode is **single-threaded test-only**: the
constructor opens one connection with the default
``check_same_thread=True`` so cross-thread access raises
``ProgrammingError`` immediately at the misuse site rather than
producing silent corruption. Operators who need multi-threaded SQLite
must use file-backed mode.

Concurrent multi-process: WAL mode supports it on **local filesystems**.
Multiple ``SQLiteToolRegistryBackend`` instances against the same db
from different processes on the SAME host see consistent reads +
serialized writes. **Network-mounted filesystems (NFS, SMB) are NOT
supported** — SQLite WAL on NFS is documented-broken upstream.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import warnings
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlparse


def _redact_url(url: str) -> str:
    """Redact credentials in a URL for safe error-message echo.

    Step 11 adversarial P1 REPRODUCED: when an operator pastes a
    credential-bearing URL (e.g., ``postgres://user:supersecret@host/db``)
    into ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL`` while
    ``..._BACKEND=sqlite``, the URL factory's ``ValueError`` echoed
    the raw URL including the password. ``doctor.check_tool_registry_backend``
    catches the exception via broad ``except Exception`` and drops the
    message, BUT any other caller of this factory (notably
    ``get_default_tool_registry_backend`` → ``AtomicAgent.__init__``)
    propagates the raw exception, leaking the credential to logs,
    WSGI middleware, error-tracking services.

    Strips credentials from netloc + truncates path/query/fragment to
    keep error messages diagnostic but not credential-bearing.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        # Malformed URL — return a sanitized stub.
        return "<unparseable url>"
    if parsed.password or parsed.username:
        # Redact the userinfo portion of netloc.
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        try:
            sanitized = parsed._replace(netloc=host).geturl()
            return sanitized
        except Exception:
            return f"{parsed.scheme}://..." if parsed.scheme else "<redacted>"
    # No credentials — but still cap length to bound the echoed string.
    return url if len(url) <= 256 else url[:256] + "..."

from .._io import atomic_write
from ..exceptions import (
    ToolAlreadyInstalled,
    ToolDescriptorInvalid,
    ToolHandlerImportFailed,
    ToolNotInRegistry,
)
from ..tools import ToolDefinition
from .filesystem import (
    _MAX_DESCRIPTOR_BYTES,
    _import_handler,
    _parse_descriptor,
    _validate_tool_name,
)
from .types import ToolRef, ToolRegistryCapabilities, ValidationResult


# Schema version — bumped on any breaking schema change. Idempotent
# init via ``INSERT OR IGNORE`` per the established sibling pattern
# (cold-start race mitigation from #61 PR 3 SQLiteLogBackend P0 #2).
_SCHEMA_VERSION = 1


_CREATE_TOOLS = """
CREATE TABLE IF NOT EXISTS tools (
    agent_scope TEXT NOT NULL,
    name TEXT NOT NULL,
    descriptor_json TEXT NOT NULL,
    handler_path TEXT NOT NULL,
    version TEXT,
    classification TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent_scope, name)
)
"""

# Composite (agent_scope, name) is already the PRIMARY KEY so SQLite
# auto-builds an index for it. A scope-only index speeds up
# ``list_tools()``'s ORDER BY name when many tools live under one scope.
_CREATE_TOOLS_SCOPE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tools_scope_name
ON tools(agent_scope, name)
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)
"""


# Tool-name validation imported from filesystem.py:_validate_tool_name —
# operator-controlled name flows into both the SQL primary-key column
# and the handler-file path. Path-traversal refusal at the API boundary
# matches spec/25 MUST #1. Cross-module import of underscore-prefixed
# helpers is a documented coupling smell (Step 9.1 maintainability
# finding) — tracked for a future hoist into a shared
# ``atomic_agents/registry/_descriptor.py`` module so both backends
# import from one place.


class SQLiteToolRegistryBackend:
    """SQLite-backed ToolRegistryBackend — stdlib sqlite3, no optional dep.

    Conforms to the ``ToolRegistryBackend`` Protocol. Constructed once
    per process (per agent_scope) with a db file path + scope.

    Args:
        db_path: filesystem path to the SQLite db file, OR the literal
            string ``":memory:"`` for an in-memory database (test-only —
            emits ``RuntimeWarning`` on construction; all data lost on
            process exit).
        agent_scope: opaque per-scope identifier. All ``list_tools`` /
            ``load_tool`` / ``uninstall`` calls filter on this value.
            Different scopes against the same db are isolated. Plain
            string identifier — refuses path-traversal tokens at the
            constructor (defense in depth; the SQL parametrization
            already prevents injection but a scope like
            ``"../../other"`` would smuggle through ``handlers_root``
            path construction).
        handlers_root: directory under which handler .py files live
            (one subdir per agent_scope). Defaults to
            ``<db_path>.parent / "handlers"`` for file-backed
            deployments. For ``:memory:`` the default is a tempdir-
            shaped path under ``db_path.parent`` of the cwd, which
            test fixtures override; production :memory: use is
            test-only anyway. Created with ``mkdir(parents=True,
            exist_ok=True)`` on first install.
    """

    @property
    def backend_id(self) -> str:
        return "sqlite"

    def __init__(
        self,
        db_path: Path | str,
        agent_scope: str = "default",
        *,
        handlers_root: Path | None = None,
    ) -> None:
        # ``agent_scope`` is operator-controlled and flows into
        # filesystem paths under ``handlers_root``. Refuse path-
        # traversal tokens at the constructor — defense in depth on
        # top of the SQL parametrization (the SQL filter already
        # treats agent_scope as opaque, but the per-scope handler
        # subdirectory does not).
        if not agent_scope or not isinstance(agent_scope, str):
            raise ValueError(
                "SQLiteToolRegistryBackend agent_scope must be a non-empty string"
            )
        if "/" in agent_scope or "\\" in agent_scope:
            raise ValueError(
                f"agent_scope {agent_scope!r} contains a path separator — "
                f"refused to prevent handlers_root escape"
            )
        if agent_scope.startswith(".") or ".." in agent_scope:
            raise ValueError(
                f"agent_scope {agent_scope!r} starts with '.' or contains "
                f"'..' — refused to prevent traversal"
            )
        for ch in agent_scope:
            if ord(ch) < 0x20 or ord(ch) == 0x7F:
                raise ValueError(
                    f"agent_scope {agent_scope!r} contains a control "
                    f"character (0x{ord(ch):02x}) — refused"
                )
        self._agent_scope = agent_scope

        # Detect :memory: sentinel — string equality, NOT Path coercion
        # (Path(':memory:') would create a real file named ':memory:').
        if db_path == ":memory:":
            self._in_memory = True
            self._db_path_str = ":memory:"
            # Single shared connection for :memory:. ``check_same_thread``
            # left at its default ``True`` — :memory: mode is test-only
            # and single-threaded by design (see module docstring); a
            # ProgrammingError at first cross-thread use is the honest
            # failure mode. PRAGMA journal_mode is a no-op on :memory:
            # databases (SQLite forces 'memory' journal regardless) so
            # the WAL pragma is intentionally skipped here.
            self._shared_conn: sqlite3.Connection | None = sqlite3.connect(
                self._db_path_str
            )
            self._shared_conn.row_factory = sqlite3.Row
            self._ensure_schema(self._shared_conn)
            # Default handlers_root for :memory: — isolated tempdir
            # per backend instance. Step 11 adversarial P2 REPRODUCED:
            # the old default (``Path.cwd() / ".handlers"``) wrote
            # handler bodies to a process-CWD-relative dir, contradicting
            # the ":memory: is non-persistent" promise AND letting two
            # :memory: backends with the same scope+name clobber each
            # other's on-disk handlers. Per-instance tempdir keeps the
            # ":memory:" semantics honest. Tests override via the kwarg.
            self._handlers_root = (
                Path(handlers_root)
                if handlers_root is not None
                else Path(
                    tempfile.mkdtemp(prefix="atomic_agents_memory_handlers_")
                )
            )
            warnings.warn(
                "SQLiteToolRegistryBackend(':memory:') is non-persistent — "
                "all tools are lost on process exit. Use "
                "sqlite:///absolute/path/to/tools.db for durable "
                "deployments.",
                RuntimeWarning,
                stacklevel=2,
            )
        else:
            self._in_memory = False
            self._db_path = Path(db_path)
            self._db_path_str = str(self._db_path)
            self._shared_conn = None
            self._handlers_root = (
                Path(handlers_root)
                if handlers_root is not None
                else self._db_path.parent / "handlers"
            )

        # Validate handlers_root — Step 11 adversarial P2: an operator
        # passing ``handlers_root=Path("/")`` would make install()
        # write handler files to /<scope>/<name>.py (succeeds when
        # running as root, refused otherwise). Refuse paths that have
        # <= 1 component after resolution. Mirrors the agent_scope
        # defense-in-depth rigor.
        resolved = self._handlers_root.resolve(strict=False)
        if len(resolved.parts) <= 1:
            raise ValueError(
                f"handlers_root {self._handlers_root!r} resolves to "
                f"{resolved!r} which has <= 1 path component — refused "
                f"to prevent filesystem-root scoping. Pass an absolute "
                f"path inside an operator-owned directory."
            )

        # Per-thread connection pool (file-backed only; :memory: uses
        # the shared connection above).
        self._tls = threading.local()

    @property
    def agent_scope(self) -> str:
        """The scope this backend is bound to. Read-only after construction."""
        return self._agent_scope

    @property
    def db_path(self) -> str:
        """The SQLite db path string (or ``":memory:"``). Read-only."""
        return self._db_path_str

    @property
    def handlers_root(self) -> Path:
        """Filesystem directory under which handler .py files live."""
        return self._handlers_root

    # ────────────────────────────────────────────────────────────
    # Connection management

    def _get_conn(self) -> sqlite3.Connection:
        """Return the calling thread's connection, creating on first use."""
        if self._in_memory:
            return self._shared_conn  # type: ignore[return-value]
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            # Ensure parent dir exists before sqlite3.connect — sqlite3
            # raises OperationalError if the parent dir is missing.
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path_str)
            conn.row_factory = sqlite3.Row
            # busy_timeout BEFORE the WAL pragma — Step 11 adversarial
            # P1 REPRODUCED (3/5 races) showed `PRAGMA journal_mode=WAL`
            # raises `sqlite3.OperationalError: database is locked`
            # when N processes concurrently open the same fresh db
            # (WAL transition needs an EXCLUSIVE lock; contention is a
            # hard failure without a busy_timeout). 5000ms is generous
            # for a cold-start race that should resolve in <50ms once
            # the winning process completes the WAL transition.
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema(conn)
            self._tls.conn = conn
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables + indexes + meta row. Idempotent across processes.

        Uses ``CREATE TABLE IF NOT EXISTS`` (always idempotent) and
        ``INSERT OR IGNORE`` for the schema_version row — the latter is
        the multi-process cold-start race mitigation per the established
        sibling pattern (#61 PR 3 + #63 PR 3 + this PR).
        """
        with conn:  # implicit transaction
            conn.execute(_CREATE_TOOLS)
            conn.execute(_CREATE_TOOLS_SCOPE_INDEX)
            conn.execute(_CREATE_META)
            # INSERT OR IGNORE — whichever of N concurrent processes
            # loses the insert sees the row already there and proceeds.
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(_SCHEMA_VERSION)),
            )
        # Verify schema_version matches expected — defensive guard
        # against opening a future-incompatible db file. No auto-
        # migration; operators run an explicit migrate command (
        # successor issue tracked).
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or int(row["value"]) != _SCHEMA_VERSION:
            raise RuntimeError(
                f"SQLiteToolRegistryBackend schema version mismatch at "
                f"{self._db_path_str}: expected {_SCHEMA_VERSION}, "
                f"found {row['value'] if row else 'no row'}. Migration "
                f"required."
            )

    # ────────────────────────────────────────────────────────────
    # Discovery

    def list_tools(self) -> list[ToolRef]:
        """SELECT * FROM tools WHERE agent_scope=? ORDER BY name."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT name, descriptor_json, handler_path, version, classification
            FROM tools
            WHERE agent_scope = ?
            ORDER BY name
            """,
            (self._agent_scope,),
        ).fetchall()
        refs: list[ToolRef] = []
        for row in rows:
            try:
                descriptor = json.loads(row["descriptor_json"])
            except json.JSONDecodeError:
                # Corrupt row — skip; operators triage via validate(name).
                continue
            description = str(descriptor.get("description", "") or "")
            refs.append(
                ToolRef(
                    name=row["name"],
                    description=description,
                    classification=row["classification"],
                    version=row["version"],
                    source=f"sqlite://{self._db_path_str}#{self._agent_scope}/{row['name']}",
                )
            )
        return refs

    def load_tool(self, name: str) -> ToolDefinition:
        """SELECT scope + name; import the handler from handler_path.

        Handler import uses the same ``_import_handler`` helper as the
        filesystem reference — Python's import machinery sees a real
        .py file, so closures over module-level imports + decorators +
        top-level resource setup all work the way operators expect.
        """
        _validate_tool_name(name)
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT descriptor_json, handler_path, version, classification
            FROM tools
            WHERE agent_scope = ? AND name = ?
            """,
            (self._agent_scope, name),
        ).fetchone()
        if row is None:
            raise ToolNotInRegistry(
                f"tool {name!r} not found in SQLite registry "
                f"(scope={self._agent_scope!r}, db={self._db_path_str})"
            )

        try:
            descriptor = json.loads(row["descriptor_json"])
        except json.JSONDecodeError as exc:
            raise ToolDescriptorInvalid(
                f"tool {name!r} descriptor JSON is corrupt: {exc}"
            ) from exc

        handler_path = Path(row["handler_path"])
        if not handler_path.is_file():
            raise ToolHandlerImportFailed(
                f"tool {name!r} handler file missing at {handler_path} "
                f"(referenced by SQLite row; operator may have deleted "
                f"the file out-of-band)"
            )

        # ``_import_handler`` raises ``ToolHandlerImportFailed`` on
        # missing handler symbol or import-time exceptions — same
        # contract as the filesystem reference.
        handler = _import_handler(handler_path, name)

        description = str(descriptor.get("description", "") or "")
        input_schema = descriptor.get("input_schema") or {
            "type": "object",
            "properties": {},
            "required": [],
        }
        if not isinstance(input_schema, dict):
            raise ToolDescriptorInvalid(
                f"tool {name!r} descriptor input_schema has type "
                f"{type(input_schema).__name__} — expected dict"
            )
        classification = row["classification"]
        return ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            classification=classification,
        )

    def validate(self, name: str) -> ValidationResult:
        """Static check on the named tool — does NOT execute the handler.

        Spec/25 Decision 6 (same as filesystem reference). Parses the
        descriptor JSON + imports the handler module + checks the
        handler is callable + validates classification.
        """
        errors: list[str] = []
        warnings_out: list[str] = []

        try:
            _validate_tool_name(name)
        except ValueError as exc:
            return ValidationResult(ok=False, errors=[str(exc)], warnings=[])

        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT descriptor_json, handler_path, classification
            FROM tools
            WHERE agent_scope = ? AND name = ?
            """,
            (self._agent_scope, name),
        ).fetchone()
        if row is None:
            return ValidationResult(
                ok=False,
                errors=[
                    f"tool {name!r} not in registry "
                    f"(scope={self._agent_scope!r})"
                ],
                warnings=[],
            )

        try:
            descriptor = json.loads(row["descriptor_json"])
        except json.JSONDecodeError as exc:
            return ValidationResult(
                ok=False,
                errors=[f"descriptor JSON corrupt: {exc}"],
                warnings=[],
            )

        if not descriptor.get("description"):
            warnings_out.append(
                f"tool {name!r} has no description — LLM-routing quality degrades"
            )

        classification = row["classification"]
        if classification is None:
            warnings_out.append(
                f"tool {name!r} has no classification — judge layer will "
                f"default to external_side_effect at dispatch"
            )
        elif classification not in {
            "read_only",
            "reversible_write",
            "external_side_effect",
            "high_risk",
        }:
            errors.append(
                f"tool {name!r} classification {classification!r} is not "
                f"a valid ActionClass"
            )

        handler_path = Path(row["handler_path"])
        if not handler_path.is_file():
            errors.append(
                f"tool {name!r} handler file missing at {handler_path}"
            )
        else:
            try:
                handler = _import_handler(handler_path, name)
            except ToolHandlerImportFailed as exc:
                errors.append(str(exc))
            else:
                if not callable(handler):
                    errors.append(
                        f"tool {name!r} handler at {handler_path} is not "
                        f"callable (got {type(handler).__name__})"
                    )

        return ValidationResult(
            ok=not errors, errors=errors, warnings=warnings_out
        )

    # ────────────────────────────────────────────────────────────
    # Capability-gated mutation — install / uninstall flipped True

    def install(
        self, source: str, version: str | None = None
    ) -> ToolRef:
        """Install a tool from ``source`` (a filesystem directory).

        ``source`` MUST be a filesystem path string. The directory must
        contain ``<name>.md`` (descriptor) + ``<name>.py`` (handler).
        The name is derived from the .md / .py file stems (both MUST
        match).

        ``version`` is reserved (spec/25 Decision 4). Backends declaring
        ``supports_versioning=False`` (which PR 3 SQLite does) MUST
        reject non-None values — capability honesty (plan-subagent
        Risk L). The column accepts non-NULL values for forward
        compatibility but PR 3 raises ``ValueError`` here.

        Semantics:

        * Reads the descriptor + handler from ``source``.
        * Validates the name against path-traversal.
        * Atomically copies the handler .py into
          ``<handlers_root>/<agent_scope>/<name>.py`` via
          ``_io.atomic_write``.
        * INSERT INTO tools ON CONFLICT(agent_scope, name) DO NOTHING.
          If ``cursor.rowcount == 0``, raises ``ToolAlreadyInstalled``.

        Raises:
            ValueError: invalid name OR non-None version on a backend
                without versioning.
            ToolDescriptorInvalid: descriptor parse error.
            ToolHandlerImportFailed: handler module syntax error /
                missing handler symbol at install time (caught early
                so the row never lands on disk for a broken module).
            ToolAlreadyInstalled: a tool with the same name already
                exists under this scope.
        """
        if version is not None and not self.capabilities().supports_versioning:
            raise ValueError(
                f"version pin {version!r} passed to install() but backend "
                f"declares supports_versioning=False. PR 3 stores the "
                f"version column for forward compatibility but does not "
                f"dispatch on it."
            )

        # Source must be a directory containing <name>.md + <name>.py.
        # The name is derived from the descriptor file stem.
        source_path = Path(source)
        if not source_path.is_dir():
            raise ValueError(
                f"install source {source!r} is not a directory. Pass an "
                f"absolute path to a directory containing <name>.md + "
                f"<name>.py."
            )

        # Find the descriptor — exactly one .md file in the source dir.
        descriptors = sorted(source_path.glob("*.md"))
        if not descriptors:
            raise ValueError(
                f"install source {source_path} contains no descriptor .md file"
            )
        if len(descriptors) > 1:
            raise ValueError(
                f"install source {source_path} contains multiple .md "
                f"files; install() expects exactly one"
            )
        descriptor_path = descriptors[0]
        name = descriptor_path.stem

        _validate_tool_name(name)

        # Parse descriptor (uses the filesystem helper — same size cap
        # + frontmatter discipline) — fail fast on malformed input.
        parsed = _parse_descriptor(descriptor_path)
        descriptor_name = parsed.frontmatter.get("name")
        if descriptor_name is not None and descriptor_name != name:
            raise ToolDescriptorInvalid(
                f"tool {name!r} descriptor at {descriptor_path} declares "
                f"name={descriptor_name!r} — must match file stem"
            )

        handler_src_path = source_path / f"{name}.py"
        if not handler_src_path.is_file():
            raise ToolHandlerImportFailed(
                f"install source {source_path} is missing handler file "
                f"{name}.py"
            )

        # Verify the handler imports cleanly (with the documented top-
        # level-side-effect tradeoff). install() is the operator's
        # explicit gate — refusing to install a broken handler is
        # better than installing it and failing at every agent
        # construction afterwards.
        _import_handler(handler_src_path, name)

        # Read handler source for atomic write — the handler lives at
        # <handlers_root>/<agent_scope>/<name>.py.
        try:
            handler_src = handler_src_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolHandlerImportFailed(
                f"could not read handler source at {handler_src_path}: {exc}"
            ) from exc

        scope_dir = self._handlers_root / self._agent_scope
        scope_dir.mkdir(parents=True, exist_ok=True)
        handler_dest = scope_dir / f"{name}.py"

        descriptor_json = json.dumps(parsed.frontmatter, default=str)
        classification = parsed.frontmatter.get("classification")
        if classification is not None:
            classification = str(classification)
        now = datetime.now().astimezone().isoformat()

        # **CRITICAL ORDERING (Step 11 adversarial P1 fix — REPRODUCED
        # 50/50 race pre-fix).** The original implementation wrote the
        # handler file FIRST, then INSERT'd. Concurrent install() of the
        # same name produced: T1 atomic_write → T2 atomic_write
        # (clobbers) → T1 INSERT wins → T2 INSERT loses → T2's
        # `handler_dest.unlink()` rollback DELETED THE WINNER'S file.
        # Winner's catalog row remained but pointed at a missing handler
        # → permanent `ToolHandlerImportFailed` until manual
        # uninstall+reinstall. Spec/25 promises install is atomic at the
        # tool level — write-then-INSERT violated that.
        #
        # Fixed shape: INSERT first (atomic at the SQL level via
        # ON CONFLICT DO NOTHING + rowcount check). Only on success do
        # we materialize the handler file. The loser sees rowcount=0
        # and raises ToolAlreadyInstalled WITHOUT touching disk —
        # the winner's handler file is untouched.
        conn = self._get_conn()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO tools
                  (agent_scope, name, descriptor_json, handler_path,
                   version, classification, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_scope, name) DO NOTHING
                """,
                (
                    self._agent_scope,
                    name,
                    descriptor_json,
                    str(handler_dest),
                    version,
                    classification,
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                # Loser path — winner's INSERT got there first. Do NOT
                # touch handler_dest (winner's file lives there).
                raise ToolAlreadyInstalled(
                    f"tool {name!r} already installed under scope "
                    f"{self._agent_scope!r}. Call uninstall({name!r}) "
                    f"first if you want to replace it."
                )

        # Winner path — SQL row landed; now write the handler file.
        # There's a brief window where the row exists but the file
        # doesn't; load_tool() would raise ToolHandlerImportFailed
        # honestly during that window (sub-millisecond on local
        # filesystems). The framework's wiring loop tolerates this
        # via the broadened exception catch from #64 PR 2.
        atomic_write(handler_dest, handler_src)

        description = str(parsed.frontmatter.get("description", "") or "")
        return ToolRef(
            name=name,
            description=description,
            classification=classification,
            version=version,
            source=f"sqlite://{self._db_path_str}#{self._agent_scope}/{name}",
        )

    def uninstall(self, name: str) -> None:
        """Remove ``name`` from the catalog. Idempotent.

        Spec/25: ``uninstall`` MUST be a no-op for unknown names —
        uninstalling a name that doesn't exist does not raise.
        Refuses path-traversal names at the API boundary (MUST #1)
        BEFORE any disk / DB access.
        """
        _validate_tool_name(name)
        conn = self._get_conn()
        # Get handler_path before delete so we know which file to remove.
        row = conn.execute(
            "SELECT handler_path FROM tools WHERE agent_scope = ? AND name = ?",
            (self._agent_scope, name),
        ).fetchone()
        if row is None:
            return  # idempotent no-op
        handler_path = Path(row["handler_path"])
        with conn:
            conn.execute(
                "DELETE FROM tools WHERE agent_scope = ? AND name = ?",
                (self._agent_scope, name),
            )
        try:
            handler_path.unlink()
        except FileNotFoundError:
            pass  # already gone — fine
        except OSError:
            # Failed to remove the handler file. The SQL row is gone;
            # the orphan file will resurface only if a future install()
            # for the same scope+name happens AND atomic_write doesn't
            # overwrite it (atomic_write DOES, via os.replace). Don't
            # raise — uninstall succeeded at the catalog level.
            pass

    # ────────────────────────────────────────────────────────────
    # Reserved skill catalog — same as filesystem (False)

    def list_skills_catalog(self) -> list[ToolRef]:
        """SQLite tool registry does NOT publish a skill catalog — raises."""
        raise NotImplementedError(
            "SQLiteToolRegistryBackend does not publish a skill catalog — "
            "skill catalog surface is reserved (spec/25 Decision 2)."
        )

    def load_skill_catalog_body(self, name: str) -> str:
        """SQLite tool registry does NOT publish a skill catalog — raises."""
        raise NotImplementedError(
            "SQLiteToolRegistryBackend does not publish a skill catalog body — "
            "skill catalog surface is reserved (spec/25 Decision 2)."
        )

    # ────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> ToolRegistryCapabilities:
        return ToolRegistryCapabilities(
            # PR 3 ships install + uninstall — operators install via
            # source filesystem path; uninstall is idempotent.
            supports_install=True,
            supports_uninstall=True,
            # Reserved — column exists but PR 3 does not dispatch on
            # version (plan-subagent Risk L: capability honesty).
            supports_versioning=False,
            # Static-only validate, like filesystem.
            supports_sandbox_validate=False,
            # Skill catalog reserved (spec/25 Decision 2).
            supports_skills_catalog=False,
            # File-backed SQLite is durable; :memory: is not.
            durable=not self._in_memory,
        )


# ────────────────────────────────────────────────────────────────────
# URL factory


def make_sqlite_tool_registry_backend_from_url(
    url: str,
) -> SQLiteToolRegistryBackend:
    """Parse a ``sqlite://`` URL and construct the backend.

    Accepts:
    * ``"sqlite::memory:"`` or ``"sqlite:///:memory:"`` → in-memory
      backend (emits ``RuntimeWarning``).
    * ``"sqlite:///absolute/path/to/tools.db"`` (3 slashes) → file-backed
      with ``agent_scope="default"``.
    * ``"sqlite:///absolute/path/to/tools.db?agent_scope=<name>"`` —
      explicit scope. The ``handlers_root`` is NOT URL-configurable;
      it defaults to ``<db_path>.parent / "handlers"`` (operators
      wanting a custom location use the constructor directly).

    Raises ``ValueError`` for:
    * non-``sqlite`` scheme
    * netloc-bearing URLs like ``sqlite://host/path``
    * empty path
    * ``#fragment`` (SQLAlchemy-style directives not honored)
    * any query parameter OTHER than ``agent_scope`` (silent drops
      would let an operator's intent slip through unrecognized — same
      shape as the profile / log URL factories' query-fragment refusal,
      narrowed here to allow agent_scope)
    """
    # ``sqlite::memory:`` (no slashes) is the conventional in-memory
    # shorthand — match it before urlparse mangles the structure.
    # Case-insensitive + stripped so ``sqlite::Memory:`` (operator
    # typo) doesn't fall through and create a real file named
    # ``:Memory:`` (same defense as profile/log siblings).
    if url.strip().lower() == "sqlite::memory:":
        return SQLiteToolRegistryBackend(":memory:")

    parsed = urlparse(url.strip())
    # ``_redact_url`` strips credentials from the echoed URL — Step 11
    # P1 REPRODUCED that operator-pasted credentials in this env var
    # would otherwise leak via the propagated ValueError. Apply to all
    # 5 raise sites.
    safe_url = _redact_url(url)
    if parsed.scheme.lower() != "sqlite":
        raise ValueError(
            f"make_sqlite_tool_registry_backend_from_url: url {safe_url!r} has "
            f"scheme {parsed.scheme!r}; expected 'sqlite'"
        )
    if parsed.netloc:
        raise ValueError(
            f"make_sqlite_tool_registry_backend_from_url: url {safe_url!r} "
            f"has a netloc; SQLite URLs use the 3-slash convention "
            f"(sqlite:///absolute/path) with empty netloc."
        )
    if parsed.fragment:
        raise ValueError(
            f"make_sqlite_tool_registry_backend_from_url: url {safe_url!r} "
            f"carries a fragment; not honored by this backend."
        )

    # Parse query — only ``agent_scope`` is recognized; multi-value or
    # other params raise.
    query_pairs = parse_qsl(parsed.query, keep_blank_values=False)
    seen_keys: set[str] = set()
    query_params: dict[str, str] = {}
    for key, value in query_pairs:
        if key in seen_keys:
            # Plan-subagent F-5 spirit: silent drops of duplicate keys
            # are operator-intent footguns (which alice wins?).
            raise ValueError(
                f"make_sqlite_tool_registry_backend_from_url: url "
                f"{safe_url!r} carries duplicate query parameter "
                f"{key!r}; operator intent is ambiguous."
            )
        seen_keys.add(key)
        query_params[key] = value
    unknown_params = set(query_params) - {"agent_scope"}
    if unknown_params:
        raise ValueError(
            f"make_sqlite_tool_registry_backend_from_url: url {safe_url!r} "
            f"carries unsupported query parameters: {sorted(unknown_params)}. "
            f"Only 'agent_scope' is recognized."
        )
    agent_scope = query_params.get("agent_scope", "default")

    path = parsed.path
    if not path or path == "/":
        raise ValueError(
            f"make_sqlite_tool_registry_backend_from_url: url {safe_url!r} "
            f"has an empty path; expected sqlite:///absolute/path/to/tools.db"
        )
    # ``sqlite:///:memory:`` (case-insensitive) → in-memory.
    if path.lower() == "/:memory:":
        return SQLiteToolRegistryBackend(":memory:", agent_scope=agent_scope)
    return SQLiteToolRegistryBackend(Path(path), agent_scope=agent_scope)
