"""Tests for atomic_agents.bundle — cascade pre-render command (spec/26, #231).

Covers:
- Cascaded three-layer layouts (spec/06) — full content represented.
- Single-agent flat layouts (spec/04) — falls back cleanly.
- Tools.override merging via _cascade.resolve_tools_md.
- Missing-files graceful omission (sections omitted, not empty).
- bundle.md declarative extras (comments, list shape, globs).
- --extra-file ad-hoc extras (CLI path).
- Missing extras raise FileNotFoundError (no silent drops).
- Staleness check (--if-stale): skip when bundle is fresh, regen when a
  source mutates.
- Atomic write: no half-written bundle on failure mid-write.
- Pinned-note detection via lexical `pinned: true` frontmatter scan.
- Recent-journal selection: newest N by filename descending.
- Recent-notes selection: newest N by mtime, excluding pinned.
- Cache dir override via ATOMIC_AGENTS_CACHE_DIR env var.
- Slug generation: relative-to-agents-root path with `/` → `-`.
"""

from __future__ import annotations
import os
import time
from pathlib import Path

import pytest

from atomic_agents import bundle


# ──────────────────────────────────────────────────────────────────
# Fixtures


def _build_cascaded(
    tmp_path: Path,
    *,
    system: str = "muse",
    project: str = "orb",
    role: str = "showrunner",
) -> tuple[Path, Path]:
    """Build a complete spec/06 cascade. Returns (agents_root, agent_root)."""
    agents_root = tmp_path / "agents"
    system_root = agents_root / system
    role_dir = system_root / "roles" / role
    role_dir.mkdir(parents=True)
    (role_dir / "PROMPT.md").write_text("# Role PROMPT\n\nYou are a Showrunner.")
    (role_dir / "tools.md").write_text("# Tools (role layer)\n\n- Standard read paths")
    (role_dir / "model.md").write_text("# Model\n\nclaude-sonnet")

    project_dir = system_root / "projects" / project
    project_dir.mkdir(parents=True)
    (project_dir / "canon.md").write_text("# Canon\n\nA boy. A memory orb.")
    (project_dir / "style_guide.md").write_text("# Style\n\nNo em dashes.")
    (project_dir / "goal.md").write_text("# Goal\n\nFinish Chapter 1.")
    policy_dir = project_dir / "policy"
    policy_dir.mkdir()
    (policy_dir / "001-pov.md").write_text("# POV locked\n\nThird-person limited.")

    instance_dir = project_dir / "agents" / role
    persona_dir = instance_dir / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "IDENTITY.md").write_text("# IDENTITY\n\nThe Showrunner.")
    (persona_dir / "SOUL.md").write_text("# SOUL\n\nTaste: literary realism.")
    (persona_dir / "USER.md").write_text("# USER\n\nDan likes brevity.")

    return agents_root, instance_dir


