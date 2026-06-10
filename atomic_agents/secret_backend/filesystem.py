"""FilesystemSecretBackend -- reference implementation of SecretBackend (spec/38).

Resolves credentials from three machine-scoped sources in fixed priority order:

    Source 1: environment variables (env vars first non-empty wins)
    Source 2: macOS Keychain (Darwin-only; ``security find-generic-password``)
    Source 3: ``~/.config/atomic_agents/keys.json`` (JSON dict keyed by config_key)

All three sources are machine-scoped. No vault-relative paths are used.
Credentials MUST NOT travel with the vault (spec/38 MUST 2).

The lookup order is fixed and not operator-configurable. New sources are
appended at the bottom; the existing order is preserved for backward
compatibility (operators who set ANTHROPIC_API_KEY in the environment
continue to get their key via Source 1 as they did before).

Package name: ``secret_backend/`` (not ``secrets/``) to avoid shadowing the
Python stdlib ``secrets`` module (cryptographic random generation). Mirrors
the ``mcp_registry/`` naming convention (not ``mcp/``).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from .backend import SecretNotFound, _validate_key
from .types import SecretCapabilities, SecretRef

_logger = logging.getLogger(__name__)

# Provider-key mapping: maps config_key (keys.json key) to the resolution
# metadata for each known provider. Used to build informative SecretRef.source
# strings and SecretNotFound messages.
#
# Structure: config_key -> (env_vars, keychain_name)
# Mirrors the mapping in _llm.py's _get_anthropic_key / _get_openai_key / etc.
_PROVIDER_METADATA: dict[str, tuple[list[str], str]] = {
    "anthropic": (
        ["ATOMIC_AGENTS_ANTHROPIC_KEY", "ANTHROPIC_API_KEY"],
        "atomic-agents-anthropic",
    ),
    "openai": (
        ["ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY"],
        "atomic-agents-openai",
    ),
    "moonshot": (
        ["ATOMIC_AGENTS_MOONSHOT_KEY", "MOONSHOT_API_KEY"],
        "atomic-agents-moonshot",
    ),
}

# Reverse map: env var name -> config_key, for locate() source labeling
_ENV_VAR_TO_CONFIG_KEY: dict[str, str] = {
    var: config_key
    for config_key, (env_vars, _) in _PROVIDER_METADATA.items()
    for var in env_vars
}

# Reverse map: env var name -> keychain_name, for locate() source labeling
_ENV_VAR_TO_KEYCHAIN: dict[str, str] = {
    var: keychain
    for _, (env_vars, keychain) in _PROVIDER_METADATA.items()
    for var in env_vars
}

# Machine-scoped config file path -- NEVER vault-relative (spec/38 MUST 2).
_KEYS_JSON_PATH = Path.home() / ".config" / "atomic_agents" / "keys.json"


def _keychain_name_for_env_var(env_var: str) -> str:
    """Return the macOS Keychain service name for a given env var name."""
    return _ENV_VAR_TO_KEYCHAIN.get(env_var, f"atomic-agents-{env_var.lower()}")


def _config_key_for_env_var(env_var: str) -> str:
    """Return the keys.json config_key for a given env var name.

    For known env vars, returns the canonical config_key (e.g., "anthropic").
    For unknown env vars, derives a config_key by lowercasing the env var name
    (e.g., "MY_CUSTOM_KEY" -> "my_custom_key").
    """
    if env_var in _ENV_VAR_TO_CONFIG_KEY:
        return _ENV_VAR_TO_CONFIG_KEY[env_var]
    # Derive from env var name by lowercasing (no prefix stripping)
    return env_var.lower()


def _resolve_from_keychain(keychain_name: str) -> str | None:
    """Source 2: try macOS Keychain (Darwin only).

    Returns the stripped value if found, None on any failure including:
    - Non-Darwin platform (silently skip)
    - ``security`` command not found (FileNotFoundError)
    - Key not in keychain (CalledProcessError)
    - Keychain dialog timeout (TimeoutExpired)

    Uses timeout=5 to prevent indefinite blocking on headless CI runners
    or systems with a locked Keychain dialog; chosen as a conservative
    ceiling that accommodates slow local keychains without stalling the
    process.
    """
    import subprocess

    if sys.platform != "darwin":
        return None

    import os

    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                os.environ.get("USER", ""),
                "-s",
                keychain_name,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        val = result.stdout.strip()
        if val:
            return val
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        pass
    return None


def _resolve_from_keys_json(config_key: str) -> str | None:
    """Source 3: try ``~/.config/atomic_agents/keys.json``.

    Path is always machine-scoped (never vault-relative). Returns the
    stripped value if found, None otherwise.

    If keys.json exists but fails to parse, logs a WARNING and falls through
    rather than silently swallowing the error -- a corrupt-but-present config
    file is more likely a rotation-in-progress issue than a normal absence.
    """
    config_path = _KEYS_JSON_PATH
    if not config_path.exists():
        return None
    try:
        keys = json.loads(config_path.read_text(encoding="utf-8"))
        val = keys.get(config_key)
        if val is not None:
            stripped = str(val).strip()
            if stripped:
                return stripped
    except json.JSONDecodeError as e:
        _logger.warning(
            "keys.json at %s exists but failed to parse (JSONDecodeError: %s); "
            "falling through to next source. If a key rotation is in progress, "
            "retry after the write completes.",
            config_path,
            e,
        )
    except OSError:
        pass
    return None


class FilesystemSecretBackend:
    """Reference implementation of SecretBackend for filesystem + env + Keychain.

    Resolves credentials from three machine-scoped sources in fixed priority
    order. Each ``get()`` call re-resolves from live sources (no caching) for
    rotation awareness and thread safety.

    Machine-scoped sources only -- never vault-relative (spec/38 MUST 2).
    Credentials MUST NOT be stored in or resolved relative to the agent vault
    root, because vault portability would carry credentials with the agent.

    Constructor takes no arguments. The backend is stateless; capabilities and
    id are exposed via the class-level constants ``_CAPABILITIES`` and
    ``_BACKEND_ID``.
    """

    # Package name is secret_backend/ (not secrets/) to avoid shadowing the
    # Python stdlib secrets module (cryptographic random). Mirrors the
    # mcp_registry/ naming convention (not mcp/).
    _BACKEND_ID = "filesystem"

    _CAPABILITIES = SecretCapabilities(
        supports_rotation=True,  # each get() re-resolves from live sources
        supports_audit_logging=False,  # no durable audit trail
        persists_plaintext=False,  # credential sources (keys.json) are
        # machine-scoped, not vault-portable; no plaintext credential
        # travels with the agent vault (see SecretCapabilities docstring)
        supports_canonical_export=True,  # spec/40 addendum — wiring-map only, never plaintext
    )

    @property
    def capabilities(self) -> SecretCapabilities:
        """Return frozen capabilities for this backend instance.

        All call sites use property syntax: ``backend.capabilities.supports_rotation``
        NOT ``backend.capabilities().supports_rotation`` (spec/36 precedent).
        """
        return self._CAPABILITIES

    @property
    def backend_id(self) -> str:
        """Stable backend identifier: ``"filesystem"``."""
        return self._BACKEND_ID

    # ─── Internal per-key cascade ─────────────────────────────────────────

    def _resolve(self, key: str) -> tuple[str | None, str | None]:
        """Attempt to resolve ``key`` using the fixed cascade.

        Probes ``key`` directly as an env var name (also the primary env var
        for most callers). For known multi-alias providers (those in
        ``_PROVIDER_METADATA``), probes all aliases. For unknown env var names,
        probes only the single name supplied, deriving keychain/config_key via
        the ``_keychain_name_for_env_var`` / ``_config_key_for_env_var``
        helpers.

        Returns (resolved_value_or_None, source_label_or_None).

        This is the single source of truth for all resolution logic. ``get()``,
        ``get_optional()``, ``has()``, and ``locate()`` all delegate here so
        they can never disagree about a key's presence.
        """
        # If the key is a primary env var for a known provider, probe all aliases.
        # E.g., ANTHROPIC_API_KEY -> also try ATOMIC_AGENTS_ANTHROPIC_KEY first
        all_env_vars_to_try = [key]
        for config_key, (env_vars, keychain) in _PROVIDER_METADATA.items():
            if key in env_vars:
                # Put the canonical alias list in order (it starts with the
                # primary Atomic Agents prefix, then the upstream provider prefix)
                all_env_vars_to_try = env_vars
                break

        # Remove duplicates while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for ev in all_env_vars_to_try:
            if ev not in seen:
                deduped.append(ev)
                seen.add(ev)

        # Probe env vars for all aliases first (Source 1 wins for all aliases)
        import os

        for env_var in deduped:
            val = os.environ.get(env_var)
            if val is not None:
                stripped = val.strip()
                if stripped:
                    return stripped, f"env:{env_var}"

        # Source 2: macOS Keychain -- try the canonical keychain name
        primary_alias = deduped[0]
        keychain_name = _keychain_name_for_env_var(primary_alias)
        val = _resolve_from_keychain(keychain_name)
        if val is not None:
            return val, f"keychain:{keychain_name}"

        # Source 3: ~/.config/atomic_agents/keys.json
        config_key = _config_key_for_env_var(primary_alias)
        val = _resolve_from_keys_json(config_key)
        if val is not None:
            return val, f"config:~/.config/atomic_agents/keys.json:{config_key}"

        return None, None

    def resolve_with_spec(
        self,
        env_vars: list[str],
        keychain_name: str,
        config_key: str,
    ) -> str | None:
        """Resolve a credential using an explicit KeySpec triple.

        Called by ``_llm._get_key()`` for callers that already have the
        (env_vars, keychain_name, config_key) triple from a ``KeySpec`` (e.g.,
        custom ``OpenAICompatibleLLMBackend`` registrations). Unlike ``get()``,
        which derives keychain/config_key from ``_PROVIDER_METADATA`` for
        known providers, this method uses the CALLER-SUPPLIED values exactly.
        This preserves backward compatibility for custom providers whose
        keychain service name or keys.json key does not follow the
        ``atomic-agents-{env.lower()}`` / ``{env.lower()}`` derivation.

        Resolution order: all env_vars first (empty-string absent per MUST 7),
        then keychain_name, then config_key in keys.json.

        Returns the resolved value (non-empty string) or None if absent.
        Does NOT raise SecretNotFound — callers build their own error messages
        (so ``_get_key`` can format the full env_vars list in its error text).

        Internal method; not part of the SecretBackend Protocol.
        """
        import os

        # Source 1: probe every alias in env_vars (caller-supplied order)
        for env_var in env_vars:
            val = os.environ.get(env_var)
            if val is not None:
                stripped = val.strip()
                if stripped:
                    return stripped

        # Source 2: macOS Keychain (caller-supplied name, not derived)
        val = _resolve_from_keychain(keychain_name)
        if val is not None:
            return val

        # Source 3: keys.json (caller-supplied config_key, not derived)
        val = _resolve_from_keys_json(config_key)
        if val is not None:
            return val

        return None

    def _sources_tried_message(self, key: str) -> str:
        """Build a human-readable string describing the sources searched for ``key``.

        Used in SecretNotFound messages. Never includes resolved values.
        """
        primary_alias = key
        for _, (env_vars, _) in _PROVIDER_METADATA.items():
            if key in env_vars:
                primary_alias = env_vars[0]
                break

        keychain_name = _keychain_name_for_env_var(primary_alias)
        config_key = _config_key_for_env_var(primary_alias)

        # Build the alias list for the env message
        all_aliases = [key]
        for _, (env_vars, _) in _PROVIDER_METADATA.items():
            if key in env_vars:
                all_aliases = list(env_vars)
                break

        env_names = " / ".join(all_aliases)
        return (
            f"Sources tried (in order): env:{env_names}, "
            f"keychain:{keychain_name}, "
            f"config:~/.config/atomic_agents/keys.json:{config_key}. "
            f"Set one of these sources to configure the credential."
        )

    # ─── Protocol methods ─────────────────────────────────────────────────

    def get(self, key: str) -> str:
        """Return the resolved secret value for ``key``.

        Raises ``ValueError`` if ``key`` does not match ``[A-Z0-9_]+``.
        Raises ``SecretNotFound`` if no source has the key or all sources
        return empty/whitespace.

        The error message names the key and sources tried, NEVER the resolved
        value (spec/38 secrecy MUST 4).
        """
        _validate_key(key)
        val, _source = self._resolve(key)
        if val is None:
            raise SecretNotFound(
                f"No secret found for key '{key}'. " + self._sources_tried_message(key)
            )
        return val

    def get_optional(self, key: str) -> str | None:
        """Return the resolved secret value for ``key``, or None if absent.

        Raises ``ValueError`` if ``key`` does not match ``[A-Z0-9_]+``.
        Returns None (not empty string) when absent.
        """
        _validate_key(key)
        val, _source = self._resolve(key)
        return val

    def has(self, key: str) -> bool:
        """Return True if ``key`` is resolvable, False otherwise.

        Strictly delegates to ``get_optional()`` to guarantee ``has()`` and
        ``get()`` never disagree on a key's presence (split-brain prevention).
        """
        return self.get_optional(key) is not None

    def locate(self, key: str) -> SecretRef | None:
        """Return a ``SecretRef`` describing where ``key`` resolves, or None.

        MUST NOT include the resolved value in the returned ``SecretRef``
        (spec/38 secrecy MUST 5). The ``source`` field names the location only.
        """
        _validate_key(key)
        _val, source = self._resolve(key)
        if source is None:
            return None
        return SecretRef(key=key, source=source, present=True)

    def export(self, query=None):
        """Export secret backend wiring map as a SecretExport canonical object (spec/40).

        Emits ONLY logical reference names + binding hints. NEVER contains
        resolved plaintext values (spec/40 MUST 9 — absolute invariant).

        Calls locate() for each known provider key to check presence.
        NEVER calls get() or get_optional() — doing so would leak plaintext.

        Args:
            query: ``SecretExportQuery | None``. Pass None to export all known
                provider keys (anthropic, openai, moonshot). Custom operator
                keys are out of scope for PR1 — see issue #432.

        Returns:
            ``SecretExport`` with entries as SecretExportRef list.
        """
        from ..export.filesystem import export_secret
        from ..export.types import SecretExportQuery

        return export_secret(self, query or SecretExportQuery())

    def export_all(self):
        """Convenience wrapper. Equivalent to export(None)."""
        return self.export(None)

    def close(self) -> None:
        """No-op for FilesystemSecretBackend (stateless sources)."""
