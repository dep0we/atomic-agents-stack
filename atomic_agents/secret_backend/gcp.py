"""GCPSecretManagerBackend -- GCP Secret Manager reference implementation of SecretBackend (spec/38).

Resolves credentials from GCP Secret Manager via the google-cloud-secret-manager SDK.
Every ``get()`` call hits Secret Manager live (no caching) so that secret rotation is
immediately visible without a process restart (spec/38 MUST 9 + supports_rotation=True).

Key-to-secret-name mapping: ``key.lower().replace('_', '-')``
    e.g. ``ANTHROPIC_API_KEY`` -> ``anthropic-api-key``

GCP secret name charset: ``[a-zA-Z0-9_-]``, max 255 chars (verified against the
"Creating and accessing secrets" guide, which states: "A secret name can contain
uppercase and lowercase letters, numerals, hyphens, and underscores. The maximum
allowed length for a name is 255 characters"). The mapping above stays within that
charset. Any secret ID that GCP's API rejects surfaces as a get()-time GCP error
(mapped to SecretError), not as a mapping error here. Reference:
https://cloud.google.com/secret-manager/docs/creating-and-accessing-secrets

Resource path format: ``projects/<project_id>/secrets/<secret_name>/versions/latest``
The ``_url`` constructor arg provides the ``projects/<project_id>/secrets`` prefix.
Reference: https://cloud.google.com/secret-manager/docs/access-secret-version

Authentication: Application Default Credentials (ADC) or workload identity.
The SecretManagerServiceClient resolves credentials automatically.
Reference: https://cloud.google.com/docs/authentication/application-default-credentials

No explicit credentials are plumbed in this class -- ADC resolution happens inside
the SDK client on first RPC.  Explicit credentials are deferred to a future PR.

Thread safety: ``_client_lock`` guards lazy client construction and idempotent close().
Two concurrent first-calls see at most one channel opened (double-checked locking);
close() is safe to call from any thread.

Audit logging note (spec/38 MUST 3 / capability honesty): GCP Cloud Audit Logs for
Secret Manager ``accessSecretVersion`` calls are NOT automatic -- they require the
operator to enable "Data Access" audit log policies in IAM. The framework does not
configure, read, or guarantee these logs. Therefore ``supports_audit_logging=False``
is the honest capability value.
Reference: https://cloud.google.com/secret-manager/docs/audit-logging
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .backend import SecretError, SecretNotFound, _validate_key
from .types import SecretCapabilities, SecretRef

_logger = logging.getLogger(__name__)


class GCPSecretManagerBackend:
    """GCP Secret Manager implementation of the SecretBackend Protocol (spec/38).

    Reads credentials from GCP Secret Manager. Each ``get()`` call re-resolves
    the ``latest`` version live -- no instance-level value caching (MUST 9).

    Constructor is side-effect-free: import/presence check + URL parsing only.
    The SecretManagerServiceClient (and its gRPC channel) is constructed lazily
    on the first ``get()`` / ``get_optional()`` call via ``_get_client()``.

    Args:
        url: GCP Secret Manager URL prefix. Format: ``projects/<project_id>/secrets``
             (trailing slash is normalised away). Embedded in every resource path
             as ``{url}/{secret_name}/versions/latest``.

    Raises:
        ImportError: if ``google-cloud-secret-manager`` is not installed.
            Install: ``uv add 'atomic-agents-stack[gcp]'``
        ValueError: if the URL does not match ``projects/<id>/secrets`` format.
    """

    _BACKEND_ID = "gcp"

    # Capability advertisement (spec/38 MUST 3).
    # supports_rotation=True  -- every get() re-resolves the 'latest' version live;
    #     no caching, so a rotated secret is visible on the very next call (MUST 9).
    # supports_audit_logging=False -- GCP Cloud Audit Logs for accessSecretVersion
    #     are operator-configured (IAM Data Access policy), NOT automatic framework
    #     behaviour. The framework neither configures nor reads these logs.
    #     Reference: https://cloud.google.com/secret-manager/docs/audit-logging
    # persists_plaintext=False -- GCP Secret Manager stores AES-256 encrypted
    #     payloads in Google-managed KMS; no plaintext is stored in any framework
    #     file or vault directory.
    _CAPABILITIES = SecretCapabilities(
        supports_rotation=True,
        supports_audit_logging=False,
        persists_plaintext=False,
    )

    def __init__(self, url: str, *, _client: Any = None) -> None:
        """Construct a GCPSecretManagerBackend.

        Side-effect-free: performs only import checks and URL parsing.
        No network calls; no SDK client constructed here.

        Args:
            url: ``projects/<project_id>/secrets`` prefix (trailing slash stripped).
            _client: Test-only injectable mock client. When supplied, overrides
                lazy client construction in ``_get_client()``. Never used in
                production; allows conformance tests to run without live GCP.

        Raises:
            ImportError: if google-cloud-secret-manager is not installed.
            ValueError: if ``url`` does not match ``projects/<id>/secrets``.
        """
        # Import check -- surfaces a clean ImportError naming the [gcp] extra
        # if the SDK is absent. No SDK objects constructed here (side-effect-free).
        try:
            from google.cloud import secretmanager as _sm_pkg  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "GCPSecretManagerBackend requires the [gcp] extra. "
                "Install: uv add 'atomic-agents-stack[gcp]'"
            ) from e

        self._url: str = url.rstrip("/")

        # Validate URL shape: must be projects/<something>/secrets
        parts = self._url.split("/")
        if len(parts) != 3 or parts[0] != "projects" or parts[2] != "secrets":
            raise ValueError(
                f"GCPSecretManagerBackend url must match "
                f"'projects/<project_id>/secrets', got: {self._url!r}"
            )
        self._project_id: str = parts[1]

        # Threading lock guards lazy client construction and idempotent close().
        # Initialized here (not at first get()) -- threading.Lock() is not an SDK
        # call and carries no startup cost.
        self._client_lock = threading.Lock()

        # Lazily constructed SecretManagerServiceClient. None until first get().
        # Test-only _client seam: when supplied, _get_client() returns it directly.
        self._client: Any = _client
        self._client_is_injected: bool = _client is not None

    # ─── Backend identity ──────────────────────────────────────────────────────

    @property
    def capabilities(self) -> SecretCapabilities:
        """Return frozen capabilities. All call sites use property syntax."""
        return self._CAPABILITIES

    @property
    def backend_id(self) -> str:
        """Stable backend identifier: ``"gcp"``."""
        return self._BACKEND_ID

    # ─── Client lifecycle ──────────────────────────────────────────────────────

    def _get_client(self) -> Any:
        """Return the SecretManagerServiceClient, constructing it lazily if needed.

        Double-checked locking prevents a race between two concurrent first callers:
        both check ``_client is None`` before acquiring the lock; only one proceeds
        to construct. The other finds ``_client`` set after acquiring the lock and
        returns the already-built client.

        Test-injected clients (_client_is_injected=True) are returned directly
        without going through lazy construction.
        """
        if self._client_is_injected:
            return self._client

        if self._client is not None:
            return self._client

        with self._client_lock:
            if self._client is not None:
                return self._client
            # Deferred import -- only runs when the client is first needed.
            # The SDK was already confirmed importable in __init__.
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
            return self._client

    def close(self) -> None:
        """Release the gRPC channel held by the SecretManagerServiceClient.

        Idempotent: calling ``close()`` twice (or on a never-used instance) is safe.
        Mirrors ``HTTPMCPServerRegistryBackend.close()`` lock-guarded shape
        (http.py lines 1390-1400).
        """
        if self._client_is_injected:
            # Test-injected client; test code manages lifecycle.
            return
        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.transport.close()
                except Exception:
                    _logger.debug(
                        "GCPSecretManagerBackend.close(): error closing gRPC "
                        "transport (ignored)",
                        exc_info=True,
                    )
                self._client = None

    # ─── Key-to-secret-name mapping ────────────────────────────────────────────

    def _secret_name(self, key: str) -> str:
        """Map a POSIX env-var key to a GCP Secret Manager secret name.

        Mapping: ``key.lower().replace('_', '-')``
        e.g. ``ANTHROPIC_API_KEY`` -> ``anthropic-api-key``

        GCP secret name charset: [a-zA-Z0-9_-], max 255 chars (verified against
        the "Creating and accessing secrets" guide -- "A secret name can contain
        uppercase and lowercase letters, numerals, hyphens, and underscores. The
        maximum allowed length for a name is 255 characters"; no first-character
        restriction is documented there).
        Reference: https://cloud.google.com/secret-manager/docs/creating-and-accessing-secrets
        Keys validated by ``_validate_key`` (``^[A-Z0-9_]+$``) always map within
        this charset. The framework does not pre-validate GCP's own naming rules:
        any secret ID the GCP API rejects surfaces as a get()-time GCP error
        (mapped to SecretError via InvalidArgument), not as a mapping error here.
        """
        return key.lower().replace("_", "-")

    def _resource_name(self, key: str) -> str:
        """Build the full Secret Manager version resource name for a key.

        Format: ``projects/<project_id>/secrets/<secret_name>/versions/latest``
        Always resolves 'latest' -- version pinning is deferred (spec/38 deliberately
        absent from v1.0; supports_versioning not advertised in SecretCapabilities).
        Reference: https://cloud.google.com/secret-manager/docs/access-secret-version
        """
        secret_name = self._secret_name(key)
        return f"{self._url}/{secret_name}/versions/latest"

    # ─── Protocol methods ──────────────────────────────────────────────────────

    def get(self, key: str) -> str:
        """Return the resolved secret value for ``key``.

        Hits GCP Secret Manager live on every call (MUST 9: no caching).
        Always resolves the 'latest' version.

        Raises:
            ValueError: if key does not match [A-Z0-9_]+.
            SecretNotFound: if the secret does not exist in Secret Manager or
                if its value is empty/whitespace after stripping (MUST 7).
            SecretError: for all other GCP API errors (PermissionDenied,
                Unauthenticated, ResourceExhausted, etc.).

        MUST NOT include the resolved value in any exception message (MUST 4).
        """
        _validate_key(key)
        resource_name = self._resource_name(key)

        try:
            client = self._get_client()
            response = client.access_secret_version(name=resource_name)
        except ImportError as exc:
            # Defensive belt-and-suspenders: the SDK was confirmed importable in
            # __init__, so this only fires if a lazily-imported SDK submodule fails
            # to resolve during the RPC (e.g. a partial/corrupt install). Propagate
            # as SecretError with explicit cause chaining, matching the sibling
            # ``except Exception as exc`` block below.
            raise SecretError(
                f"GCP Secret Manager SDK unavailable when resolving key {key!r}. "
                f"Install: uv add 'atomic-agents-stack[gcp]'"
            ) from exc
        except Exception as exc:
            # Lazy import of GCP exceptions -- they are available because the
            # SDK import succeeded in __init__.
            try:
                import google.api_core.exceptions as _gcp_exc
            except ImportError:
                raise SecretError(
                    f"GCP Secret Manager error resolving key {key!r}: "
                    f"{type(exc).__name__}"
                ) from exc

            if isinstance(exc, _gcp_exc.NotFound):
                # MUST 4: message names the key and resource path only -- never val.
                raise SecretNotFound(
                    f"No secret found for key {key!r} in GCP Secret Manager at "
                    f"{self._url}/{self._secret_name(key)}"
                ) from exc
            elif isinstance(exc, _gcp_exc.GoogleAPICallError):
                # Maps PermissionDenied, Unauthenticated, ResourceExhausted, etc.
                # to SecretError. MUST NOT include the value; type name is safe.
                raise SecretError(
                    f"GCP Secret Manager error for key {key!r}: {type(exc).__name__}"
                ) from exc
            else:
                raise SecretError(
                    f"GCP Secret Manager unexpected error for key {key!r}: "
                    f"{type(exc).__name__}"
                ) from exc

        # Decode and strip. The .strip() is mandatory -- secrets created via
        # ``echo`` (not ``printf '%s'``) carry trailing newlines. Stripping here
        # ensures callers never receive a credential with a trailing newline
        # (which causes silent auth failures in provider SDKs).
        # MUST NOT log response.payload.data at any level (MUST 4 secrecy).
        # A non-UTF-8 payload (Secret Manager stores arbitrary bytes) raises a
        # UnicodeDecodeError whose message includes byte fragments; re-raise as
        # SecretError with no byte content so the get() contract ("SecretError
        # for all other errors") holds and no credential bytes leak (MUST 4).
        try:
            val = response.payload.data.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise SecretError(
                f"GCP secret for key {key!r} is not valid UTF-8"
            ) from None

        if not val:
            # MUST 7: empty/whitespace value treated as absent.
            # MUST 4: never include val in error message (it is empty here,
            # but the pattern must hold even if val were non-empty).
            raise SecretNotFound(
                f"No secret found for key {key!r} in GCP Secret Manager at "
                f"{self._url}/{self._secret_name(key)} "
                f"(secret exists but value is empty)"
            )

        # Safe log: resolution succeeded. The resolved value is never logged
        # (MUST 4). The key name and key-derived resource path are intentionally
        # omitted too (only the static project URL is logged) so the scanner's
        # clear-text-logging heuristic does not flag a key-named variable; per
        # `secrets which`, operators get the full source label on demand anyway.
        _logger.debug(
            "GCPSecretManagerBackend.get: resolved a secret from %s",
            self._url,
        )
        return val

    def get_optional(self, key: str) -> str | None:
        """Return the resolved secret value for ``key``, or None if absent.

        Raises:
            ValueError: if key does not match [A-Z0-9_]+.
        Returns None when the secret is absent from Secret Manager or its value is
        empty/whitespace after stripping (single source: the 'latest' version).
        MUST NOT include the resolved value in any exception message.
        """
        _validate_key(key)
        try:
            return self.get(key)
        except SecretNotFound:
            return None

    def has(self, key: str) -> bool:
        """Return True if ``key`` is resolvable, False otherwise.

        Strictly delegates to ``get_optional()`` (MUST 8: no separate resolution
        ladder). Guarantees ``has()`` and ``get()`` can never disagree.
        """
        return self.get_optional(key) is not None

    def locate(self, key: str) -> SecretRef | None:
        """Return a ``SecretRef`` describing where ``key`` resolves, or None.

        Source label format: ``gcp-secret-manager:<url>/<secret_name>``
        (resource path without the ``/versions/latest`` suffix -- the path, not
        the resolved version). This is the canonical label the code emits below
        and the value the GCP tests assert.
        MUST NOT include the resolved value (MUST 5).

        The source label is built from the request path only, not from any GCP
        API response, so the value can never leak into the label.
        """
        _validate_key(key)
        secret_name = self._secret_name(key)
        # Build source label from request path only (never from API response).
        # MUST 5: source label MUST NOT contain the resolved credential value.
        source = f"gcp-secret-manager:{self._url}/{secret_name}"

        if self.get_optional(key) is None:
            return None
        return SecretRef(key=key, source=source, present=True)


# ──────────────────────────────────────────────────────────────────────────────
# Factory function


def make_gcp_secret_backend_from_url(url: str) -> GCPSecretManagerBackend:
    """Construct a ``GCPSecretManagerBackend`` from a Secret Manager prefix URL.

    URL format: ``projects/<project_id>/secrets``
    (trailing slash is stripped by the constructor).

    Called by ``get_default_secret_backend()`` in the ``gcp`` branch.
    The caller is responsible for reading ``ATOMIC_AGENTS_SECRET_BACKEND_URL``
    and passing it here.

    Raises:
        ImportError: if google-cloud-secret-manager is not installed.
            Message names the ``[gcp]`` extra.
        ValueError: if the URL does not match the expected format.
    """
    return GCPSecretManagerBackend(url=url)
