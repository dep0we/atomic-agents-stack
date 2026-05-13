"""Canonical-JSON helpers for stable cross-process hashing.

Used by the judge layer (spec/28) for ``tool_definition_hash`` and
``arguments_hash`` on ``ActionProposal``. Centralizing the canonicalization
rule here ensures every caller — proposal assembly, audit serialization,
test fixtures — produces the same bytes for the same logical input.

Canonicalization rule (single source of truth):

    json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

- ``sort_keys=True`` — key order is part of the canonical form.
- ``separators=(",", ":")`` — no whitespace; bytes are reproducible.
- ``ensure_ascii=False`` — non-ASCII characters serialize as UTF-8, not
  ``\\uXXXX`` escapes. This is deliberate: JSON's UTF-8 native shape is
  the canonical one. Escaping would diverge across stdlib versions if the
  default changed.

The dict-key-order rule means ``{"a":1,"b":2}`` and ``{"b":2,"a":1}``
hash identically. Conformance suite asserts this for both
``arguments_hash`` and ``tool_definition_hash`` callers.

PR 1 of #112 (scaffolding) — this module ships now so the conformance
suite can validate determinism + sensitivity without waiting for
proposal-assembly to land in PR 2.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Serialize ``obj`` to its canonical JSON string.

    Raises ``TypeError`` if ``obj`` contains a value that is not natively
    JSON-serializable (e.g., ``set``, ``bytes``, ``Path``, custom classes
    without ``__dict__`` introspection). Callers should normalize inputs
    to JSON-native types (``dict``, ``list``, ``str``, ``int``, ``float``,
    ``bool``, ``None``) before hashing — the framework does not silently
    drop or stringify unknown types, because such silent fallbacks would
    diverge hashes across callers.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_sha256(obj: Any) -> str:
    """Return the hex sha256 of ``obj``'s canonical JSON encoding.

    Equivalent to ``hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()``
    but centralizes the ``.encode()`` step — callers that need the hash
    skip a footgun where a ``.encode()`` default (or worse, a missing
    ``.encode()``) would diverge bytes silently.
    """
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
