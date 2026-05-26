"""Canonical dataclasses for the PersonaBackend Protocol (spec/33).

Three frozen dataclasses define the persona identity-layer contract:

- ``Persona`` -- the canonical record for a single persona (identity,
  soul, user bodies plus version metadata).
- ``PersonaSnapshot`` -- snapshot metadata returned by
  ``PersonaBackend.list_snapshots()``, mirroring the ``ProfileSnapshot``
  shape from ``profile/types.py``.
- ``PersonaCapabilities`` -- backend-declared capability snapshot, with
  ``supports_templates`` retiring spec/24 line 436's TemplateProfileBackend
  reservation (D5).

No exceptions live here. Per D-PI-1 (pre-impl prep amendment 2026-05-26),
persona exceptions live in ``atomic_agents/exceptions.py`` so that
``PersonaOwnershipConflict`` (raised by profile backends) and
``PersonaLinkInvalid`` (raised by the persona_link_md.py parser) can
be imported without creating cross-module import cycles.

Scaffolding PR (#62 PR 1 of 4): no call site routes through the Protocol
yet. ``AtomicAgent.__init__`` is unchanged. PR 2 wires the bootstrap path;
these types exist so PR 2 has a stable contract to wire against.
"""

from __future__ import annotations

from dataclasses import dataclass


# ──────────────────────────────────────────────────────────────────────────────
# Persona


@dataclass(frozen=True, slots=True)
class Persona:
    """The canonical identity record for a single persona.

    A ``Persona`` carries the three body strings that the framework uses to
    assemble an agent's system prompt (``identity``, ``soul``, ``user``),
    plus version metadata. ``PersonaBackend`` is the source of truth when
    an agent's persona is owned by PersonaBackend (signaled by the presence
    of ``<agent>/persona.link.md`` in PR 3).

    Fields:

    ``identity``: the contents of the persona's IDENTITY.md body. Used as
        the agent's persona identity string in system prompt assembly.

    ``soul``: the contents of the persona's SOUL.md body. Used as the
        soul string in system prompt assembly.

    ``user``: the contents of the persona's USER.md body. Used as the user
        context string in system prompt assembly.

    ``version``: integer monotone counter incremented on every save. Starts
        at 1 for newly-created personas. Backends increment this on each
        ``save_persona`` call.

    ``label``: optional human-readable label for this version of the persona
        record (e.g., ``"post-tone-rewrite"``). Distinct from snapshot labels
        -- this is the persona record's own label, not a snapshot label.

    ``created_at``: ISO 8601 timestamp string recording when this version of
        the persona record was created (i.e., when it was last saved).
    """

    identity: str
    soul: str
    user: str
    version: int
    created_at: str
    label: str | None = None


# ──────────────────────────────────────────────────────────────────────────────
# PersonaSnapshot


@dataclass(frozen=True, slots=True)
class PersonaSnapshot:
    """Snapshot metadata for a single persona snapshot.

    Mirrors the ``ProfileSnapshot`` shape from ``profile/types.py``. Returned
    by ``PersonaBackend.list_snapshots(persona_id)``.

    Fields:

    ``snapshot_id``: backend-issued unique identifier for this snapshot.
        Passed to ``PersonaBackend.restore(persona_id, snapshot_id)`` to
        revert the persona record to the captured state.

    ``persona_id``: the persona this snapshot belongs to. Backends enforce
        cross-persona isolation: a snapshot id from persona A MUST raise
        ``PersonaSnapshotNotFound`` when restored to persona B.

    ``label``: optional human-readable label supplied by the operator when
        calling ``PersonaBackend.snapshot(persona_id, label=...)``.

    ``created_at``: ISO 8601 timestamp string recording when the snapshot
        was captured.

    ``persona``: the full ``Persona`` record captured at snapshot time.
        Round-trips the identity, soul, user, version, and label fields
        byte-for-byte.
    """

    snapshot_id: str
    persona_id: str
    created_at: str
    persona: Persona
    label: str | None = None


# ──────────────────────────────────────────────────────────────────────────────
# PersonaCapabilities


@dataclass(frozen=True, slots=True)
class PersonaCapabilities:
    """Backend-declared capability snapshot for a PersonaBackend instance.

    Conformance tests assert claim-vs-behavior parity. Backends that lie
    about capabilities produce silent failures rather than loud refusals
    (spec/33 implementer contract MUST -- finalized at PR 4 lock).

    Fields:

    ``supports_save``: True if ``save_persona`` is implemented and persists
        records. False for read-only backends (e.g., a persona-template
        library).

    ``supports_clone``: True if ``clone`` is implemented. The
        ``FilesystemPersonaBackend`` sets this True.

    ``supports_snapshot``: True if the snapshot trio (``snapshot``,
        ``restore``, ``list_snapshots``) is fully implemented. The
        ``FilesystemPersonaBackend`` sets this False in PR 1; the capability
        flips to True in PR 3 when the filesystem snapshot trio lands.

    ``supports_subscribe``: True if the backend supports change-notification
        subscriptions (reserved for v1.1+; all v1 backends set False).

    ``durable``: True if the backend persists records across process restart.
        Filesystem and database backends are durable; in-memory test-fixture
        backends are not.

    ``supports_templates``: True if the backend provides read-only persona
        templates (e.g., a pip-installable persona marketplace package).
        Retires spec/24 line 436's TemplateProfileBackend reservation (D5) --
        templates are PersonaBackend's domain because they are persona-centric.
        All v1.0 backends set this False; the marketplace surface is v1.1+.
    """

    supports_save: bool
    supports_clone: bool
    supports_snapshot: bool
    supports_subscribe: bool
    durable: bool
    supports_templates: bool
