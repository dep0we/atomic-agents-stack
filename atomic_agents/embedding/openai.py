"""OpenAIEmbeddingBackend -- reference implementation for the EmbeddingBackend Protocol.

Default model: ``text-embedding-3-small`` (1536 dimensions, $0.020/1M tokens).
The OpenAI embeddings API is called via the ``openai`` SDK (``[openai]`` extra).

API key resolution delegates to the framework SecretBackend (spec/38) via the
same ``_llm._get_key`` resolver as ``OpenAICompatibleLLMBackend`` (KeySpec
``["ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY"]`` / keychain
``atomic-agents-openai`` / config key ``openai``). Resolution honors whatever
backend is registered (Filesystem env/Keychain/keys.json, GCP Secret Manager,
…), so an operator's single OpenAI credential serves both LLM and embedding
calls through one backend -- and a non-filesystem backend is NOT bypassed.

Per-call client construction (``_build_client()``) follows the same pattern
as ``OpenAICompatibleLLMBackend`` -- the ``openai.OpenAI`` client is
constructed inside ``embed()``/``embed_batch()``, not in ``__init__``. This
ensures that ``patch.dict(sys.modules, {'openai': fake})`` in tests intercepts
calls correctly regardless of when the patch is applied. Holding a persistent
client in ``__init__`` would make test-isolation patches miss the already-bound
SDK reference.
"""

from __future__ import annotations

import logging

from ..exceptions import AtomicAgentsError
from .backend import EmbeddingCapabilities

_logger = logging.getLogger(__name__)

# MSG_NO_OPENAI_SDK names the actual failure mode (SDK absence at import),
# NOT a credential failure. The constant deliberately avoids the
# _KEY / _TOKEN / _SECRET / _PASSWORD substrings that trip CodeQL's
# py/clear-text-logging heuristic; see
# feedback_codeql_constant_name_false_positives.md and the #317 fix.
MSG_NO_OPENAI_SDK = (
    "openai SDK not installed; required for OpenAIEmbeddingBackend. "
    "pip install openai or install with [openai] extra"
)

# Default model for the reference implementation.
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

# Per-model NATIVE (full) dimensionality. The advertised ``dimensions`` defaults
# to the model's native size when the caller omits the argument, so constructing
# a model with no explicit ``dimensions`` always advertises exactly what the API
# returns (MUST 3 capability honesty). A single global default constant would be
# a honesty bug: it would advertise 1536 for text-embedding-3-large while the API
# returns that model's native 3072 -- the exact silent mis-sizing MUST 3 forbids
# and the pgvector wiring in PR3 would turn into insert failures / truncation.
# source: https://developers.openai.com/api/docs/models (accessed 2026-06-17)
_MODEL_NATIVE_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}

# Fallback native dimension for unknown models (3-small's size); used only when
# the caller omits ``dimensions`` for a model not in _MODEL_NATIVE_DIMENSIONS.
DEFAULT_EMBEDDING_DIMENSIONS = 1536

# Largest batch size the OpenAI embeddings API accepts in a single call.
_OPENAI_MAX_BATCH_SIZE = 2048

# Max input tokens per item for the OpenAI embedding models (text-embedding-3-*
# and ada-002 all share this limit).
_OPENAI_MAX_INPUT_TOKENS = 8192

# Models that accept the server-side ``dimensions`` reduction parameter.
# text-embedding-3-* support it; text-embedding-ada-002 does NOT (the SDK
# raises if ``dimensions`` is passed to ada-002). The dimension is forwarded
# to the API only when the configured value differs from the MODEL'S NATIVE
# dimension AND the model is in this set; otherwise the model's native
# dimensionality is returned. See MUST 3 (capability honesty) -- the
# ``dimensions`` property must never advertise a length the API won't produce.
_DIMENSIONS_REDUCIBLE_MODELS = frozenset(
    {"text-embedding-3-small", "text-embedding-3-large"}
)


def _native_dimension(model_id: str) -> int:
    """Return the model's native (full) embedding dimension.

    Falls back to ``DEFAULT_EMBEDDING_DIMENSIONS`` (1536) for models not in
    ``_MODEL_NATIVE_DIMENSIONS``. The fallback is only reached for unknown
    models; for those the caller must pass an explicit ``dimensions`` if the
    true native size differs, and the non-reducible-model construction guard
    refuses a mismatch it cannot honor.
    """
    return _MODEL_NATIVE_DIMENSIONS.get(model_id, DEFAULT_EMBEDDING_DIMENSIONS)