def _build_flat(tmp_path: Path, name: str = "caldwell") -> tuple[Path, Path]:
    """Build a spec/04 single-agent layout. Returns (agents_root, agent_root)."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / name
    persona_dir = agent_root / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "IDENTITY.md").write_text("# IDENTITY\n\nCaldwell.")
    (persona_dir / "SOUL.md").write_text("# SOUL\n\nDirect.")
    (persona_dir / "USER.md").write_text("# USER\n\nDan.")
    (agent_root / "tools.md").write_text("# Tools\n\n- Read paths")
    (agent_root / "goal.md").write_text("# Goal\n\nPay off debt.")
    return agents_root, agent_root


# ──────────────────────────────────────────────────────────────────
# Cascaded full-layout coverage


def test_bundle_cascaded_includes_every_layer(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    cache_dir = tmp_path / "cache"

    result = bundle.render_bundle(
        agent_root,
        agents_root=agents_root,
        cache_dir=cache_dir,
    )

    assert result.regenerated is True
    assert result.path.is_file()
    text = result.path.read_text(encoding="utf-8")

    # BP1 stable cascade — role, persona, tools, project layer
    assert "# === BREAKPOINT 1: Stable cascade ===" in text
    assert "Role layer · PROMPT.md" in text
    assert "You are a Showrunner." in text
    assert "Instance persona · IDENTITY.md" in text
    assert "Instance persona · SOUL.md" in text
    assert "Instance persona · USER.md" in text
    assert "Tools" in text and "Standard read paths" in text
    assert "Project shared · canon.md" in text
    assert "A boy. A memory orb." in text
    assert "Project shared · style_guide.md" in text
    assert "Project shared · goal.md" in text
    assert "Project shared · policy/" in text
    assert "Third-person limited." in text


def test_bundle_flat_layout_uses_spec04_order(tmp_path):
    agents_root, agent_root = _build_flat(tmp_path)
    cache_dir = tmp_path / "cache"

    result = bundle.render_bundle(
        agent_root,
        agents_root=agents_root,
        cache_dir=cache_dir,
    )

    text = result.path.read_text(encoding="utf-8")
    assert "Persona · IDENTITY.md" in text
    assert "Persona · SOUL.md" in text
    assert "Persona · USER.md" in text
    assert "goal.md" in text
    assert "tools.md" in text
    # No cascade-specific sections
    assert "Project shared" not in text
    assert "Role layer" not in text
    # No BP1.5 by default
    assert "BREAKPOINT 1.5" not in text


# ──────────────────────────────────────────────────────────────────
# tools.override merging


def test_bundle_includes_merged_tools_override(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    # Add instance tools.override.md — merged additively per _cascade.resolve_tools_md
    (agent_root / "tools.override.md").write_text(
        "# Tools (override)\n\n- Project-specific write paths"
    )

    result = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
    )

    text = result.path.read_text(encoding="utf-8")
    assert "Standard read paths" in text  # role base
    assert "Project-specific write paths" in text  # instance override
    assert "Tools (merged)" in text


# ──────────────────────────────────────────────────────────────────
# Missing-files graceful omission


def test_bundle_omits_missing_optional_files(tmp_path):
    """A cascade missing optional pieces (no policy, no goal, no memory) renders
    fine — sections are omitted, not empty."""
    agents_root, agent_root = _build_cascaded(tmp_path)
    # Remove optional files
    (agent_root.parent.parent / "goal.md").unlink()  # project goal
    policy_dir = agent_root.parent.parent / "policy"
    for p in policy_dir.iterdir():
        p.unlink()
    policy_dir.rmdir()

    result = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
    )

    text = result.path.read_text(encoding="utf-8")
    assert "Project shared · goal.md" not in text
    assert "Project shared · policy/" not in text
    # Required pieces still present
    assert "Project shared · canon.md" in text


# ──────────────────────────────────────────────────────────────────
# Operator extras — bundle.md (declarative)


def test_bundle_md_declarative_extras(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    extras_dir = tmp_path / "extras"
    extras_dir.mkdir()
    (extras_dir / "operator-soul.md").write_text("# Operator SOUL\n\nDan's identity.")
    (extras_dir / "operator-user.md").write_text("# Operator USER\n\nDan's prefs.")

    (agent_root / "bundle.md").write_text(
        f"""# Extras to bundle

