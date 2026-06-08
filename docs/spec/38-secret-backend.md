# spec/38: SecretBackend Protocol

> **Status:** DRAFT at PR 1 (issue #340). Conformance suite covers all MUSTs for `FilesystemSecretBackend`. GCP Secret Manager backend ships at PR 2.

---

## Origin

Carved out from the credential-resolution pattern established in spec/01 (secrets handling) and formalized as the thirteenth backend protocol. Filed as [#340](https://github.com/dep0we/atomic-agents-stack/issues/340) after v1.0.0 shipped the twelve core protocols. Originally drafted as spec/37, then renumbered to spec/38 on merge after spec/37 was assigned to the `atomic-agents serve` runtime ([#342](https://github.com/dep0we/atomic-agents-stack/issues/342)), which landed first. (Separately, the spec/23 slot is an intentional gap: spec/23 was scoped and then superseded before a spec was written.) Both the renumber and the gap are kept as honest history per the project convention (CLAUDE.md §10: spec docs are not aspirational).

**Divergence from issue #340's acceptance sketch.** Issue #340 sketched `FilesystemSecretBackend` as reading from a `.env` file or a vault-relative `secrets/` directory. This spec deliberately supersedes that sketch: MUST 2 below prohibits vault-relative credential paths entirely (machine-scoped sources only), because vault portability must not carry credentials — a vault synced to a shared repo or object store would otherwise leak its keys. `.env` support is not vault-relative either and is deferred to [#361](https://github.com/dep0we/atomic-agents-stack/issues/361). The issue's acceptance text has been reconciled to match this spec (CLAUDE.md §10/§13).

**Cross-links:**
- spec/01. Agent anatomy. Secrets-handling convention: env vars → Keychain → keys.json.
- spec/31. LLMBackend. `KeySpec` and `_get_key()` rewired to delegate here as of PR 1.
- spec/32. PolicyBackend. Precedent for a read-only Protocol with no WritePolicy.
- spec/36. MCPServerRegistryBackend. `@property capabilities`, `_redact_for_error_message`, factory pattern.

---

## Shipping plan (2 PRs)

- **PR 1.** Protocol scaffold + dataclasses + capability advertisement + `FilesystemSecretBackend` reference impl + spec/38 DRAFT + `_get_key()` supersede + all 6 live caller rewires + `check_secret_backend()` doctor check + `atomic-agents secrets check/which/validate` CLI + a full conformance suite plus filesystem-specific and CLI tests.
- **PR 2.** `GCPSecretManagerBackend` reference impl + `gcp` extra + the `docs/deployment/` env-var→SecretBackend migration guide (issue #340 acceptance criterion; its natural home is alongside GCP, where backend migration first becomes operator-relevant) + spec/38 LOCKED.

---

## Overview

`SecretBackend` is the **thirteenth** backend Protocol in the protocol-pattern series. It abstracts credential resolution (API keys and other secrets) behind a Protocol so the framework's core stays small and alternate secret substrates (GCP Secret Manager, AWS Secrets Manager, HashiCorp Vault) drop in without forking.

The framework's existing `_llm._get_key()` function (env vars → macOS Keychain → `~/.config/atomic_agents/keys.json`) has been **superseded**: the cascade now lives in `FilesystemSecretBackend`, and `_get_key()` is a thin redirect wrapper. All six live callers were rewired in PR 1.

---

## Module layout

```
atomic_agents/secret_backend/
├── __init__.py        # registry: register_secret_backend /
│                      # get_secret_backend / list_secret_backends +
│                      # get_default_secret_backend factory +
│                      # _redact_for_error_message helper
├── types.py           # canonical types: SecretRef, SecretCapabilities
├── backend.py         # SecretBackend Protocol contract + exception classes +
│                      # _validate_key helper
└── filesystem.py      # FilesystemSecretBackend reference implementation
```

Package name `secret_backend/` (not `secrets/` which would shadow the Python stdlib `secrets` module for cryptographic random generation). Mirrors `mcp_registry/` naming convention (not `mcp/`).

---

## WritePolicy applicability

SecretBackend is **read-only**. The framework never writes secrets; secret rotation is orchestrated outside the framework (in the secret store itself). This Protocol does not include a WritePolicy for the same reason PolicyBackend (spec/32) does not: a backend whose write path is out-of-scope has no write policy to govern. The read-only contract is enforced structurally — there is no write method on the Protocol surface.

---

## Deliberately absent from v1.0

**`list_secrets()`** — listing available secret keys is an enumeration surface that creates a side channel. Add `list_secrets()` to the Protocol only when an `atomic-agents secrets list` CLI subcommand has a shipped spec that documents the disclosure model.

**`scope()` method** — secrets are flat per-deployment. There is no per-agent namespace for credentials. This is a deliberate design choice: credentials (API keys) are machine-scoped, not agent-scoped.

**`get_all()`** — never returns plaintext values in bulk. Security constraint, not a missing feature.

**`supports_versioning`** in `SecretCapabilities` — rotation (re-keying by the secret store) and versioning (read-back of prior secret values) are distinct capabilities. Add `supports_versioning` to the Protocol only when the first backend that can read prior versions ships (e.g., a GCP Secret Manager backend with version pinning). Do not add it before then.

---

## Protocol surface

```python
class SecretBackend(Protocol):
    @property
    def capabilities(self) -> SecretCapabilities: ...
    @property
    def backend_id(self) -> str: ...

    def get(self, key: str) -> str: ...
    def get_optional(self, key: str) -> str | None: ...
    def has(self, key: str) -> bool: ...
    def locate(self, key: str) -> SecretRef | None: ...
    def close(self) -> None: ...
```

All call sites use property syntax: `backend.capabilities.supports_rotation` NOT `backend.capabilities().supports_rotation` (spec/36 Decision precedent).

---

## Canonical types

### `SecretRef`

```python
@dataclass(frozen=True)
class SecretRef:
    key: str      # key name (e.g., "ANTHROPIC_API_KEY")
    source: str   # source label (e.g., "env:ANTHROPIC_API_KEY"); NEVER the value
    present: bool # always True for refs returned by locate() when key exists
```

`SecretRef.source` MUST NOT contain credential values — it names the source location only (MUST 5). Examples: `"env:ANTHROPIC_API_KEY"`, `"keychain:atomic-agents-anthropic"`, `"config:~/.config/atomic_agents/keys.json:anthropic"`.

### `SecretCapabilities`

```python
@dataclass(frozen=True)
class SecretCapabilities:
    supports_rotation: bool       # each get() re-resolves from live sources
    supports_audit_logging: bool  # durable audit log per get() call
    persists_plaintext: bool      # credentials stored in a portable file
```

### Exception hierarchy

```
AtomicAgentsError
└── SecretError                  # base for all secret backend errors
    ├── SecretNotFound           # key absent or all sources returned empty
    └── SecretBackendNotRegistered  # backend_id not in registry
```

Callers catching errors at the `_get_key()` redirect boundary MUST catch `SecretError` (the base class), not `SecretNotFound` specifically, per the fail-closed wrapper lesson (MEMORY.md `feedback_fail_closed_catches_base_error_class`).

---

## Key charset constraint (MUST 1)

All `key` parameters MUST match `[A-Z0-9_]+` (POSIX env-var charset — the superset that covers all current env-var sources). Backends MUST validate at the API boundary BEFORE any backend access and raise `ValueError` on any character outside that set, including `.`, `/`, `\`, control characters, and empty string. This prevents path-traversal attacks via the key name. Mirrors MCPServerRegistryBackend MUST 1.

---

## Lookup order (FilesystemSecretBackend)

Fixed, non-operator-configurable. New sources are appended at the bottom; existing order is preserved for backward compatibility.

1. **Environment variables** — the primary env var for the key plus all known aliases (e.g., `ANTHROPIC_API_KEY` also probes `ATOMIC_AGENTS_ANTHROPIC_KEY`). First non-empty wins.
2. **macOS Keychain** (Darwin only) — `security find-generic-password -a <USER> -s <keychain_name> -w` (account defaults to `$USER`; scopes the lookup to the current user's keychain account). Timeout: 5 seconds. Skipped silently on non-Darwin platforms.
3. **`~/.config/atomic_agents/keys.json`** — machine-scoped JSON dict keyed by `config_key` (e.g., `"anthropic"`). If the file exists but fails to parse, logs a WARNING and falls through.

**Machine-scoped paths only (MUST 2).** FilesystemSecretBackend MUST NOT use any path relative to the agent vault root for secret storage. Credential files MUST resolve to machine-scoped locations only (e.g., `~/.config/atomic_agents/keys.json`). Vault-relative paths (e.g., `<agent_root>/secrets/`) are prohibited because vault portability would carry credentials. This protects both shapes: home users whose vault is a local folder and org users whose vault is in a shared repo or S3-backed store.

**`.env` file support** is explicitly deferred. When `.env` parsing ships (tracked issue [#361](https://github.com/dep0we/atomic-agents-stack/issues/361)), the `.env` file MUST be machine-scoped (e.g., `~/.config/atomic_agents/.env` or `$HOME/.env`), NOT vault-relative.

---

## Secrecy clauses (MUSTs 4-6)

These are three SEPARATE MUSTs because they gate three separate leak sites:

**MUST 4** — `get()` and `get_optional()` MUST NOT include the resolved secret value in any exception message. `SecretNotFound` names the key and the sources searched, never the value.

**MUST 5** — `locate()` MUST NOT include the resolved secret value in the returned `SecretRef`. The `source` field names the source location only (e.g., `"env:ANTHROPIC_API_KEY"`), never the resolved value.

**MUST 6** — No CLI subcommand (`check`, `which`, `validate`) MUST print the resolved secret value to stdout, stderr, or any log sink at any verbosity level. Allowed output: key name, source label, present/absent status, validation pass/fail.

---

## Empty-string semantics (MUST 7)

An empty-string or whitespace-only value from any source is treated as absent. `get()` raises `SecretNotFound` on empty-string resolution; `get_optional()` returns `None`. This preserves the original `_get_key()` `if val:` semantics and prevents operators who accidentally set `ANTHROPIC_API_KEY=` from getting a blank API key passed to the SDK.

---

## `has()` implementation contract (MUST 8)

`has(key)` MUST be implemented strictly as `return self.get_optional(key) is not None`. Backends MUST NOT implement a separate resolution ladder for `has()`. This guarantees `has()` and `get()` can never disagree about a key's presence (split-brain prevention).

---

## Capability advertisement (MUST 3)

`capabilities` MUST be a `@property` (not a plain method). `SecretCapabilities` MUST be `@dataclass(frozen=True)`. Conformance tests assert:
- `isinstance(type(backend).capabilities, property)` — not accidentally a method
- `FrozenInstanceError` when a field is assigned — immutability enforced

FilesystemSecretBackend capabilities:
- `supports_rotation=True` — each `get()` call re-resolves from live sources (no caching)
- `supports_audit_logging=False` — no durable audit trail
- `persists_plaintext=False` — credential sources (keys.json) are machine-scoped, not vault-portable; no plaintext credential travels with the agent vault

---

## Thread safety (implementation guidance; covered by Implementer Contract MUST 9)

Backend implementations MUST NOT cache resolved values in instance state. Each call to `get()` MUST re-resolve from the live sources. This ensures rotation awareness (a key rotated at the secret store becomes visible on the next `get()` call without a process restart) and thread safety for `helper_call_parallel` worker threads. This is the behavioral rationale behind Implementer Contract MUST 9 (No caching); it is not a separate numbered MUST.

---

## Doctor dual-probe requirement (implementation guidance; not a conformance MUST)

`doctor.check_secret_backend()` probes backend construction and capability advertisement. Key resolution is validated by `check_provider_keys()` which routes through `_get_key()` → backend. Both checks must pass for the "doctor verdict and runtime behaviour can never disagree" invariant (CLAUDE.md methodology §"Self-dogfood the work as it ships"). This is operational guidance for the doctor check implementation, not an additional numbered MUST in the Implementer Contract.

---

## Operator surface

| Env var | Default | Notes |
|---------|---------|-------|
| `ATOMIC_AGENTS_SECRET_BACKEND` | `"filesystem"` | Backend id. Empty/whitespace → filesystem. |
| `ATOMIC_AGENTS_SECRET_BACKEND_URL` | (unset) | Reserved for GCP backend (PR 2). Factory raises a clear error if `gcp` is set without the URL. |

Known backend ids: `"filesystem"` (shipped PR 1), `"gcp"` (PR 2).

The `_redact_for_error_message()` helper sanitizes `ATOMIC_AGENTS_SECRET_BACKEND` and `ATOMIC_AGENTS_SECRET_BACKEND_URL` before echoing in error messages (URL credential heuristic, matching mcp_registry, logs, corpus factories).

---

## Implementer Contract

Implementations satisfy the Protocol structurally (do not subclass). **The nine MUSTs below are the authoritative conformance index.** All MUST references in the prose sections above map 1:1 to this list. Prose sections that provide rationale or implementation guidance (Thread safety, Doctor dual-probe) are labeled accordingly and do not add additional numbered MUSTs.

1. **Key charset** — validate `[A-Z0-9_]+` at the API boundary before any backend access; raise `ValueError` on invalid keys.
2. **Machine-scoped sources** — MUST NOT resolve any secret from a path relative to the agent vault root; all filesystem sources MUST resolve relative to `os.path.expanduser("~")` or an absolute machine path.
3. **Capability honesty** — `capabilities` fields are a contract not a hint; conformance tests assert claim-vs-behavior parity.
4. **No value in exceptions** — `get()` / `get_optional()` exception messages MUST NOT contain the resolved secret value; only the key name and source names.
5. **No value in `locate()` output** — `SecretRef.source` MUST NOT contain the resolved credential value.
6. **No value in CLI output** — `check`, `which`, `validate` MUST NOT print the resolved value to any output stream.
7. **Empty-string as absent** — empty/whitespace values MUST be treated as absent; `get()` raises `SecretNotFound`, `get_optional()` returns `None`.
8. **`has()` delegation** — MUST be `return self.get_optional(key) is not None`; no separate resolution ladder.
9. **No caching** — each `get()` call MUST re-resolve from live sources; no instance-level value cache.
