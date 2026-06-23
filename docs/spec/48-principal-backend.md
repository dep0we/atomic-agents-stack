# spec/48 — PrincipalBackend Protocol

**Status: LOCKED** | Introduced: v2.0.0 | Issue: #556

---

## Purpose

`PrincipalBackend` is the twenty-first backend Protocol added to the framework. It closes the identity-derivation gap: prior to this spec, agent.call() had no structured way to carry a caller's identity, and conversation isolation (spec/47) defaulted to `LOCAL_PRINCIPAL` for every caller.

This Protocol maps already-verified caller claims to a `Principal` dataclass and wires that principal into the HARD-REFUSE gate, conversation isolation, and the JSONL audit trail.

**Key design constraint:** PrincipalBackend is an identity-derivation Protocol, NOT a storage backend. It derives a `Principal` from a `Mapping` of already-verified claims. It never verifies tokens itself (the perimeter — IAP, OIDC middleware — has already done that). It never touches the filesystem.

---

## What it replaces

Before this spec, `agent.call()` hardcoded `LOCAL_PRINCIPAL` for every caller in every context (see `conversation/types.py`). The serve layer had no principal-threading path. Conversation directories (spec/47) were isolated per `LOCAL_PRINCIPAL.identifier` only, meaning all HTTP callers shared one conversation namespace.

---

## The Principal dataclass

`Principal` is defined in `atomic_agents/conversation/types.py` (the conversation layer owns it — spec/47). PrincipalBackend re-exports it; it NEVER redefines it.

```python
@dataclass(frozen=True)
class Principal:
    identifier: str           # storage key (64-char sha256 hex for static_claims)
    derivation_source: str    # which backend derived this principal
    is_verified: bool         # True = perimeter-verified; False = fail-closed sentinel
```

The canonical home-user singleton:

```python
LOCAL_PRINCIPAL = Principal(
    identifier="local",
    derivation_source="local",
    is_verified=True,
)
```

---

## PrincipalBackend Protocol

```python
from typing import Mapping, Protocol, runtime_checkable
from .types import Principal, PrincipalCapabilities

@runtime_checkable
class PrincipalBackend(Protocol):
    @property
    def backend_id(self) -> str:
        """Unique lowercase identifier for this backend (e.g. 'local', 'static_claims')."""
        ...

    def derive_principal(self, verified_claims: Mapping) -> Principal:
        """Map already-verified caller claims to a Principal.

        MUST NOT raise for absent or malformed claims (MUST 1).
        MUST NOT verify tokens (the perimeter has already done that) (MUST 2).
        Absent or malformed claims MUST return Principal(is_verified=False) (MUST 3).
        """
        ...

    def capabilities(self) -> PrincipalCapabilities:
        """Return a PrincipalCapabilities snapshot describing this backend's behavior."""
        ...
```

### PrincipalCapabilities

```python
@dataclass(frozen=True)
class PrincipalCapabilities:
    backend_id: str                           # required, no default
    is_local_only: bool = False               # True = LocalPrincipalBackend (home-user)
    supports_token_verification: bool = False # True = backend validates tokens itself
    produces_verified_principals: bool = False # True = CAN produce is_verified=True
```

**spec/40 Export Exemption:** `PrincipalCapabilities` carries NO `supports_canonical_export` field. PrincipalBackend is an identity-derivation Protocol, not a state store. There is no snapshot to export.

**WritePolicy NOT applicable:** PrincipalBackend has no write path. WritePolicy (spec/22) does not apply.

---

## Storage-key derivation (MUST 7)

`StaticClaimsPrincipalBackend` derives the principal identifier as:

```python
# provider and sub are stripped of surrounding ASCII whitespace ONLY (MUST 7).
# _ASCII_WS = " \t\n\r\f\v" — deliberately NOT str.strip()'s Unicode set.
sha256(f'{provider.strip(_ASCII_WS)}\x00{sub.strip(_ASCII_WS)}'.encode('utf-8')).hexdigest()
```

