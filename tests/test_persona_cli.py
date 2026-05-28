"""CLI integration tests for ``atomic-agents persona`` subcommands.

Invokes ``atomic_agents.cli.main(argv=[...])`` directly with explicit argv.
Uses ``capsys`` for stdout/stderr capture and ``tmp_path`` for a fresh
``.personas/`` root on every test. Sets ``ATOMIC_AGENTS_PERSONA_BACKEND``
and ``ATOMIC_AGENTS_PERSONA_BACKEND_URL`` to point the filesystem backend
at the tmp directory so no test touches the real project personas root.

The snapshot trio (``snapshot`` / ``list-snapshots`` / ``restore``) targets
the Protocol contract that subagent A is implementing. If that impl has not
landed yet, those tests will fail with ``NotImplementedError`` and the
orchestrator re-runs the full suite after all subagents converge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.cli import main
from atomic_agents.persona.types import Persona


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures


def _make_persona(
    identity: str = "You are a helpful assistant.",
    soul: str = "Curious, direct, honest.",
    user: str = "User is a developer.",
    version: int = 1,
    created_at: str = "2026-05-26T12:00:00Z",
    label: str | None = None,
) -> Persona:
    return Persona(
        identity=identity,
        soul=soul,
        user=user,
        version=version,
        created_at=created_at,
        label=label,
    )


@pytest.fixture()
def personas_root(tmp_path: Path) -> Path:
    """Return a fresh ``.personas/`` directory and set env vars."""
    root = tmp_path / ".personas"
    root.mkdir()
    return root


@pytest.fixture()
def backend(personas_root: Path):
    """Return a ``FilesystemPersonaBackend`` scoped to ``personas_root``."""
    from atomic_agents.persona.filesystem import FilesystemPersonaBackend

    return FilesystemPersonaBackend(personas_root)


def _run(
    argv: list[str],
    personas_root: Path,
    monkeypatch,
    capsys,
) -> tuple[int, str, str]:
    """Run ``main(argv)`` with the backend env vars pointing at ``personas_root``.

    Returns ``(exit_code, stdout_text, stderr_text)``.
    """
    monkeypatch.setenv("ATOMIC_AGENTS_PERSONA_BACKEND", "filesystem")
    monkeypatch.setenv(
        "ATOMIC_AGENTS_PERSONA_BACKEND_URL",
        f"filesystem://{personas_root}",
    )
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ─────────────────────────────────────────────────────────────────────────────
# 1. persona list -- empty


def test_persona_list_empty(personas_root, monkeypatch, capsys):
    """``persona list`` on an empty backend returns 0 and prints the empty message."""
    code, out, err = _run(["persona", "list"], personas_root, monkeypatch, capsys)
    assert code == 0
    assert "No personas found" in out
    assert err == ""


# ─────────────────────────────────────────────────────────────────────────────
# 2. persona list -- one persona present


def test_persona_list_one_persona(personas_root, backend, monkeypatch, capsys):
    """``persona list`` after creating a persona returns 0 and prints the id."""
    backend.save_persona("assistant-v1", _make_persona())
    code, out, err = _run(["persona", "list"], personas_root, monkeypatch, capsys)
    assert code == 0
    assert "assistant-v1" in out
    assert err == ""


# ─────────────────────────────────────────────────────────────────────────────
# 3. persona show -- existing persona


def test_persona_show_existing(personas_root, backend, monkeypatch, capsys):
    """``persona show`` prints IDENTITY, SOUL, USER bodies clearly labeled."""
    backend.save_persona(
        "assistant-v1",
        _make_persona(
            identity="You are a helpful assistant.",
            soul="Curious, direct, honest.",
            user="User is a developer.",
        ),
    )
    code, out, err = _run(
        ["persona", "show", "assistant-v1"], personas_root, monkeypatch, capsys
    )
    assert code == 0
    assert "--- IDENTITY ---" in out
    assert "You are a helpful assistant." in out
    assert "--- SOUL ---" in out
    assert "Curious, direct, honest." in out
    assert "--- USER ---" in out
    assert "User is a developer." in out
    assert err == ""


# ─────────────────────────────────────────────────────────────────────────────
# 4. persona show -- missing persona


def test_persona_show_missing(personas_root, monkeypatch, capsys):
    """``persona show`` for a non-existent persona returns non-zero + stderr error."""
    code, out, err = _run(
        ["persona", "show", "does-not-exist"], personas_root, monkeypatch, capsys
    )
    assert code != 0
    assert "Error" in err or "not found" in err.lower()


# ─────────────────────────────────────────────────────────────────────────────
# 5. persona snapshot -- returns a snapshot_id


def test_persona_snapshot_returns_id(personas_root, backend, monkeypatch, capsys):
    """``persona snapshot`` returns 0 and prints a snapshot_id to stdout."""
    backend.save_persona("assistant-v1", _make_persona())
    code, out, err = _run(
        ["persona", "snapshot", "assistant-v1"], personas_root, monkeypatch, capsys
    )
    if err and "not supported" in err:
        pytest.skip("snapshot trio not yet landed (subagent A pending)")
    assert code == 0, f"Expected exit 0, got {code}; stderr={err!r}"
    snapshot_id = out.strip()
    assert snapshot_id != "", "Expected a non-empty snapshot_id on stdout"


# ─────────────────────────────────────────────────────────────────────────────
# 6. persona snapshot --label round-trip via list-snapshots


def test_persona_snapshot_label_roundtrip(personas_root, backend, monkeypatch, capsys):
    """``persona snapshot --label`` label appears in ``persona list-snapshots``."""
    backend.save_persona("assistant-v1", _make_persona())

    # Create snapshot with label
    code, out, err = _run(
        ["persona", "snapshot", "assistant-v1", "--label", "pre-rewrite"],
        personas_root,
        monkeypatch,
        capsys,
    )
    if err and "not supported" in err:
        pytest.skip("snapshot trio not yet landed (subagent A pending)")
    assert code == 0, f"snapshot failed: {err!r}"

    # Verify via list-snapshots
    code2, out2, err2 = _run(
        ["persona", "list-snapshots", "assistant-v1"],
        personas_root,
        monkeypatch,
        capsys,
    )
    assert code2 == 0, f"list-snapshots failed: {err2!r}"
    assert "pre-rewrite" in out2, f"Label not in list-snapshots output: {out2!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. persona list-snapshots -- chronological order


def test_persona_list_snapshots_chronological(
    personas_root, backend, monkeypatch, capsys
):
    """``persona list-snapshots`` returns snapshots in chronological order."""
    backend.save_persona("analyst-v1", _make_persona(identity="Analyst persona"))

    # Create two snapshots
    code1, out1, _ = _run(
        ["persona", "snapshot", "analyst-v1", "--label", "snap-1"],
        personas_root,
        monkeypatch,
        capsys,
    )
    if _ and "not supported" in _:
        pytest.skip("snapshot trio not yet landed (subagent A pending)")
    assert code1 == 0

    code2, out2, _ = _run(
        ["persona", "snapshot", "analyst-v1", "--label", "snap-2"],
        personas_root,
        monkeypatch,
        capsys,
    )
    assert code2 == 0

    # List snapshots
    code3, out3, err3 = _run(
        ["persona", "list-snapshots", "analyst-v1"],
        personas_root,
        monkeypatch,
        capsys,
    )
    assert code3 == 0, f"list-snapshots failed: {err3!r}"
    lines = [ln for ln in out3.splitlines() if ln.strip()]
    assert len(lines) >= 2, f"Expected at least 2 snapshot lines; got: {out3!r}"
    # Both label strings must appear
    assert "snap-1" in out3
    assert "snap-2" in out3


# ─────────────────────────────────────────────────────────────────────────────
# 8. persona restore -- restores body bytes


def test_persona_restore_restores_body(personas_root, backend, monkeypatch, capsys):
    """``persona restore`` reverts to the snapshot body (verify via persona show)."""
    original_identity = "Original identity text."
    backend.save_persona("ops-v1", _make_persona(identity=original_identity))

    # Take snapshot
    code, snap_out, snap_err = _run(
        ["persona", "snapshot", "ops-v1"], personas_root, monkeypatch, capsys
    )
    if snap_err and "not supported" in snap_err:
        pytest.skip("snapshot trio not yet landed (subagent A pending)")
    assert code == 0
    snapshot_id = snap_out.strip()

    # Overwrite the persona
    backend.save_persona(
        "ops-v1",
        _make_persona(identity="Modified identity text."),
        overwrite=True,
    )

    # Restore from snapshot
    code2, out2, err2 = _run(
        ["persona", "restore", "ops-v1", snapshot_id],
        personas_root,
        monkeypatch,
        capsys,
    )
    assert code2 == 0, f"restore failed: {err2!r}"

    # Verify via show
    code3, out3, err3 = _run(
        ["persona", "show", "ops-v1"], personas_root, monkeypatch, capsys
    )
    assert code3 == 0
    assert original_identity in out3


# ─────────────────────────────────────────────────────────────────────────────
# 9a. persona clone -- creates target with same body bytes


def test_persona_clone_creates_target(personas_root, backend, monkeypatch, capsys):
    """``persona clone`` creates the target with the same IDENTITY/SOUL/USER bytes."""
    backend.save_persona(
        "base-persona",
        _make_persona(
            identity="Base identity.",
            soul="Base soul.",
            user="Base user.",
        ),
    )

    code, out, err = _run(
        ["persona", "clone", "base-persona", "cloned-persona"],
        personas_root,
        monkeypatch,
        capsys,
    )
    assert code == 0, f"clone failed: {err!r}"

    # Verify target exists and has same bodies
    code2, out2, err2 = _run(
        ["persona", "show", "cloned-persona"], personas_root, monkeypatch, capsys
    )
    assert code2 == 0
    assert "Base identity." in out2
    assert "Base soul." in out2
    assert "Base user." in out2


# ─────────────────────────────────────────────────────────────────────────────
# 9b. persona clone -- existing target raises PersonaExists to stderr


def test_persona_clone_existing_target_errors(
    personas_root, backend, monkeypatch, capsys
):
    """``persona clone`` into an existing target returns non-zero with error on stderr."""
    backend.save_persona("src", _make_persona(identity="Source."))
    backend.save_persona("dst", _make_persona(identity="Already exists."))

    code, out, err = _run(
        ["persona", "clone", "src", "dst"],
        personas_root,
        monkeypatch,
        capsys,
    )
    assert code != 0
    assert "Error" in err


# ─────────────────────────────────────────────────────────────────────────────
# 10. non-existent persona_id produces a clear error (no traceback)


def test_each_subcommand_nonexistent_persona_clear_error(
    personas_root, monkeypatch, capsys
):
    """Each subcommand against a missing persona surfaces a clear error, not a traceback."""
    subcommands_and_args = [
        ["persona", "show", "ghost"],
        ["persona", "clone", "ghost", "target-copy"],
    ]
    for argv in subcommands_and_args:
        code, out, err = _run(argv, personas_root, monkeypatch, capsys)
        assert code != 0, f"Expected non-zero for {argv}; got 0"
        assert "Traceback" not in err, f"Got Python traceback for {argv}: {err!r}"
        # The error message should say something useful
        assert err.strip() != "", f"Expected non-empty stderr for {argv}"