class OpenAIEmbeddingBackend:
    """EmbeddingBackend reference implementation via the OpenAI embeddings API.

    Stateless aside from the configuration set at construction. Safe to
    construct once and share across threads -- no persistent connection is held.

    Key behavior:

    - ``embed()`` and ``embed_batch()`` MUST NOT raise. All SDK exceptions are
      caught and converted to ``None`` return values with structured logging.
    - ``embed_batch()`` issues a single batched SDK call for efficiency; if
      the batch fails the implementation degrades to per-item ``embed()`` calls
      so each item is independently retried (and independently ``None``-able).
    - ``close()`` is a no-op (per-call client construction holds no resources).
    - API key resolved via ``_get_key()``, which delegates to the framework
      SecretBackend (spec/38) through the same resolver as
      ``OpenAICompatibleLLMBackend`` -- one credential, one backend, for both.

    MUST-7 non-determinism note: the live OpenAI embeddings API may return
    slightly different float vectors for the same input across calls and across
    model-weight updates in a deployment. The MUST-7 determinism guarantee is
    therefore best-effort for this remote provider -- downstream consumers MUST
    NOT rely on bit-exact reproducibility (cosine similarity is stable to many
    decimal places, but exact equality is not guaranteed). The conformance
    ``test_embed_same_text_same_vector`` asserts strict equality only because it
    runs against a deterministic mock, not the live API.

    Mocking note for tests: construct with a fake key (``api_key="sk-fake"``).
    Patch the ``openai`` module via ``sys.modules`` OR patch
    ``_build_client`` on the instance for the most direct interception.

    supports_input_type: honestly ``False`` (MUST 3 capability honesty).
    Verified against the installed ``openai`` SDK (openai 2.35.1): its
    ``embeddings.create()`` signature does NOT expose an ``input_type``
    parameter, so there is nothing for the backend to forward.  The
    ``embed()`` / ``embed_batch()`` Protocol surface DOES accept an
    ``input_type`` kwarg (added for forward-compatibility with providers that
    support it), but for OpenAI it is accepted-and-ignored — the flag stays
    ``False`` so a caller never assumes query/document differentiation is in
    effect.  See ``capabilities()`` and spec/46 §"supports_input_type".
    """

    def __init__(
        self,
        model_id: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int | None = None,
        api_key: str | None = None,
    ) -> None:
        """Construct the backend with optional model, dimension, and key overrides.

        Construction imports the ``openai`` SDK (fail-fast on missing dependency)
        but does NOT call any embedding endpoint -- construction is side-effect-free
        with respect to provider I/O. The import-at-construction behavior mirrors
        ``OpenAICompatibleLLMBackend.__init__`` in ``openai_compat.py`` (symbol
        reference, not a line number, to avoid line-ref drift -- cf. PR #76).

        Args:
            model_id: OpenAI embedding model identifier. Defaults to
                ``text-embedding-3-small``. MUST be a non-empty string
                (spec/46 MUST 1).
            dimensions: Expected vector dimension. When ``None`` (the default),
                resolves to the model's NATIVE dimension via
                ``_native_dimension()`` (3-small→1536, 3-large→3072,
                ada-002→1536) so the advertised ``dimensions`` always matches
                what the API returns (MUST 3 capability honesty). When given
                explicitly, MUST be a positive integer (spec/46 MUST 1) and
                MUST equal what the model produces. For ``text-embedding-3-*`` a
                reduced dimension is forwarded to the API; for
                ``text-embedding-ada-002`` (no server-side reduction) a
                non-native ``dimensions`` is rejected at construction rather
                than silently advertised but not honored (MUST 3).
            api_key: Explicit API key. When None, resolved via _get_key(),
                which delegates to the framework SecretBackend (spec/38) via the
                same ``_llm._get_key`` resolver OpenAICompatibleLLMBackend uses
                (env ATOMIC_AGENTS_OPENAI_KEY / OPENAI_API_KEY → the registered
                backend: Filesystem Keychain/keys.json, GCP Secret Manager, …).
                One credential resolves through one backend for both LLM and
                embedding calls. Unresolved → None (graceful; embed() degrades).

        Raises:
            EmbeddingError: when ``model_id`` is empty/whitespace, when
                ``dimensions`` is not a positive integer, or when a
                non-default ``dimensions`` is requested for a model that has
                no server-side dimension-reduction parameter.
            AtomicAgentsError: when the ``openai`` SDK is not installed.
        """
        # Local import avoids a module-level cycle (exceptions imports nothing
        # from embedding, but the family lives there by project convention).
        from ..exceptions import EmbeddingError

        # MUST 1 -- input validation. Refuse rather than silently accept a
        # value the backend cannot honor; a misadvertised model_id/dimensions
        # produces silent downstream failures (e.g. a pgvector column sized to
        # a dimension the API never returns).
        if not isinstance(model_id, str) or not model_id.strip():
            raise EmbeddingError(
                "model_id must be a non-empty string (spec/46 MUST 1); "
                f"got {model_id!r}"
            )
        # Resolve an omitted dimension to the model's NATIVE size so the
        # advertised dimensions always matches what the API returns (MUST 3).
        # A global default would advertise 1536 for 3-large while the API
        # returns 3072 -- the silent mis-sizing the construction guard exists
        # to prevent.
        if dimensions is None:
            dimensions = _native_dimension(model_id)
        if (
            not isinstance(dimensions, int)
            or isinstance(dimensions, bool)
            or dimensions <= 0
        ):
            raise EmbeddingError(
                "dimensions must be a positive integer (spec/46 MUST 1); "
                f"got {dimensions!r}"
            )

        try:
            import openai  # noqa: F401 -- presence check at construction
        except ImportError as exc:
            raise AtomicAgentsError(MSG_NO_OPENAI_SDK) from exc

        # MUST 3 -- capability honesty. If the operator requests a dimension
        # other than the model's NATIVE size for a model that has no
        # server-side reduction parameter, the API would return the native
        # dimension while the ``dimensions`` property advertised the requested
        # one. Refuse the mismatch at construction. (Comparing against the
        # model's native size -- not a single global constant -- is what makes
        # this honest for every model, e.g. ada-002 native 1536.)
        if (
            model_id not in _DIMENSIONS_REDUCIBLE_MODELS
            and dimensions != _native_dimension(model_id)
        ):
            if model_id in _MODEL_NATIVE_DIMENSIONS:
                # Known non-reducible model (e.g. ada-002): we genuinely know its
                # native size, so assert it.
                raise EmbeddingError(
                    f"model {model_id!r} does not support server-side dimension "
                    f"reduction; cannot honor a non-native dimensions={dimensions} "
                    f"(native is {_native_dimension(model_id)}). Use a "
                    "text-embedding-3-* model for reduced dimensions, or omit the "
                    "dimensions argument."
                )
            # Unknown model: the backend does NOT actually know this model's
            # native size or whether it supports reduction (spec/46 MUST 3
            # unknown-model limitation). Refuse, but do NOT assert a native size
            # we cannot validate -- the message must stay honest about what is
            # and isn't known.
            raise EmbeddingError(
                f"model {model_id!r} is unknown to this backend; cannot validate "
                f"dimensions={dimensions} against its native size or confirm it "
                "supports server-side dimension reduction. Add it to "
                "_MODEL_NATIVE_DIMENSIONS, omit the dimensions argument to accept "
                "the provider default, or use a known text-embedding-3-* model "
                "for reduced dimensions."
            )

        # MUST 3 -- reducible models (text-embedding-3-*): server-side reduction
        # can only SHRINK the vector, never grow it. A dimensions > native would
        # be forwarded to the API, rejected (400), and swallowed to None on
        # EVERY call while the ``dimensions`` property advertised the impossible
        # larger size -- a capability-honesty violation AND a silent-forever
        # failure (PR3 would size a pgvector column to a vector the API never
        # returns). The non-reducible guard above only fires on != native;
        # without this, dimensions > native slips through for reducible models.
        if model_id in _DIMENSIONS_REDUCIBLE_MODELS and dimensions > _native_dimension(
            model_id
        ):
            raise EmbeddingError(
                f"dimensions={dimensions} exceeds the native size "
                f"{_native_dimension(model_id)} for {model_id!r}; server-side "
                "dimension reduction can only shrink the vector, not grow it. "
                "Use a dimensions value <= the native size, or omit it."
            )

        self._model_id = model_id
        self._dimensions = dimensions
        # MUST 3 / SecretBackend coherence: use `is not None` rather than the
        # falsy `or` short-circuit. An empty string api_key='' is truthy-false
        # and would incorrectly skip _get_key(); the `is not None` form honours
        # "caller explicitly passed None (use the backend)" vs "caller passed
        # an explicit value (use it literally, even '')".  This also prevents
        # get_default_embedding_backend() from forwarding an empty env var
        # directly — the factory should coerce '' to None before construction.
        self._api_key = api_key if api_key is not None else _get_key()

    # ─── EmbeddingBackend Protocol surface ───────────────────────────────

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def provider_id(self) -> str:
        return "openai"

    def capabilities(self) -> EmbeddingCapabilities:
        """Return capability advertisement for this backend.

        supports_input_type=False: the OpenAI embeddings API (as exposed by the
        installed openai SDK, openai 2.35.1) does NOT offer an ``input_type``
        parameter on ``embeddings.create()`` (verified 2026-06-18 via installed
        SDK signature -- parameters: input, model, dimensions, encoding_format, user).
        The kwarg is accepted on ``embed()``/``embed_batch()`` per the Protocol
        surface added in PR 3, but is NOT forwarded to the API.
        ``supports_input_type=False`` is therefore capability-honest (MUST 3).
        See class docstring and spec/46 §"supports_input_type flag".
        """
        return EmbeddingCapabilities(
            max_batch_size=_OPENAI_MAX_BATCH_SIZE,
            max_input_tokens=_OPENAI_MAX_INPUT_TOKENS,
            supports_input_type=False,
        )

    def embed(self, text: str, *, input_type: str | None = None) -> list[float] | None:
        """Embed a single text string via the OpenAI embeddings API.

        Returns ``None`` on ANY failure (network error, rate limit, malformed
        input, token count exceeded, authentication failure). MUST NOT raise.

        Two-branch failure handling (spec/46 MUST 4):

        - The typed ``EmbeddingProviderUnavailable`` branch fires on
          provider-availability failures, which ``_raise_if_provider_unavailable()``
          maps onto that typed class so the branch is REACHABLE. Detection uses
          two sets (see the module comment for the rationale): the EXACT leaf
          name ``OpenAIError`` (``_PROVIDER_UNAVAILABLE_EXACT`` -- the SDK root,
          raised at client construction when no key is resolvable, matched by
          exact type name so it does NOT sweep in every API subclass that
          inherits it), plus the MRO-membership set
          (``_PROVIDER_UNAVAILABLE_MRO``): ``AuthenticationError``,
          ``RateLimitError``, ``APIConnectionError``, ``APITimeoutError``,
          and ``InternalServerError`` (5xx).
          It logs at WARNING level with the distinctive phrase "embedding
          provider unavailable".
        - The broad ``Exception`` fallback catches everything else --
          malformed-response errors, unexpected SDK errors, 4xx CLIENT
          errors (``BadRequestError`` 400 / ``NotFoundError`` 404 /
          ``UnprocessableEntityError`` 422, e.g. input-too-long), AND
          ``PermissionDeniedError`` (403, a persistent operator-actionable
          config error -- see the note on ``_PROVIDER_UNAVAILABLE_MRO``).
          These are NOT provider-availability failures, so they correctly log
          the DIFFERENT phrase, "embedding failed (unexpected error)".

        Tests MUST assert the specific log line -- not just the None return --
        to confirm the right branch fired and to avoid false-green tests where
        both branches return None
        (feedback_layered_except_typed_branch_false_green.md).

        Credential redaction: exceptions are logged by type name only, NEVER
        by ``str(exc)`` or ``exc.args`` -- the OpenAI SDK may echo partial
        credentials in AuthenticationError messages.
        """
        from ..exceptions import (
            EmbeddingProviderUnavailable,
        )  # local import avoids cycles

        try:
            # _build_client() is INSIDE the try: openai.OpenAI(api_key=None)
            # raises OpenAIError AT CONSTRUCTION when no key is resolvable
            # (a documented, common scenario per _get_key()). Constructing
            # outside the try would let that escape, violating MUST 4
            # (embed() MUST NOT raise). OpenAIError routes to the typed
            # provider-unavailable branch (OpenAIError is matched leaf-exact in
            # _PROVIDER_UNAVAILABLE_EXACT).
            client = self._build_client()
            response = client.embeddings.create(**self._create_kwargs([text]))
            vector = response.data[0].embedding
            # MUST 3 -- capability honesty enforced at the produced vector, not
            # just at construction: if the API returns a length != the advertised
            # ``dimensions`` (e.g. an unknown model whose true native size differs
            # from the 1536 fallback, or an SDK/model drift), refuse the vector
            # rather than hand a wrong-length embedding to a dimension-sized
            # vector store. Returns None (MUST-NOT-RAISE) with a distinct log.
            if len(vector) != self._dimensions:
                _logger.warning(
                    "embedding length mismatch in embed(): model %r returned %d "
                    "floats but backend advertises dimensions=%d; returning None "
                    "(MUST 3 capability honesty).",
                    self._model_id,
                    len(vector),
                    self._dimensions,
                )
                return None
            return vector
        except EmbeddingProviderUnavailable as exc:
            # Already-typed: forward-defense for a future inner helper that raises
            # the typed class directly (no current inner call does, but the broad
            # branch below would also catch it -- this branch makes the intent
            # explicit and is exercised by
            # test_openai_embed_already_typed_provider_unavailable_branch so the
            # "defensive" claim is verified, not merely asserted (Principle #12).
            _logger.warning(
                "embedding provider unavailable in embed(): %s",
                type(exc).__name__,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            # Map known provider-availability SDK errors onto the typed branch
            # so the branch-distinctive WARNING is REACHABLE; re-raise others.
            try:
                _raise_if_provider_unavailable(exc)
            except EmbeddingProviderUnavailable:
                # Log the ORIGINAL SDK exception's type name (never str(exc) --
                # MUST 5 redaction; a type name carries no credentials) so the
                # audit trail identifies which provider failure mode fired.
                _logger.warning(
                    "embedding provider unavailable in embed(): %s",
                    type(exc).__name__,
                )
                return None
            # Broad fallback: malformed responses, unexpected SDK errors.
            # Different phrase from the typed branch so tests tell them apart.
            _logger.warning(
                "embedding failed (unexpected error) in embed(): %s",
                type(exc).__name__,
            )
            return None

    def embed_batch(
        self, texts: list[str], *, input_type: str | None = None
    ) -> list[list[float] | None]:
        """Embed a list of texts via a single batched OpenAI API call.

        Issues ONE ``client.embeddings.create(input=texts)`` call. On success,
        builds the result list by index-matching response objects to input
        texts (preserving the API's response ordering). On batch failure,
        degrades to per-item ``embed()`` calls so each text is independently
        retried and independently ``None``-able.

        CONFORMANCE INVARIANT: ``len(result) == len(texts)`` always holds,
        including empty input (returns ``[]``) and all-failure (returns
        ``[None] * len(texts)``). The invariant is enforced defensively
        (NOT via ``assert``, which is stripped under ``python -O``): the
        result is normalized to exactly ``len(texts)`` elements before return.

        Partial-batch failure note: the OpenAI SDK raises exceptions on
        total batch failures; it does NOT surface partial-success exceptions
        carrying partial ``response.data``. When the batch raises, the
        per-item fallback avoids double-billing (re-billing items the failed
        batch did not embed). If future SDK versions surface partial-success
        exceptions, the degradation logic should check ``exc.response.data``
        before falling back to per-item calls.
        """
        if not texts:
            return []

        from ..exceptions import (
            EmbeddingProviderUnavailable,
        )  # local import avoids cycles

        try:
            # _build_client() is INSIDE the try for the same MUST-4 reason as
            # embed(): a no-key openai.OpenAI(api_key=None) raises at
            # construction. The except below catches it; OpenAIError routes to
            # the provider-unavailable short-circuit (no per-item amplification).
            client = self._build_client()
            response = client.embeddings.create(**self._create_kwargs(texts))
            # Validate the response mapping rather than trusting it blindly. A
            # duplicate index would silently overwrite a DIFFERENT text's vector
            # (a WRONG vector at an index -- the worst failure mode for a vector
            # store); an out-of-range index would be silently dropped; a vector
            # whose length != advertised dimensions violates MUST 3. Any of these
            # means the batch response is untrustworthy -> degrade to per-item
            # embed() (each independently validated + None-able).
            by_index: dict[int, list[float]] = {}
            structural_anomaly = False
            for obj in response.data:
                idx = getattr(obj, "index", None)
                if (
                    not isinstance(idx, int)
                    or isinstance(idx, bool)
                    or idx < 0
                    or idx >= len(texts)
                    or idx in by_index
                    or len(obj.embedding) != self._dimensions
                ):
                    structural_anomaly = True
                    break
                by_index[idx] = obj.embedding
            if structural_anomaly:
                _logger.warning(
                    "embed_batch response malformed (duplicate/out-of-range index "
                    "or wrong vector length) for %d texts; degrading to per-item "
                    "embed()",
                    len(texts),
                )
                result = [self.embed(t, input_type=input_type) for t in texts]
                return self._normalize_length(result, len(texts))
            # Well-formed but incomplete (some indices absent from the response).
            missing_indices = [i for i in range(len(texts)) if i not in by_index]
            if not missing_indices:
                result = [by_index[i] for i in range(len(texts))]
                return self._normalize_length(result, len(texts))
            # A CREDIBLE partial -- the API returned a strict majority and merely
            # dropped a few -- self-heals: re-embed ONLY the missing indices via
            # per-item embed() (no re-billing of returned items). But an empty or
            # mostly-empty 200 is NOT credible: re-embedding it would turn one
            # batch into up to N per-item calls (the amplification + possible
            # double-bill the provider-unavailable short-circuit deliberately
            # avoids), so treat it as a degraded batch and return None for the
            # missing slots loudly (the None-fallback contract -- caller degrades
            # to FTS/substring) WITHOUT amplifying.
            if len(by_index) > len(texts) // 2:
                result = [
                    by_index[i]
                    if i in by_index
                    else self.embed(texts[i], input_type=input_type)
                    for i in range(len(texts))
                ]
                _logger.warning(
                    "embed_batch returned %d of %d embeddings; re-embedded %d "
                    "missing item(s) via per-item embed()",
                    len(by_index),
                    len(texts),
                    len(missing_indices),
                )
            else:
                result = [by_index.get(i) for i in range(len(texts))]
                _logger.warning(
                    "embed_batch returned only %d of %d embeddings (not a credible "
                    "partial); returning None for %d missing item(s) without "
                    "per-item retry (no amplification)",
                    len(by_index),
                    len(texts),
                    len(missing_indices),
                )
            return self._normalize_length(result, len(texts))
        except Exception as exc:  # noqa: BLE001
            # If the batch failed because the PROVIDER is unavailable (auth,
            # rate limit, connectivity, timeout, 5xx, missing-key construction),
            # the whole batch is unrecoverable -- per-item retries would re-issue
            # up to N more guaranteed-to-fail calls (1 batch + N items), pure
            # amplification that recovers nothing (Principle #4 + #6). Return
            # [None] * len(texts) directly with the distinctive typed log.
            try:
                _raise_if_provider_unavailable(exc)
            except EmbeddingProviderUnavailable:
                _logger.warning(
                    "embedding batch failed: provider unavailable (%s); "
                    "returning None for all %d texts (no per-item retry)",
                    type(exc).__name__,
                    len(texts),
                )
                return [None] * len(texts)
            # Otherwise the batch may have failed for a single malformed item in
            # an otherwise-healthy batch. Degrade to per-item to maximize partial
            # success and avoid returning a short list.
            _logger.warning(
                "embedding batch failed; degrading to per-item embed() for %d texts",
                len(texts),
            )
            result = [self.embed(t, input_type=input_type) for t in texts]
            return self._normalize_length(result, len(texts))

    def close(self) -> None:
        """No-op: per-call client construction holds no persistent resources.

        Idempotent -- calling twice does NOT raise. Future implementations
        that hold a persistent client pool MUST release it here while
        preserving idempotency.
        """

    # ─── Internal helpers ─────────────────────────────────────────────────

    def _create_kwargs(self, inputs: list[str]) -> dict:
        """Build kwargs for ``client.embeddings.create()``.

        Forwards ``dimensions`` to the API for reducible models whenever the
        configured dimension differs from the MODEL'S NATIVE dimension -- not a
        single global constant. This is the load-bearing honesty fix: comparing
        against the global 1536 would skip the kwarg for a 3-large left at its
        native 3072 default ONLY because the advertised value was wrongly 1536;
        comparing against the model's native (3072) keeps native==native ->
        no kwarg, and any reduction (e.g. 512) -> kwarg forwarded. ada-002 (and
        any non-reducible model) gets no ``dimensions`` kwarg -- construction
        already rejected a non-native dimension for those (MUST 3), so the
        native dimension always matches ``self._dimensions`` there.
        """
        kwargs: dict = {"input": inputs, "model": self._model_id}
        if (
            self._model_id in _DIMENSIONS_REDUCIBLE_MODELS
            and self._dimensions != _native_dimension(self._model_id)
        ):
            kwargs["dimensions"] = self._dimensions
        return kwargs

    @staticmethod
    def _normalize_length(
        result: list[list[float] | None], expected: int
    ) -> list[list[float] | None]:
        """Enforce the ``len(out) == len(in)`` invariant without ``assert``.

        ``assert`` is stripped under ``python -O`` (production-common), so the
        invariant must be enforced with real control flow. Pads with ``None``
        if short, truncates if long, and logs a WARNING on any divergence
        (which would indicate an upstream bug, never normal operation).
        """
        if len(result) == expected:
            return result
        _logger.warning(
            "embed_batch length invariant violated (got %d, expected %d); normalizing",
            len(result),
            expected,
        )
        if len(result) < expected:
            return result + [None] * (expected - len(result))
        return result[:expected]

    def _build_client(self):  # type: ignore[return]
        """Construct a fresh ``openai.OpenAI`` client on every call.

        Per-call construction is the same pattern as ``OpenAICompatibleLLMBackend``
        (``openai_compat.py``). Holding a persistent ``self._client`` breaks test
        isolation: a test that patches ``sys.modules['openai']`` after backend
        construction misses the already-bound SDK reference. The per-call pattern
        ensures the currently-patched openai module is always used.
        """
        import openai  # imported here, not at module level, for test-patch isolation

        return openai.OpenAI(api_key=self._api_key)


# ──────────────────────────────────────────────────────────────────────────────
# Provider-availability exception mapping


# Classifying an SDK exception as a provider-availability failure (auth, rate
# limit, connectivity, timeout, 5xx, missing-credentials construction) is done
# WITHOUT importing the openai SDK here (it is imported lazily in
# _build_client), so detection works by class NAME. Class-name matching also
# survives the test fakes, which simulate these by name.
#
# CRITICAL: there are two distinct match modes, because in the REAL openai SDK
# EVERY API exception inherits the SDK root ``OpenAIError`` (verified against
# openai 2.35.1: BadRequestError.__mro__ == [BadRequestError, APIStatusError,
# APIError, OpenAIError, Exception, ...]). A naive "any ancestor name in the
# set" match with ``OpenAIError`` in the set therefore routes EVERY API error --
# including 4xx CLIENT errors (BadRequestError 400 / NotFoundError 404 /
# UnprocessableEntityError 422, e.g. input-too-long, the max_input_tokens
# failure mode) -- to the typed "provider unavailable" branch, mislabeling an
# input error as an availability failure AND (worse) short-circuiting
# embed_batch() to [None]*N with no per-item degradation. Two sets fix it:
#
# - _PROVIDER_UNAVAILABLE_EXACT: matched by the EXACT leaf type name
#   (``type(exc).__name__``), never by ancestry. Holds ``OpenAIError`` only --
#   the SDK root, whose REAL MRO is just [OpenAIError, Exception, ...] (it has
#   no API-error subclass of its own that we want to catch as "the root"). It
#   is raised AT CONSTRUCTION when openai.OpenAI(api_key=None) has no resolvable
#   key; that missing-credentials construction IS a provider-availability
#   failure, so we route the bare-root instance here -- but NOT every subclass
#   that happens to inherit it.
# - _PROVIDER_UNAVAILABLE_MRO: matched by ANY ancestor name (MRO membership) so
#   that provider subclasses (e.g. APITimeoutError < APIConnectionError) still
#   route correctly. These are the genuine availability LEAF classes.
#
# 4xx client errors (BadRequestError / NotFoundError / UnprocessableEntityError)
# and the bare APIStatusError match NEITHER set -> broad "unexpected error"
# branch, where they belong. (Behavior is None either way -- MUST 4 holds --
# but the audit label is now correct, and embed_batch() degrades per-item.)
#
# Both sets MUST stay in sync with the embed() docstring and the CHANGELOG
# entry (Principle #13 -- no doc-vs-code drift). The live-SDK guard test
# test_real_sdk_mro_includes_openai_error_root pins the assumption that the
# real SDK hierarchy roots at OpenAIError, so the test fakes cannot drift away
# from it.
_PROVIDER_UNAVAILABLE_EXACT = frozenset({"OpenAIError"})
_PROVIDER_UNAVAILABLE_MRO = frozenset(
    {
        "AuthenticationError",
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
    }
)
# PermissionDeniedError (403) is deliberately NOT in the availability set. A 403
# is almost always a PERSISTENT operator-actionable misconfiguration (billing
# disabled, org/project lacks model access, key missing the embeddings scope),
# not a transient outage. Routing it to the broad "unexpected error" branch
# keeps the audit label honest ("unexpected error: PermissionDeniedError" tells
# the operator to check config, not "provider unavailable" which reads as
# retry-later). Trade-off: an embed_batch() hitting a persistent 403 now
# degrades to per-item (N rejected-but-unbilled calls) instead of a single
# short-circuit -- acceptable for a rare, unbilled, operator-actionable error.


def _raise_if_provider_unavailable(exc: BaseException) -> None:
    """Re-raise ``exc`` as ``EmbeddingProviderUnavailable`` if it is a known
    provider-availability SDK error; otherwise return without raising.

    Two-mode detection (see the module comment above the sets for the full
    rationale):

    - The EXACT leaf type name is matched against ``_PROVIDER_UNAVAILABLE_EXACT``
      (currently ``{"OpenAIError"}``). This catches the SDK root raised at
      no-key client construction WITHOUT catching every API subclass that
      inherits ``OpenAIError`` in the real SDK.
    - Any ancestor name is matched against ``_PROVIDER_UNAVAILABLE_MRO`` (the
      genuine availability leaf classes) so their subclasses still route.

    Detection is by class NAME so it works without importing the openai SDK at
    module load and matches both the real SDK types and the test fakes that
    simulate them by name. The original exception is chained via ``from exc``
    for debugging, but callers log only ``type(exc).__name__`` (MUST 5
    credential redaction).
    """
    from ..exceptions import EmbeddingProviderUnavailable

    if type(exc).__name__ in _PROVIDER_UNAVAILABLE_EXACT:
        raise EmbeddingProviderUnavailable(
            f"embedding provider unavailable: {type(exc).__name__}"
        ) from exc
    mro_names = {cls.__name__ for cls in type(exc).__mro__}
    if mro_names & _PROVIDER_UNAVAILABLE_MRO:
        raise EmbeddingProviderUnavailable(
            f"embedding provider unavailable: {type(exc).__name__}"
        ) from exc


# ──────────────────────────────────────────────────────────────────────────────
# Key resolution (delegates to the framework SecretBackend, spec/38)

# KeySpec for OpenAI embedding credentials -- identical triple to
# OpenAICompatibleLLMBackend so an operator's single OpenAI credential serves
# both LLM and embedding calls through whatever SecretBackend is registered.
_OPENAI_KEY_ENV_VARS = ["ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY"]
_OPENAI_KEYCHAIN_NAME = "atomic-agents-openai"
_OPENAI_CONFIG_KEY = "openai"


def _get_key() -> str | None:
    """Resolve the OpenAI API key via the framework SecretBackend (spec/38).

    Delegates to ``_llm._get_key`` -- the canonical resolver that routes through
    ``get_default_secret_backend()`` -- so embedding key resolution honors the
    SAME SecretBackend (Filesystem, GCP Secret Manager, ...) as every other
    backend. This is the load-bearing coherence fix: a private env→Keychain→
    keys.json cascade here would bypass the SecretBackend, so an operator on
    ``ATOMIC_AGENTS_SECRET_BACKEND=gcp`` would get LLM keys from GCP but
    embedding keys from the local cascade (which never finds them) -- a
    split-brain credential path that silently breaks semantic search on exactly
    the cloud deployments this Protocol exists to enable.

    Returns ``None`` (not raising) on a genuine "no key configured" miss so
    construction stays graceful and embed()/embed_batch() degrade to the
    None-fallback (no key -> semantic search returns None -> caller falls back to
    FTS/substring). A ``SecretBackendNotRegistered`` is the OPPOSITE case -- the
    operator pinned a backend (e.g. ``ATOMIC_AGENTS_SECRET_BACKEND=gcp``) that
    isn't installed/configured -- and is RE-RAISED so the misconfiguration
    surfaces loudly rather than masquerading as "no key" (matching
    ``_llm._get_key``'s own posture; do not swallow it into the graceful path).
    """
    from ..exceptions import AtomicAgentsError
    from ..secret_backend import SecretBackendNotRegistered
    from .._llm import _get_key as _resolve_via_secret_backend

    try:
        return _resolve_via_secret_backend(
            _OPENAI_KEY_ENV_VARS,
            _OPENAI_KEYCHAIN_NAME,
            _OPENAI_CONFIG_KEY,
        )
    except SecretBackendNotRegistered:
        # Operator-pinned backend misconfig -> surface, don't silently degrade.
        raise
    except AtomicAgentsError:
        return None