The surrounding-whitespace strip is a deliberate normalization: an accidental trailing space (`'google '` vs `'google'`) MUST NOT split one principal across two conversation directories. It operates only on leading/trailing whitespace; interior characters are preserved. **The strip set is ASCII whitespace ONLY (`_ASCII_WS`), NOT Python's `str.strip()` default** — `str.strip()` also removes Unicode whitespace (NBSP `\xa0`, the ideographic space `　`, etc.), which would let two DISTINCT subjects differing only by a leading Unicode space collapse onto one identifier (a MUST 11 non-reassignability violation). The code strips only the ASCII set so distinct subjects never collide.

The NUL byte (`\x00`) separator prevents prefix-collision attacks: without it, `('google', 'user')` and `('goo', 'gleuser')` would both produce the string `"googleuser"` and hash to the same identifier, silently aliasing two different principals onto one conversation directory.

The resulting identifier is a 64-character lowercase hexdigest. It is used directly as the conversation directory component by `FilesystemConversationBackend` (spec/47).

---

## Implementer Contract (12 MUSTs)

This contract carries the 8-MUST PersonaBackend base (spec/33: fail-closed construction, charset/input validation, capability honesty, write-case handling, redaction, storage isolation, snapshot determinism, backend_id stability — adapted to an identity-derivation Protocol with no write path) PLUS four named security-boundary MUSTs that this Protocol's trust boundary introduces. The base MUSTs occupy positions 1, 3, 4, 5, 6, 7, 8, and 9; the four trust-boundary MUSTs are interleaved at positions 2, 10, 11, and 12: **never-trust-raw-header / never-re-verify** (MUST 2), **fail-closed-on-unverified** (MUST 10), **storage-key stability + non-reassignability** (MUST 11), and **is_verified honesty** (MUST 12). They are placed where they read most naturally in sequence rather than as a contiguous trailing block; the security-boundary MUSTs are named explicitly so the conformance suite asserts them and so a future reviewer cannot fold them away.

**MUST 1 — Fail-closed on absent/malformed claims:**
`derive_principal()` MUST NOT raise for absent or malformed claims. When `provider` or `sub` is absent, empty (after strip), or not a string, return `Principal(is_verified=False)`. Never raise `PrincipalBackendError` for malformed input — that exception is reserved for unrecoverable backend I/O faults.

**MUST 2 — Never verify tokens / never trust a raw header / never re-verify:**
`derive_principal()` MUST NOT perform cryptographic token verification, and MUST NOT treat a raw, client-settable transport value (e.g. an HTTP header) as a verified claim by fiat. The perimeter (IAP, OIDC middleware) has already authenticated the caller; claims arriving here are ALREADY verified by that perimeter, and the backend re-verifies nothing. A backend that verifies tokens itself violates the single-responsibility contract and creates a double-verification race. Corollary for the serve seam: the serve layer MUST NOT promote a raw identity header into a verified claim without an explicit operator opt-in declaring that header perimeter-authenticated (see "Serve layer HYBRID flow" below) — by default a present-but-untrusted identity yields `Principal(is_verified=False)`.

**MUST 3 — Fail-closed sentinel (is_verified=False):**
Absent or malformed claims MUST return `Principal(is_verified=False)`, never a sentinel that circumvents the HARD-REFUSE gate (MUST 10). The fail-closed principal MUST have `derivation_source` set to this backend's `backend_id`.

**MUST 4 — Side-effect-free construction:**
`PrincipalBackend()` MUST be constructable with no arguments and no filesystem I/O, no network calls, no subprocess invocation. Construction failure (e.g., from a misconfigured environment) MUST raise `PrincipalBackendError` (not a generic exception). StaticClaimsPrincipalBackend takes NO constructor arguments — the claims `Mapping` is a RUNTIME argument to `derive_principal()`, never a constructor argument.

**MUST 5 — backend_id stability:**
`backend_id` MUST return the same lowercase non-empty string value on every call from any instance of this class. backend_id is the registry key; instability breaks registration and doctor probes.

**MUST 6 — capabilities() honesty:**
`capabilities().produces_verified_principals` MUST be True only if the backend can actually produce `Principal(is_verified=True)`. `capabilities().is_local_only` MUST be True only for backends that always return `LOCAL_PRINCIPAL`. `capabilities().supports_token_verification` MUST be True only if the backend performs its own token validation (reserved for future backends; both reference impls return False).

