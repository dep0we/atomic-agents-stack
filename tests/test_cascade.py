"""Tests for atomic_agents._cascade — multi-agent project cascade loader.

Covers spec/06 acceptance criteria: detection, role/project/instance loading,
override resolution (replacement + .override.md additive merge), backwards
compatibility, queue claim mechanics.
"""

from __future__ import annotations
import os
import time
from pathlib import Path

import pytest

import json

from atomic_agents import _cascade
from atomic_agents._cascade import (
    CascadePaths,
    QueueItem,
    claim_next_queued,
    detect_cascade,
    load_project_layer,
    load_role_prompt,
    move_to_dead_letter,
    recover_stale_claims,
    release_claim,
    renew_lease,
    resolve_model_md,
    resolve_tools_md,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures


def _build_cascade_layout(tmp_path: Path, system: str = "muse",
                          project: str = "the-unfinished",
                          role: str = "writer") -> Path:
    """Build the minimal cascade dir tree and return the instance path."""
    system_root = tmp_path / system
    role_dir = system_root / "roles" / role
    role_dir.mkdir(parents=True)
    project_dir = system_root / "projects" / project
    instance_dir = project_dir / "agents" / role
    instance_dir.mkdir(parents=True)
    return instance_dir


# ──────────────────────────────────────────────────────────────────
# Detection


def test_detect_cascade_full_layout(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    cascade = detect_cascade(instance)
    assert cascade is not None
    assert cascade.role_name == "writer"
    assert cascade.project_name == "the-unfinished"
    assert cascade.role_root == tmp_path / "muse" / "roles" / "writer"
    assert cascade.project_root == tmp_path / "muse" / "projects" / "the-unfinished"
    assert cascade.instance_root == instance


def test_detect_cascade_returns_none_for_single_agent(tmp_path):
    """Plain `<agents_root>/<name>/` layout is not a cascade."""
    single = tmp_path / "caldwell"
    single.mkdir()
    assert detect_cascade(single) is None


def test_detect_cascade_returns_none_when_role_dir_missing(tmp_path):
    """If <system>/roles/<role>/ doesn't exist, refuse the cascade — there's
    nothing to cascade into and we shouldn't fabricate empty role state."""
    system_root = tmp_path / "muse"
    instance = system_root / "projects" / "p" / "agents" / "writer"
    instance.mkdir(parents=True)
    # No <system>/roles/writer/
    assert detect_cascade(instance) is None


def test_detect_cascade_returns_none_for_partial_match(tmp_path):
    """Path shaped like '../agents/foo' but no projects/ ancestor."""
    odd = tmp_path / "stuff" / "agents" / "writer"
    odd.mkdir(parents=True)
    assert detect_cascade(odd) is None


def test_detect_cascade_handles_short_paths(tmp_path):
    short = tmp_path / "a"
    short.mkdir()
    assert detect_cascade(short) is None


# ──────────────────────────────────────────────────────────────────
# Layer-1 (role) loading


def test_load_role_prompt_reads_role_prompt_md(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    role_dir = tmp_path / "muse" / "roles" / "writer"
    (role_dir / "PROMPT.md").write_text("# Writer role\n\nYou draft chapters.")
    cascade = detect_cascade(instance)
    assert load_role_prompt(cascade) == "# Writer role\n\nYou draft chapters."


def test_load_role_prompt_returns_empty_when_missing(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    cascade = detect_cascade(instance)
    assert load_role_prompt(cascade) == ""


# ──────────────────────────────────────────────────────────────────
# Layer-2 (project) loading


def test_load_project_layer_reads_canon_style_goal(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    project_dir = tmp_path / "muse" / "projects" / "the-unfinished"
    (project_dir / "canon.md").write_text("World canon here.")
    (project_dir / "style_guide.md").write_text("Use Oxford commas.")
    (project_dir / "goal.md").write_text("Finish Act II by Q3.")

    cascade = detect_cascade(instance)
    layer = load_project_layer(cascade)
    assert layer["canon"] == "World canon here."
    assert layer["style_guide"] == "Use Oxford commas."
    assert layer["goal"] == "Finish Act II by Q3."
    assert layer["policy"] == ""


def test_load_project_layer_concatenates_policy_dir(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    project_dir = tmp_path / "muse" / "projects" / "the-unfinished"
    policy_dir = project_dir / "policy"
    policy_dir.mkdir()
    (policy_dir / "01_voice.md").write_text("Voice rules.")
    (policy_dir / "02_pacing.md").write_text("Pacing rules.")
    sub = policy_dir / "drafts"
    sub.mkdir()
    (sub / "rules.md").write_text("Draft rules.")

    cascade = detect_cascade(instance)
    policy_text = load_project_layer(cascade)["policy"]
    assert "Voice rules." in policy_text
    assert "Pacing rules." in policy_text
    assert "Draft rules." in policy_text
    # Alphabetical order — 01 before 02
    assert policy_text.index("Voice") < policy_text.index("Pacing")
    # Per-file H1 separators present
    assert "# policy/01_voice.md" in policy_text
    assert "# policy/drafts/rules.md" in policy_text


def test_load_project_layer_returns_empty_for_missing(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    cascade = detect_cascade(instance)
    layer = load_project_layer(cascade)
    assert layer == {"canon": "", "style_guide": "", "goal": "", "policy": ""}


# ──────────────────────────────────────────────────────────────────
# Override resolution: tools.md


def test_resolve_tools_md_role_only(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    role_dir = tmp_path / "muse" / "roles" / "writer"
    (role_dir / "tools.md").write_text("## Read paths\n- ~/docs/")

    cascade = detect_cascade(instance)
    src, text = resolve_tools_md(cascade)
    assert src == role_dir / "tools.md"
    assert "~/docs/" in text


def test_resolve_tools_md_instance_replaces_role(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    role_dir = tmp_path / "muse" / "roles" / "writer"
    (role_dir / "tools.md").write_text("## Read paths\n- ~/role/")
    (instance / "tools.md").write_text("## Read paths\n- ~/instance/")

    cascade = detect_cascade(instance)
    src, text = resolve_tools_md(cascade)
    assert src == instance / "tools.md"
    assert "~/instance/" in text
    assert "~/role/" not in text  # full replacement, not merge


def test_resolve_tools_md_override_appends_to_role(tmp_path):
    """tools.override.md = additive merge: role text first, then override section."""
    instance = _build_cascade_layout(tmp_path)
    role_dir = tmp_path / "muse" / "roles" / "writer"
    (role_dir / "tools.md").write_text("## Read paths\n- ~/role/")
    (instance / "tools.override.md").write_text("## Hard NOs\n- never delete files")

    cascade = detect_cascade(instance)
    src, text = resolve_tools_md(cascade)
    assert src == instance / "tools.override.md"
    assert "~/role/" in text  # role section preserved
    assert "never delete files" in text  # override section appended
    # Role should appear before override
    assert text.index("~/role/") < text.index("never delete files")


def test_resolve_tools_md_override_wins_over_instance_tools(tmp_path):
    """If both tools.md AND tools.override.md exist at instance level, override wins."""
    instance = _build_cascade_layout(tmp_path)
    role_dir = tmp_path / "muse" / "roles" / "writer"
    (role_dir / "tools.md").write_text("## Read paths\n- ~/role/")
    (instance / "tools.md").write_text("## Read paths\n- ~/instance-replace/")
    (instance / "tools.override.md").write_text("## Hard NOs\n- never")

    cascade = detect_cascade(instance)
    src, text = resolve_tools_md(cascade)
    assert src == instance / "tools.override.md"
    # Override merges role + override; instance/tools.md is ignored
    assert "~/role/" in text
    assert "never" in text
    assert "~/instance-replace/" not in text


def test_resolve_tools_md_neither_exists(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    cascade = detect_cascade(instance)
    src, text = resolve_tools_md(cascade)
    assert src is None
    assert text == ""


# ──────────────────────────────────────────────────────────────────
# Override resolution: model.md


def test_resolve_model_md_role_only(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    role_dir = tmp_path / "muse" / "roles" / "writer"
    role_model = role_dir / "model.md"
    role_model.write_text("## Default model\nclaude-sonnet-4-6-20260101")

    cascade = detect_cascade(instance)
    assert resolve_model_md(cascade) == role_model


def test_resolve_model_md_instance_overrides_role(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    role_dir = tmp_path / "muse" / "roles" / "writer"
    (role_dir / "model.md").write_text("## Default model\nrole-model")
    instance_model = instance / "model.md"
    instance_model.write_text("## Default model\ninstance-model")

    cascade = detect_cascade(instance)
    assert resolve_model_md(cascade) == instance_model


def test_resolve_model_md_neither(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    cascade = detect_cascade(instance)
    assert resolve_model_md(cascade) is None


# ──────────────────────────────────────────────────────────────────
# Queue claim mechanics


def test_claim_next_queued_picks_first_alphabetical(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    qd = project / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "002_chapter_b.md").write_text("Write chapter B")
    (qd / "001_chapter_a.md").write_text("Write chapter A")

    item = claim_next_queued(project, role="writer", lease_token="lease-1")
    assert item is not None
    assert item.original_name == "001_chapter_a.md"
    assert item.path.exists()
    assert item.path.parent == project / "queue" / "claimed" / "lease-1"
    assert item.path.read_text() == "Write chapter A"
    # Source moved
    assert not (qd / "001_chapter_a.md").exists()


def test_claim_next_queued_returns_none_when_empty(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    (project / "queue" / "queued" / "writer").mkdir(parents=True)
    assert claim_next_queued(project, role="writer", lease_token="lease-1") is None


def test_claim_next_queued_returns_none_when_no_queue_dir(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    assert claim_next_queued(project, role="writer", lease_token="lease-1") is None


def test_release_claim_moves_to_done(tmp_path):
    # done/ is namespaced by lease_token to prevent silent overwrites.
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    qd = project / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "task.md").write_text("Do the thing")

    item = claim_next_queued(project, role="writer", lease_token="lease-1")
    release_claim(item, project)
    assert (project / "queue" / "done" / "lease-1" / "task.md").exists()
    assert not item.path.exists()


def test_move_to_dead_letter_writes_reason(tmp_path):
    # dead-letter/ is namespaced by lease_token to prevent silent overwrites.
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    qd = project / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "bad.md").write_text("Bad task")

    item = claim_next_queued(project, role="writer", lease_token="lease-1")
    move_to_dead_letter(item, project, reason="Failed validation 3 times")

    dl_dir = project / "queue" / "dead-letter" / "lease-1"
    assert (dl_dir / "bad.md").exists()
    assert (dl_dir / "bad.md.reason.txt").read_text() == "Failed validation 3 times"


def test_recover_stale_claims_moves_old_files_back(tmp_path):
    # Legacy-path: sidecar has been backdated so lease_expires_at is in the past.
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    qd = project / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "stuck.md").write_text("Stuck task")
    item = claim_next_queued(project, role="writer", lease_token="dead-lease",
                             lease_seconds=3600)

    # Backdate the sidecar's lease_expires_at to 2 hours ago so it looks expired.
    sidecar_path = item.path.parent / (item.original_name + ".lease.json")
    data = json.loads(sidecar_path.read_text())
    from datetime import datetime, timezone
    past = datetime.fromtimestamp(time.time() - 7200, tz=timezone.utc)
    data["lease_expires_at"] = past.isoformat()
    sidecar_path.write_text(json.dumps(data))

    recovered = recover_stale_claims(project, lease_seconds=3600)
    assert len(recovered) == 1
    assert recovered[0].name == "stuck.md"
    # Recovery is now namespaced by lease_token.
    assert recovered[0].parent == project / "queue" / "queued" / "_recovered" / "dead-lease"


def test_recover_stale_claims_leaves_fresh_claims_alone(tmp_path):
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    qd = project / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "fresh.md").write_text("Just claimed")
    item = claim_next_queued(project, role="writer", lease_token="fresh-lease")

    recovered = recover_stale_claims(project, lease_seconds=3600)
    assert recovered == []
    assert item.path.exists()  # still claimed


def test_two_workers_racing_only_one_claims(tmp_path, monkeypatch):
    """Atomicity: even if two workers list the same files, rename is atomic."""
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    qd = project / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "only_one.md").write_text("first")

    item1 = claim_next_queued(project, role="writer", lease_token="worker-A")
    item2 = claim_next_queued(project, role="writer", lease_token="worker-B")
    assert item1 is not None
    assert item2 is None  # second worker finds nothing left


# ──────────────────────────────────────────────────────────────────
# Regression tests — Bug 1: collision-safe namespaced destinations


def test_release_claim_no_overwrite_with_namespaced_dest(tmp_path):
    """Two leases with same-named files both released → both preserved (different done/ dirs)."""
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"

    for lease_token, content in [("lease-A", "Work A"), ("lease-B", "Work B")]:
        qd = project / "queue" / "queued" / "writer"
        qd.mkdir(parents=True, exist_ok=True)
        (qd / "task.md").write_text(content)
        item = claim_next_queued(project, role="writer", lease_token=lease_token)
        release_claim(item, project)

    done_a = project / "queue" / "done" / "lease-A" / "task.md"
    done_b = project / "queue" / "done" / "lease-B" / "task.md"
    assert done_a.exists(), "lease-A item should be in done/lease-A/"
    assert done_b.exists(), "lease-B item should be in done/lease-B/"
    assert done_a.read_text() == "Work A"
    assert done_b.read_text() == "Work B"


def test_dead_letter_no_overwrite(tmp_path):
    """Two leases with same-named files dead-lettered → both preserved (different dirs)."""
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"

    for lease_token, content in [("lease-X", "Bad X"), ("lease-Y", "Bad Y")]:
        qd = project / "queue" / "queued" / "writer"
        qd.mkdir(parents=True, exist_ok=True)
        (qd / "bad.md").write_text(content)
        item = claim_next_queued(project, role="writer", lease_token=lease_token)
        move_to_dead_letter(item, project, reason=f"reason-{lease_token}")

    dl_x = project / "queue" / "dead-letter" / "lease-X" / "bad.md"
    dl_y = project / "queue" / "dead-letter" / "lease-Y" / "bad.md"
    assert dl_x.exists(), "lease-X item should be in dead-letter/lease-X/"
    assert dl_y.exists(), "lease-Y item should be in dead-letter/lease-Y/"
    assert dl_x.read_text() == "Bad X"
    assert dl_y.read_text() == "Bad Y"
    assert (project / "queue" / "dead-letter" / "lease-X" / "bad.md.reason.txt").read_text() == "reason-lease-X"
    assert (project / "queue" / "dead-letter" / "lease-Y" / "bad.md.reason.txt").read_text() == "reason-lease-Y"


# ──────────────────────────────────────────────────────────────────
# Regression tests — Bug 2: explicit lease sidecar prevents false recovery


def test_recover_stale_claims_uses_explicit_lease(tmp_path):
    """A fresh claim with a future lease_expires_at is NOT recovered even if mtime is old."""
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    qd = project / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "active.md").write_text("Active work")
    item = claim_next_queued(project, role="writer", lease_token="active-lease",
                             lease_seconds=3600)

    # Backdate the work file's mtime to 2 hours ago — this would have triggered
    # recovery under the old mtime-only logic.
    old_time = time.time() - 7200
    os.utime(item.path, (old_time, old_time))
    # The sidecar still has lease_expires_at in the future (just written), so
    # recover_stale_claims should leave this item alone.

    recovered = recover_stale_claims(project, lease_seconds=3600)
    assert recovered == [], "Item with valid sidecar lease must not be recovered despite old mtime"
    assert item.path.exists()


def test_recover_stale_claims_recovers_expired_lease(tmp_path):
    """A claim whose sidecar lease_expires_at is in the past IS recovered."""
    from datetime import datetime, timezone as tz

    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    qd = project / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "expired.md").write_text("Expired work")
    item = claim_next_queued(project, role="writer", lease_token="expired-lease",
                             lease_seconds=3600)

    # Rewrite the sidecar with lease_expires_at 2 hours in the past.
    sidecar_path = item.path.parent / (item.original_name + ".lease.json")
    data = json.loads(sidecar_path.read_text())
    past = datetime.fromtimestamp(time.time() - 7200, tz=tz.utc)
    data["lease_expires_at"] = past.isoformat()
    sidecar_path.write_text(json.dumps(data))

    recovered = recover_stale_claims(project, lease_seconds=3600)
    assert len(recovered) == 1
    assert recovered[0].name == "expired.md"
    assert recovered[0].parent == project / "queue" / "queued" / "_recovered" / "expired-lease"
    # Sidecar should be deleted after recovery.
    assert not sidecar_path.exists()


def test_renew_lease_updates_sidecar(tmp_path):
    """renew_lease(item, 3600) updates lease_expires_at in the sidecar."""
    from datetime import datetime, timezone as tz

    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    qd = project / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "renew.md").write_text("Long work")
    item = claim_next_queued(project, role="writer", lease_token="renew-lease",
                             lease_seconds=60)  # short initial lease

    sidecar_path = item.path.parent / (item.original_name + ".lease.json")
    original_expires = datetime.fromisoformat(
        json.loads(sidecar_path.read_text())["lease_expires_at"]
    )

    # Renew with a longer lease.
    renew_lease(item, additional_seconds=3600)

    updated_data = json.loads(sidecar_path.read_text())
    updated_expires = datetime.fromisoformat(updated_data["lease_expires_at"])
    if updated_expires.tzinfo is None:
        updated_expires = updated_expires.replace(tzinfo=tz.utc)

    # New expiry must be later than the original (which was only 60s).
    assert updated_expires > original_expires
    # Sanity: new expiry should be roughly 1 hour from now.
    delta = updated_expires.timestamp() - time.time()
    assert 3500 < delta < 3700, f"Expected ~3600s lease, got {delta:.0f}s"
    assert updated_data["lease_seconds"] == 3600


def test_recover_stale_claims_legacy_mtime_fallback(tmp_path):
    """A claim with NO sidecar (legacy state) triggers recovery via mtime fallback."""
    instance = _build_cascade_layout(tmp_path)
    project = tmp_path / "muse" / "projects" / "the-unfinished"
    qd = project / "queue" / "queued" / "writer"
    qd.mkdir(parents=True)
    (qd / "legacy.md").write_text("Legacy task")
    item = claim_next_queued(project, role="writer", lease_token="legacy-lease",
                             lease_seconds=3600)

    # Remove the sidecar to simulate a legacy claim (written before this fix).
    sidecar_path = item.path.parent / (item.original_name + ".lease.json")
    sidecar_path.unlink()
    assert not sidecar_path.exists()

    # Backdate work file mtime to 2 hours ago.
    old_time = time.time() - 7200
    os.utime(item.path, (old_time, old_time))

    recovered = recover_stale_claims(project, lease_seconds=3600)
    assert len(recovered) == 1
    assert recovered[0].name == "legacy.md"
    assert recovered[0].parent == project / "queue" / "queued" / "_recovered" / "legacy-lease"