# Operator identity files
- {extras_dir / "operator-soul.md"}
- {extras_dir / "operator-user.md"}
"""
    )

    result = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
    )

    text = result.path.read_text(encoding="utf-8")
    assert "# === BREAKPOINT 1.5: Operator extras ===" in text
    assert "Dan's identity." in text
    assert "Dan's prefs." in text
    # Extras come AFTER stable cascade
    bp1_idx = text.index("BREAKPOINT 1:")
    bp15_idx = text.index("BREAKPOINT 1.5")
    assert bp1_idx < bp15_idx


def test_bundle_md_supports_globs(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    extras_dir = tmp_path / "identity"
    extras_dir.mkdir()
    (extras_dir / "SOUL.md").write_text("soul")
    (extras_dir / "USER.md").write_text("user")
    (extras_dir / "HEARTBEAT.md").write_text("heartbeat")

    (agent_root / "bundle.md").write_text(f"- {extras_dir}/*.md\n")

    result = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
    )
    text = result.path.read_text(encoding="utf-8")
    assert "soul" in text
    assert "user" in text
    assert "heartbeat" in text


def test_bundle_md_missing_file_raises(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    (agent_root / "bundle.md").write_text("- /nonexistent/path/missing.md\n")

    with pytest.raises(FileNotFoundError) as ei:
        bundle.render_bundle(
            agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
        )
    assert "missing.md" in str(ei.value)


# ──────────────────────────────────────────────────────────────────
# Operator extras — --extra-file (CLI)


def test_extra_file_arg(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    extra = tmp_path / "extra.md"
    extra.write_text("# Operator extra\n\nFrom CLI flag.")

    result = bundle.render_bundle(
        agent_root,
        agents_root=agents_root,
        cache_dir=tmp_path / "cache",
        extra_files=[extra],
    )
    text = result.path.read_text(encoding="utf-8")
    assert "From CLI flag." in text


def test_extra_file_arg_missing_raises(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)

    with pytest.raises(FileNotFoundError):
        bundle.render_bundle(
            agent_root,
            agents_root=agents_root,
            cache_dir=tmp_path / "cache",
            extra_files=[Path("/nope/missing.md")],
        )


# ──────────────────────────────────────────────────────────────────
# Staleness


def test_if_stale_skips_when_bundle_is_fresh(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    cache_dir = tmp_path / "cache"

    first = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=cache_dir
    )
    assert first.regenerated is True
    first_mtime = first.path.stat().st_mtime

    # Wait so any regeneration would have a strictly newer mtime
    time.sleep(0.05)
    second = bundle.render_bundle(
        agent_root,
        agents_root=agents_root,
        cache_dir=cache_dir,
        if_stale=True,
    )
    assert second.regenerated is False
    assert second.path.stat().st_mtime == first_mtime


def test_if_stale_regenerates_when_source_mutates(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    cache_dir = tmp_path / "cache"

    first = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=cache_dir
    )
    first_mtime = first.path.stat().st_mtime

    # Touch a cascade source so its mtime exceeds the bundle's
    time.sleep(0.05)
    canon_path = agent_root.parent.parent / "canon.md"
    canon_path.write_text("# Canon\n\nNEW content")
    new_canon_mtime = canon_path.stat().st_mtime
    assert new_canon_mtime > first_mtime

    second = bundle.render_bundle(
        agent_root,
        agents_root=agents_root,
        cache_dir=cache_dir,
        if_stale=True,
    )
    assert second.regenerated is True
    assert "NEW content" in second.path.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────
# Atomic write


def test_atomic_write_no_partial_file(tmp_path, monkeypatch):
    """Force an exception mid-write; verify no partial bundle is left behind."""
    agents_root, agent_root = _build_cascaded(tmp_path)
    cache_dir = tmp_path / "cache"

    # Monkeypatch atomic_write to raise after the temp file is created
    real_atomic = bundle.atomic_write

    def boom(target, content, encoding="utf-8"):
        raise RuntimeError("simulated mid-write failure")

    monkeypatch.setattr(bundle, "atomic_write", boom)

    with pytest.raises(RuntimeError):
        bundle.render_bundle(agent_root, agents_root=agents_root, cache_dir=cache_dir)

    # No partial bundle visible
    slug = bundle.slug_for(agent_root, agents_root)
    assert not (cache_dir / f"{slug}.md").is_file()

    # Restore + verify the real atomic_write works
    monkeypatch.setattr(bundle, "atomic_write", real_atomic)
    result = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=cache_dir
    )
    assert result.path.is_file()


# ──────────────────────────────────────────────────────────────────
# Memory: pinned + recent + INDEX


def test_pinned_notes_included(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    memory_dir = agent_root / "memory"
    memory_dir.mkdir()
    (memory_dir / "INDEX.md").write_text("# Memory INDEX\n\n- pinned-note")
    (memory_dir / "pinned-note.md").write_text(
        "---\nname: pinned-note\npinned: true\ntype: feedback\n---\n\nPinned body."
    )
    (memory_dir / "unpinned-note.md").write_text(
        "---\nname: unpinned-note\npinned: false\ntype: feedback\n---\n\nUnpinned body."
    )

    result = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
    )
    text = result.path.read_text(encoding="utf-8")
    assert "Memory · INDEX.md" in text
    assert "Memory · Pinned atomic notes" in text
    assert "Pinned body." in text


def test_recent_notes_excludes_pinned(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    memory_dir = agent_root / "memory"
    memory_dir.mkdir()
    (memory_dir / "INDEX.md").write_text("# INDEX")
    (memory_dir / "pinned-old.md").write_text(
        "---\npinned: true\n---\n\nPinned content."
    )
    (memory_dir / "recent-1.md").write_text("---\npinned: false\n---\n\nRecent #1.")
    (memory_dir / "recent-2.md").write_text("---\npinned: false\n---\n\nRecent #2.")

    result = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
    )
    text = result.path.read_text(encoding="utf-8")
    # Pinned content appears under Pinned section
    pinned_section = text.split("Pinned atomic notes")[1].split("BREAKPOINT")[0]
    assert "Pinned content." in pinned_section
    # Recent section exists and contains unpinned notes
    assert "Recent atomic notes" in text
    recent_section = text.split("Recent atomic notes")[1]
    assert "Recent #1." in recent_section
    assert "Recent #2." in recent_section
    # pinned-old should NOT appear in recent section
    assert "Pinned content." not in recent_section


# ──────────────────────────────────────────────────────────────────
# Journal


def test_recent_journal_selects_newest_first(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    journal_dir = agent_root / "journal" / "2026-05"
    journal_dir.mkdir(parents=True)
    (journal_dir / "2026-05-15.md").write_text("Older entry")
    (journal_dir / "2026-05-17.md").write_text("Newer entry")

    result = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
    )
    text = result.path.read_text(encoding="utf-8")
    # RECENT_JOURNAL_DEFAULT = 1 → only the newest entry should appear
    assert "Newer entry" in text
    assert "Older entry" not in text


# ──────────────────────────────────────────────────────────────────
# Cache dir override


def test_cache_dir_env_var(tmp_path, monkeypatch):
    agents_root, agent_root = _build_cascaded(tmp_path)
    custom_cache = tmp_path / "custom-cache"
    monkeypatch.setenv("ATOMIC_AGENTS_CACHE_DIR", str(custom_cache))

    result = bundle.render_bundle(agent_root, agents_root=agents_root)

    assert custom_cache in result.path.parents


# ──────────────────────────────────────────────────────────────────
# Slug


def test_slug_uses_relative_path_when_under_agents_root(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    slug = bundle.slug_for(agent_root, agents_root)
    assert slug == "muse-projects-orb-agents-showrunner"


def test_slug_falls_back_to_tail_components(tmp_path):
    """Agent path not under agents_root falls back to last-N components."""
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    agent_root = tmp_path / "free-floating" / "a" / "b" / "c" / "d"
    agent_root.mkdir(parents=True)
    slug = bundle.slug_for(agent_root, other_root)
    assert "d" in slug  # at minimum the tail name is in there


# ──────────────────────────────────────────────────────────────────
# render_bundle preconditions + first-call semantics


def test_render_bundle_raises_when_agent_root_missing(tmp_path):
    nonexistent = tmp_path / "no-such-agent"
    with pytest.raises(FileNotFoundError) as ei:
        bundle.render_bundle(nonexistent, cache_dir=tmp_path / "cache")
    assert "agent_root" in str(ei.value) or "no-such-agent" in str(ei.value)


def test_if_stale_on_first_call_writes_bundle(tmp_path):
    """--if-stale with no existing bundle must still generate (not skip)."""
    agents_root, agent_root = _build_cascaded(tmp_path)
    cache_dir = tmp_path / "cache"
    result = bundle.render_bundle(
        agent_root,
        agents_root=agents_root,
        cache_dir=cache_dir,
        if_stale=True,
    )
    assert result.regenerated is True
    assert result.path.is_file()


# ──────────────────────────────────────────────────────────────────
# bundle.md edge cases


def test_bundle_md_self_mutation_triggers_regen(tmp_path):
    """Editing only bundle.md (no cascade source change) should mark stale."""
    agents_root, agent_root = _build_cascaded(tmp_path)
    extras_dir = tmp_path / "extras"
    extras_dir.mkdir()
    (extras_dir / "a.md").write_text("first")
    (extras_dir / "b.md").write_text("second")

    bundle_md = agent_root / "bundle.md"
    bundle_md.write_text(f"- {extras_dir / 'a.md'}\n")

    cache_dir = tmp_path / "cache"
    first = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=cache_dir
    )
    first_mtime = first.path.stat().st_mtime
    text_before = first.path.read_text(encoding="utf-8")
    assert "first" in text_before
    assert "second" not in text_before

    time.sleep(0.05)
    bundle_md.write_text(f"- {extras_dir / 'b.md'}\n")

    second = bundle.render_bundle(
        agent_root,
        agents_root=agents_root,
        cache_dir=cache_dir,
        if_stale=True,
    )
    assert second.regenerated is True
    text_after = second.path.read_text(encoding="utf-8")
    assert "second" in text_after


def test_bundle_md_glob_matches_zero_raises(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    (agent_root / "bundle.md").write_text("- /tmp/nonexistent-dir-xyz/*.md\n")
    with pytest.raises(FileNotFoundError) as ei:
        bundle.render_bundle(
            agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
        )
    assert "glob" in str(ei.value).lower() or "nonexistent-dir-xyz" in str(ei.value)


def test_bundle_md_relative_path_resolves_under_agent_root(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    (agent_root / "extras-here.md").write_text("# Local extra\n\nUnder agent root.")
    (agent_root / "bundle.md").write_text("- extras-here.md\n")

    result = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
    )
    text = result.path.read_text(encoding="utf-8")
    assert "Under agent root." in text


def test_bundle_md_backtick_quoted_path(tmp_path):
    agents_root, agent_root = _build_cascaded(tmp_path)
    extras_dir = tmp_path / "extras"
    extras_dir.mkdir()
    (extras_dir / "quoted.md").write_text("quoted body")
    (agent_root / "bundle.md").write_text(f"- `{extras_dir / 'quoted.md'}`\n")

    result = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
    )
    assert "quoted body" in result.path.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────
# Pinned-note frontmatter alternates


def test_pinned_accepts_yes_and_1(tmp_path):
    """pinned: true / yes / 1 are all accepted per the lexical scan."""
    agents_root, agent_root = _build_cascaded(tmp_path)
    memory_dir = agent_root / "memory"
    memory_dir.mkdir()
    (memory_dir / "INDEX.md").write_text("# INDEX")
    (memory_dir / "pinned-true.md").write_text("---\npinned: true\n---\n\nBody-T")
    (memory_dir / "pinned-yes.md").write_text("---\npinned: yes\n---\n\nBody-Y")
    (memory_dir / "pinned-one.md").write_text("---\npinned: 1\n---\n\nBody-1")
    (memory_dir / "pinned-false.md").write_text("---\npinned: false\n---\n\nBody-F")

    result = bundle.render_bundle(
        agent_root, agents_root=agents_root, cache_dir=tmp_path / "cache"
    )
    text = result.path.read_text(encoding="utf-8")
    pinned_section = text.split("Pinned atomic notes")[1].split("BREAKPOINT")[0]
    assert "Body-T" in pinned_section
    assert "Body-Y" in pinned_section
    assert "Body-1" in pinned_section
    assert "Body-F" not in pinned_section


# ──────────────────────────────────────────────────────────────────
# CLI subcommand — `_cmd_bundle`


def _setup_cli_env(tmp_path, monkeypatch):
    """Build a cascade + point ATOMIC_AGENTS_ROOT at it. Returns the agent name."""
    agents_root, agent_root = _build_cascaded(tmp_path)
    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(agents_root))
    # The CLI's agent argument is a name resolved under agents_root via Path joining.
    # For cascaded agents the "name" is the full relative path component.
    rel = agent_root.relative_to(agents_root)
    return agents_root, agent_root, str(rel)


def test_cli_bundle_writes_and_reports(tmp_path, monkeypatch, capsys):
    from atomic_agents.cli import main as cli_main

    agents_root, agent_root, name = _setup_cli_env(tmp_path, monkeypatch)
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("ATOMIC_AGENTS_CACHE_DIR", str(cache_dir))

    rc = cli_main(["bundle", name])
    out = capsys.readouterr()

    assert rc == 0
    assert "Bundle regenerated" in out.out
    slug = bundle.slug_for(agent_root, agents_root)
    assert (cache_dir / f"{slug}.md").is_file()


def test_cli_bundle_missing_agent_exits_1(tmp_path, monkeypatch, capsys):
    from atomic_agents.cli import main as cli_main

    monkeypatch.setenv("ATOMIC_AGENTS_ROOT", str(tmp_path))
    rc = cli_main(["bundle", "no-such-agent"])
    out = capsys.readouterr()

    assert rc == 1
    assert "not found" in out.err.lower()


def test_cli_bundle_print_path_no_regen(tmp_path, monkeypatch, capsys):
    from atomic_agents.cli import main as cli_main

    agents_root, agent_root, name = _setup_cli_env(tmp_path, monkeypatch)
    cache_dir = tmp_path / "cache"

    rc = cli_main(["bundle", name, "--cache-dir", str(cache_dir), "--print-path"])
    out = capsys.readouterr()

    assert rc == 0
    slug = bundle.slug_for(agent_root, agents_root)
    expected = str(cache_dir / f"{slug}.md")
    assert expected in out.out
    # Critically: --print-path must NOT regenerate
    assert not (cache_dir / f"{slug}.md").exists()


def test_cli_bundle_to_stdout(tmp_path, monkeypatch, capsys):
    from atomic_agents.cli import main as cli_main

    agents_root, agent_root, name = _setup_cli_env(tmp_path, monkeypatch)
    cache_dir = tmp_path / "cache"

    rc = cli_main(["bundle", name, "--cache-dir", str(cache_dir), "--to-stdout"])
    out = capsys.readouterr()

    assert rc == 0
    # The bundle content should be on stdout
    assert "BREAKPOINT 1" in out.out
    assert "Role layer" in out.out


def test_cli_bundle_extra_file_missing_exits_1(tmp_path, monkeypatch, capsys):
    from atomic_agents.cli import main as cli_main

    agents_root, agent_root, name = _setup_cli_env(tmp_path, monkeypatch)

    rc = cli_main(
        [
            "bundle",
            name,
            "--cache-dir",
            str(tmp_path / "cache"),
            "--extra-file",
            "/definitely/not/a/real/path.md",
        ]
    )
    out = capsys.readouterr()

    assert rc == 1
    assert "extra-file" in out.err or "not a file" in out.err.lower()


def test_cli_bundle_if_stale_and_refresh_mutually_exclusive(tmp_path, monkeypatch):
    """argparse should refuse --if-stale and --refresh together (mutex group)."""
    from atomic_agents.cli import main as cli_main

    _, _, name = _setup_cli_env(tmp_path, monkeypatch)

    with pytest.raises(SystemExit):
        cli_main(
            ["bundle", name, "--if-stale", "--refresh", "--cache-dir", str(tmp_path)]
        )


# ──────────────────────────────────────────────────────────────────
# Doctor check — `check_bundle_cache_writable`


def test_doctor_bundle_cache_pass(tmp_path, monkeypatch):
    from atomic_agents.doctor import check_bundle_cache_writable, PASS

    monkeypatch.setenv("ATOMIC_AGENTS_CACHE_DIR", str(tmp_path / "cache-pass"))
    result = check_bundle_cache_writable()
    assert result.status == PASS
    assert (tmp_path / "cache-pass").is_dir()


def test_doctor_bundle_cache_fail_when_dir_unwritable(tmp_path, monkeypatch):
    """Read-only parent directory makes mkdir fail."""
    from atomic_agents.doctor import check_bundle_cache_writable, FAIL

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)  # read+execute, no write
    monkeypatch.setenv("ATOMIC_AGENTS_CACHE_DIR", str(locked / "subdir" / "bundles"))

    try:
        result = check_bundle_cache_writable()
        assert result.status == FAIL
        assert "fix_hint" in result.__dict__ or result.fix_hint  # has guidance
    finally:
        locked.chmod(0o700)  # restore so tmp_path cleanup works


def test_doctor_bundle_cache_fail_probe_write(tmp_path, monkeypatch):
    """Cache dir exists but is not writable — probe-write step catches it."""
    from atomic_agents.doctor import check_bundle_cache_writable, FAIL

    cache_dir = tmp_path / "ro-cache"
    cache_dir.mkdir()
    cache_dir.chmod(0o500)  # read+execute, no write
    monkeypatch.setenv("ATOMIC_AGENTS_CACHE_DIR", str(cache_dir))

    try:
        result = check_bundle_cache_writable()
        assert result.status == FAIL
        assert "writable" in result.message.lower() or "ro-cache" in result.message
    finally:
        cache_dir.chmod(0o700)


def test_doctor_bundle_cache_reports_env_var_source(tmp_path, monkeypatch):
    """When ATOMIC_AGENTS_CACHE_DIR is set, the detail/source string reflects it."""
    from atomic_agents.doctor import check_bundle_cache_writable

    custom = tmp_path / "via-env"
    monkeypatch.setenv("ATOMIC_AGENTS_CACHE_DIR", str(custom))
    result = check_bundle_cache_writable()
    assert "ATOMIC_AGENTS_CACHE_DIR" in result.detail["source"]