**MUST 7 — Storage-key encoding (for hash-key backends):**
Backends that derive a storage key from `(provider, sub)` MUST use exactly `sha256(f'{provider.strip(_ASCII_WS)}\x00{sub.strip(_ASCII_WS)}'.encode('utf-8')).hexdigest()` — the `provider`/`sub` values are stripped of surrounding **ASCII** whitespace (`_ASCII_WS = " \t\n\r\f\v"`) inside the encoding, NOT Python's `str.strip()` default Unicode set. Leading/trailing ASCII whitespace is normalized away so an accidental trailing space cannot split a principal's namespace, while distinct subjects differing only by a leading Unicode space (NBSP, ideographic space) MUST NOT collide onto one identifier (a MUST 11 non-reassignability violation). NUL byte separator is mandatory. No other separator, no concatenation without separator.

**MUST 8 — PrincipalCapabilities has no supports_canonical_export field:**
Per spec/40 Export Exemption: PrincipalCapabilities MUST NOT carry a `supports_canonical_export` field. PrincipalBackend is identity-derivation, not state storage.

**MUST 9 — NEVER redefine Principal or LOCAL_PRINCIPAL:**
Any backend package MUST re-export `Principal` and `LOCAL_PRINCIPAL` from `atomic_agents.conversation.types`. Redefining them (even with identical fields) creates two distinct types and breaks `is` identity checks throughout the framework (notably the `principal is not LOCAL_PRINCIPAL` JSONL stamp guard in agent.call()).

**MUST 10 — Fail-closed at the HARD-REFUSE gate:**
A `Principal` with `is_verified=False` MUST be refused by `agent.call()` whenever a `conversation_id` is supplied, BEFORE any storage I/O (including the idempotency dedup lookup), BEFORE lock acquisition, BEFORE the cost/spend gate, and BEFORE any LLM call. The gate keys EXCLUSIVELY on the `is_verified` boolean — never on object identity with `LOCAL_PRINCIPAL`, never on `derivation_source` (which is descriptive audit metadata, not a gate input). A fabricated `Principal(identifier='local', derivation_source='local', is_verified=False)` MUST still be refused. See "HARD-REFUSE gate (agent.call() wiring)".

**MUST 11 — Storage-key stability + non-reassignability:**
The derived `identifier` for a given verified caller MUST be STABLE — the SAME `(provider, sub)` pair MUST always map to the SAME identifier, across process restarts and re-instantiation, so a returning principal's prior conversation files remain readable. The identifier MUST be NON-REASSIGNABLE — DISTINCT `(provider, sub)` pairs MUST NEVER collide to the same identifier (the NUL separator of MUST 7 enforces this against prefix aliasing: without it `('google','user')` and `('goo','gleuser')` would alias; the ASCII-only whitespace strip of MUST 7 enforces it against Unicode-whitespace aliasing: two subjects differing only by a leading NBSP must not collapse). Likewise the serve seam MUST hash the FULL subject value, not a truncated audit copy — two distinct subjects sharing a long common prefix MUST NOT collide onto one identifier. Corollary for the serve seam: the value passed as `sub` MUST be the perimeter's STABLE subject id (e.g. IAP's `X-Goog-Authenticated-User-ID`), NOT a rotating signed token (e.g. a JWT assertion that is re-minted on refresh) — hashing a rotating token produces a new namespace on every refresh and violates stability.

**MUST 12 — is_verified honesty:**
The `is_verified` flag a backend stamps MUST reflect the actual provenance of the claims. A backend MUST set `is_verified=True` ONLY for claims it received as already-perimeter-verified (the contract of `derive_principal`'s input). It MUST NOT set `is_verified=True` as a default, for empty/malformed claims, or to "make the gate pass". Because the HARD-REFUSE gate (MUST 10) keys solely on this boolean, a dishonest `is_verified` is a direct authorization bypass.

---

## HARD-REFUSE gate (agent.call() wiring)

Placement in `agent.call()`:

1. **← HARD-REFUSE gate fires here** (BEFORE any storage I/O)
2. Dedup COMPLETED short-circuit (idempotency_key lookup) — cached results serve ONLY to callers that already passed the gate
3. Lock acquisition
4. Cost gate / guardrail check
5. `begin()` idempotency lease
6. LLM call

**The gate MUST fire BEFORE the idempotency dedup lookup (MUST 10).** An unverified caller that supplies BOTH a `conversation_id` AND an `idempotency_key` mapping to a COMPLETED ledger record would otherwise be served the prior run's cached `result_ref` + `replayed_run_id` without the principal check ever firing — letting a non-local caller replay/confirm another principal's completed conversation-bearing run by guessing or replaying a caller-supplied key. Because `idempotency_key` is a caller-supplied header at the serve layer, this is a real cross-principal disclosure if the gate runs after the dedup short-circuit. Enforcing identity FIRST closes it and uniformly protects both the Phase-1 lookup-COMPLETED and the Phase-2 begin-COMPLETED dedup-serve sites.

Gate condition (exact):

```python
if conversation_id is not None and not principal.is_verified:
    raise UnverifiedPrincipalConversationAccess(
        f"Principal not verified for conversation {conversation_id!r}",
        conversation_id=conversation_id,
        principal_id=principal.identifier,
    )
```

**Gate keys on `is_verified` boolean ONLY — never on object identity with `LOCAL_PRINCIPAL`.**

A fabricated `Principal(identifier='local', derivation_source='local', is_verified=False)` MUST be refused even though its identifier matches `LOCAL_PRINCIPAL.identifier`.

Single-shot calls (`conversation_id=None`) with unverified principals are NOT refused by this gate. The gate only protects conversation namespaces from unauthorized access.

### Audit record

On refuse, a best-effort JSONL record is written with `status: "principal_not_verified"`. On the ok path, `principal_id` is stamped only when `principal is not LOCAL_PRINCIPAL` (backward-compatible: existing home-user logs have no `principal_id` field).

---

## Exception hierarchy

```
AtomicAgentsError
├── PrincipalBackendError
│     Raised by a PrincipalBackend on unrecoverable backend I/O fault.
│     NOT raised for absent/malformed claims (those return is_verified=False).
│     LocalPrincipalBackend and StaticClaimsPrincipalBackend NEVER raise this.
│
└── UnverifiedPrincipalConversationAccess
      Raised by the HARD-REFUSE gate in agent.call().
      Gate condition: conversation_id is not None and not principal.is_verified
      NOT a backend fault. Serve → HTTP 401.
      Attributes: conversation_id, principal_id
```

---

## Reference implementations

### LocalPrincipalBackend (`backend_id = "local"`)

The home-user default. Ignores all claims entirely and always returns `LOCAL_PRINCIPAL`. Never raises. Side-effect-free construction. `is_local_only=True`.

When the doctor checks this backend, the negative probe (derive absent claims → is_verified=False) is SKIPPED because LocalPrincipalBackend is designed to always return is_verified=True.

### StaticClaimsPrincipalBackend (`backend_id = "static_claims"`)

The serve-layer multi-user reference impl. Maps `{provider, sub}` claims to a 64-char sha256 hex identifier using the NUL-separator encoding (MUST 7). Valid claims → `Principal(is_verified=True)`. Absent or malformed claims → `Principal(identifier='anonymous', derivation_source='static_claims', is_verified=False)`.

`is_local_only=False`. `produces_verified_principals=True`. `supports_token_verification=False`.

---

## Registry

```python
from atomic_agents.principal import (
    get_default_principal_backend,  # → LocalPrincipalBackend() when env absent
    get_principal_backend,          # → class by backend_id
    list_principal_backends,        # → list[str]
    register_principal_backend,     # extend the registry
    unregister_principal_backend,   # test isolation
)
```

**`ATOMIC_AGENTS_PRINCIPAL_BACKEND` env var:**
- Absent or empty → returns `LocalPrincipalBackend()` (home-user default)
- Set to a known backend_id → returns an instance of that backend
- Set to an unknown backend_id → raises `BackendNotRegistered` LOUDLY (does NOT silently degrade to LocalPrincipalBackend — a misconfigured org deployment must fail fast)

This differs from ConversationBackend (spec/47) which silently degrades. PrincipalBackend fails loud because silently using LocalPrincipalBackend in an org context means everyone shares one conversation namespace.

---

## Serve layer HYBRID flow (spec/48 + spec/37)

The serve layer never verifies tokens itself. The perimeter (Google IAP, OIDC middleware) already did that by the time the request arrives. But the serve layer also MUST NOT trust the raw identity header by fiat (MUST 2): a raw header is client-settable, and stamping it `is_verified=True` unconditionally would let any client forge a verified Principal and read another principal's conversation turns (CWE-290 spoofing). Treating the header as a verified claim is therefore an explicit OPT-IN, OFF by default (fail-closed).

**Opt-in gate (`identity_is_perimeter_verified`):** the serve layer builds `verified_claims` from the identity header ONLY when the operator has declared the header perimeter-authenticated (serve.md `## Identity Is Perimeter Verified` section, or `ATOMIC_AGENTS_SERVE_IDENTITY_PERIMETER_VERIFIED` env var) AND the bind is non-loopback (a loopback dev server has no perimeter in front of it). Both conditions are ANDed. Default OFF.

Three cases:

- **caller_identity is None AND perimeter-trust NOT enabled** (home-user / no identity header, no perimeter) → `LOCAL_PRINCIPAL` (is_verified=True, single-user). The `not identity_perimeter_verified` conjunct is SECURITY-LOAD-BEARING: in a perimeter-trusted (non-loopback, multi-tenant) deployment, a request that OMITS the identity header MUST NOT collapse to the shared verified `local` namespace (a fail-open). When perimeter-trust is ON and the header is absent, this falls through to the fail-closed UNVERIFIED branch below.
- **caller_identity present but perimeter-trust NOT enabled** (default, or loopback bind), **OR perimeter-trust ON but the identity header is absent** → a fail-closed `Principal(is_verified=False)` is produced directly (NOT via the registered backend, so the posture does not depend on which backend is registered) → a `conversation_id` caller is HARD-REFUSED. The serve layer does NOT silently fall back to `LOCAL_PRINCIPAL`, which would wrongly pass the gate.
- **caller_identity present AND perimeter-trust enabled (non-loopback)** → `agent.principal_backend.derive_principal(verified_claims)` — UNLESS the registered backend is `is_local_only` (see the cross-tenant misconfiguration guard below), in which case a fail-closed `Principal(is_verified=False, derivation_source='serve_local_backend_misconfig')` is minted instead.

**Cross-tenant collapse guard (`is_local_only` backend under perimeter-trust):** if perimeter-trust is enabled but the registered PrincipalBackend reports `capabilities().is_local_only` (e.g. the operator turned on `identity_is_perimeter_verified` but forgot to set `ATOMIC_AGENTS_PRINCIPAL_BACKEND`, leaving the default `LocalPrincipalBackend`), `derive_principal()` would IGNORE the verified claims and return `LOCAL_PRINCIPAL` (is_verified=True) for EVERY distinct caller — collapsing all tenants onto the shared `local` conversation namespace and silently mixing their turns. The runner refuses: it mints `Principal(identifier='unverified', derivation_source='serve_local_backend_misconfig', is_verified=False)` so the agent.call() HARD-REFUSE gate fires instead of leaking. (`doctor.check_principal_backend` stays advisory; this is the load-bearing runtime guard.)

In `serve/_runner.py` (`_call()` closure, after AtomicAgent construction):

```python
if caller_identity is None and not identity_perimeter_verified:
    principal = LOCAL_PRINCIPAL
elif identity_perimeter_verified and verified_claims is not None:
    if agent.principal_backend.capabilities().is_local_only:
        # Cross-tenant collapse guard: a local-only backend cannot honor a
        # perimeter-verified multi-tenant claim. Fail closed.
        principal = Principal(
            identifier="unverified",
            derivation_source="serve_local_backend_misconfig",
            is_verified=False,
        )
    else:
        principal = agent.principal_backend.derive_principal(verified_claims)
else:
    # Header present but perimeter not trusted, OR perimeter trusted but header
    # absent: refuse to mint a verified principal.
    principal = Principal(
        identifier="unverified",
        derivation_source="serve_untrusted_perimeter",
        is_verified=False,
    )

response = agent.call(..., principal=principal, conversation_id=conversation_id)
```

In `serve/_app.py`:

```python
# _identity_perimeter_verified = identity_is_perimeter_verified AND not is_loopback(bind_host)
# raw_identity = request.headers.get(identity_header_name)  # FULL, untruncated
# caller_identity = raw_identity[:512]  # capped for the AUDIT log ONLY
verified_claims: dict | None = None
if raw_identity is not None and _identity_perimeter_verified:
    # SECURITY (MUST 11): 'sub' MUST be the FULL raw_identity, NOT the 512-char
    # truncated caller_identity. Reusing the truncated audit value as the authz
    # key would collide two distinct subjects sharing the first 512 chars onto
    # one storage identifier — a cross-principal conversation-access break.
    verified_claims = {"provider": "http", "sub": raw_identity}

# UnverifiedPrincipalConversationAccess -> HTTP 401
except UnverifiedPrincipalConversationAccess as e:
    return JSONResponse(
        {"status": "error", "error": "Principal not verified for conversation access",
         "conversation_id": e.conversation_id, "principal_id": e.principal_id},
        status_code=401,
    )
```

**Stable subject for `sub` (MUST 11):** the value passed as `sub` becomes the storage-key input. Operators MUST configure a STABLE subject header — e.g. IAP's `X-Goog-Authenticated-User-ID` (`accounts.google.com:<numeric-sub>`) — NOT the default rotating signed JWT assertion (`X-Goog-IAP-JWT-Assertion`). A rotating token hashes to a new namespace on every refresh, orphaning prior conversation files.

`conversation_id` is extracted from the request body JSON (same validation as `idempotency_key`: bare component, no separators, no control chars, max 512 chars).

---

## Clarifying note for spec/47 (ConversationBackend)

> **NOTE (non-normative, added spec/48):** When the serve layer threads a non-local
> Principal to agent.call(), the `principal` arg replaces the hardcoded `LOCAL_PRINCIPAL`
> that was previously passed to `conversation_backend.load_turns()` and
> `conversation_backend.write_turn()`. This is the mechanism by which conversation
> isolation (spec/47) becomes multi-principal in a served deployment. No normative
> rule in spec/47 changes; the `Principal` parameter type in `load_turns` and
> `write_turn` was already present in spec/47 to support exactly this extension.

---

## Doctor check

`check_principal_backend()` in `atomic_agents/doctor.py`:

1. **Construction probe:** `get_default_principal_backend()` must not raise.
2. **Positive probe:** `derive_principal({"provider": "test", "sub": "probe"})` must return a Principal.
3. **Negative probe (skipped when `is_local_only=True`):** `derive_principal({})` must return `Principal(is_verified=False)`. Not applicable to LocalPrincipalBackend (it always returns LOCAL_PRINCIPAL with is_verified=True by design).
4. **capabilities() honesty check:** `capabilities().produces_verified_principals` SHOULD be True; doctor emits WARN (not FAIL) when it is False. A backend that never produces a verified principal simply refuses every conversation caller — a legitimate (if niche) configuration, so this is advisory.

---

## What this does NOT cover

- Token verification (perimeter responsibility)
- Role-based authorization (a future Protocol — PrincipalBackend only answers "who is this", not "what can they do")
- pgvector or database-backed principal storage (both reference impls are stateless in-memory derivers)
- Multi-factor claims (the `Mapping` interface is intentionally open; future backends can read additional claim keys)

---

## Deferred (follow-up issues)

- **PR 3:** A raw-token (pyjwt/jose) self-verifying PrincipalBackend + the `[auth]` extra; full serve multi-tenant integration (`identity_is_perimeter_verified` end-to-end with a real perimeter)
- **PR 4:** Role-based authorization layer (what principals can do, not just who they are)
- **PR 5:** Database-backed principal store for audit trail (optional; filesystem has no state to persist)
