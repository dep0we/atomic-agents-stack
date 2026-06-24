"""Tests for atomic-agents init advisor template structure + str.Template substitution.

Coverage: structural conformance to spec/35 anatomy, locked template variables,
P2 dual rendering of Q7 (USER.md + tools.md), MUST 13 safe_substitute behavior.
"""

from __future__ import annotations

import re
from importlib import resources
from string import Template

from atomic_agents.init import constants as C


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_template(relpath: str) -> str:
    """Read a template file from the advisor template tree via importlib.resources.

    Using importlib.resources ensures tests pass under wheel install per OQ11
    (no reliance on __file__ paths that may not exist in a built distribution).
    """
    base = resources.files("atomic_agents.init") / "templates" / "advisor"
    return (base / relpath).read_text(encoding="utf-8")


def _all_template_relpaths() -> list[str]:
    """Return all relative paths of files in the advisor template tree."""
    base = resources.files("atomic_agents.init") / "templates" / "advisor"
    results: list[str] = []

    def _walk(node: object, parts: list[str]) -> None:
        for child in node.iterdir():  # type: ignore[attr-defined]
            child_parts = parts + [child.name]
            try:
                list(child.iterdir())  # type: ignore[attr-defined]
                is_dir = True
            except (NotADirectoryError, OSError):
                is_dir = False
            if is_dir:
                _walk(child, child_parts)
            else:
                results.append("/".join(child_parts))

    _walk(base, [])
    return results


def _extract_template_vars(content: str) -> set[str]:
    """Return the set of ${var} variable names referenced in a template string."""
    return set(re.findall(r"\$\{([^}]+)\}", content))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_advisor_template_has_all_required_files() -> None:
    """All seven required template files must be present."""
    required = [
        "persona/IDENTITY.md",
        "persona/SOUL.md",
        "persona/USER.md",
        "tools.md",
        "model.md",
        "memory/INDEX.md",
        "wiki/INDEX.md",
    ]
    base = resources.files("atomic_agents.init") / "templates" / "advisor"
    for relpath in required:
        parts = relpath.split("/")
        node = base
        for part in parts:
            node = node / part
        assert node.is_file(), f"Missing required template file: {relpath}"


def test_advisor_template_no_em_dashes() -> None:
    """No template file should contain an em dash character (U+2014).

    Em dashes are a known AI-writing tell and are prohibited across the project
    per the plain-language style rules.
    """
    for relpath in _all_template_relpaths():
        content = _read_template(relpath)
        assert "—" not in content, f"Em dash found in template file: {relpath}"


def test_advisor_identity_uses_action_class_vocabulary() -> None:
    """IDENTITY.md must reference each action class name AND its substitution variable.

    The autonomy ladder table in IDENTITY.md lists all four action class names as
    literal row labels AND renders each policy via the corresponding ${autonomy_*}
    substitution variable. Both must be present so the rendered file is coherent.
    """
    body = _read_template("persona/IDENTITY.md")

    for action_class in C.ACTION_CLASSES:
        assert action_class in body, (
            f"IDENTITY.md missing action class name: {action_class}"
        )

    autonomy_vars = (
        C.TEMPLATE_VAR_AUTONOMY_READ_ONLY,
        C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE,
        C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT,
        C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK,
    )
    for var in autonomy_vars:
        assert f"${{{var}}}" in body, (
            f"IDENTITY.md missing substitution variable: ${{{var}}}"
        )


def test_advisor_template_variables_match_constants() -> None:
    """Every ${var} reference in any template file must be one of the 12 locked names.

    This enforces the H2 lock from spec/35: the set of template variable names is
    frozen and must not drift between the template files and constants.py.
    """
    locked_vars: set[str] = {
        C.TEMPLATE_VAR_AGENT_NAME,
        C.TEMPLATE_VAR_MISSION,
        C.TEMPLATE_VAR_SCOPE_IN,
        C.TEMPLATE_VAR_SCOPE_OUT,
        C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL,
        C.TEMPLATE_VAR_AUTONOMY_READ_ONLY,
        C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE,
        C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT,
        C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK,
        C.TEMPLATE_VAR_VOICE,
        C.TEMPLATE_VAR_COMM_PREFS,
        C.TEMPLATE_VAR_HARD_REFUSALS,
    }
    for relpath in _all_template_relpaths():
        content = _read_template(relpath)
        found_vars = _extract_template_vars(content)
        unknown = found_vars - locked_vars
        assert not unknown, (
            f"Template file {relpath} references unknown variable(s): {unknown}. "
            f"All ${'{var}'} references must be in the 12 locked names from constants.py."
        )


def test_advisor_user_md_has_things_to_avoid_section() -> None:
    """USER.md must have a 'Things to avoid' section AND reference ${hard_refusals}.

    This is the P2 dual-rendering lock from spec/35: Q7 (hard refusals) renders
    into both USER.md and tools.md so the operator's stated prohibitions appear
    in both the persona and the tool-policy files.
    """
    body = _read_template("persona/USER.md")
    assert "Things to avoid" in body, (
        "USER.md must contain a 'Things to avoid' section header (P2 dual-rendering lock)"
    )
    assert f"${{{C.TEMPLATE_VAR_HARD_REFUSALS}}}" in body, (
        f"USER.md must reference substitution variable ${{{C.TEMPLATE_VAR_HARD_REFUSALS}}} "
        f"(P2 dual-rendering lock for Q7 hard refusals)"
    )


def test_advisor_tools_md_has_hard_nos_section() -> None:
    """tools.md must have a 'Hard NOs' section AND reference ${hard_refusals}.

    Mirrors the USER.md P2 check: the hard refusals answer from Q7 must appear
    in the tool-policy file as well so it is enforced both at the persona level
    and at the tool-access level.
    """
    body = _read_template("tools.md")
    assert "Hard NOs" in body, (
        "tools.md must contain a 'Hard NOs' section header (P2 dual-rendering lock)"
    )
    assert f"${{{C.TEMPLATE_VAR_HARD_REFUSALS}}}" in body, (
        f"tools.md must reference substitution variable ${{{C.TEMPLATE_VAR_HARD_REFUSALS}}} "
        f"(P2 dual-rendering lock for Q7 hard refusals)"
    )


def test_safe_substitute_handles_dollar_in_answers() -> None:
    """str.Template.safe_substitute must replace ${mission} while leaving $primary_goal intact.

    MUST 13 from spec/35 requires safe_substitute (not substitute) so that
    operator answers containing dollar signs (e.g. referencing other template
    variables by name, or literal dollar amounts) do not cause KeyError or
    silently corrupt the rendered output.
    """
    body = _read_template("persona/IDENTITY.md")

    rendered = Template(body).safe_substitute(
        {
            C.TEMPLATE_VAR_AGENT_NAME: "test-agent",
            C.TEMPLATE_VAR_MISSION: "$primary_goal is to help",
            C.TEMPLATE_VAR_SCOPE_IN: "everything",
            C.TEMPLATE_VAR_SCOPE_OUT: "nothing",
            C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL: "Cautious",
            C.TEMPLATE_VAR_AUTONOMY_READ_ONLY: "bypass",
            C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE: "allow_with_audit",
            C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT: "escalate",
            C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK: "escalate",
        }
    )

    # ${mission} must have been substituted (no longer present as a placeholder).
    assert f"${{{C.TEMPLATE_VAR_MISSION}}}" not in rendered, (
        "${mission} should have been substituted but was still found in rendered output"
    )

    # The literal dollar sign in the answer must survive intact (safe_substitute behavior).
    assert "$primary_goal is to help" in rendered, (
        "safe_substitute should leave $primary_goal intact when it is not a known variable"
    )

    # The substituted agent_name must appear.
    assert "test-agent" in rendered, (
        "Substituted value for agent_name should appear in rendered output"
    )


# ---------------------------------------------------------------------------
# Helpers for researcher / writer template tests
# ---------------------------------------------------------------------------


def _read_template_for(template_name: str, relpath: str) -> str:
    """Read a template file from the named template tree via importlib.resources."""
    base = resources.files("atomic_agents.init") / "templates" / template_name
    parts = relpath.split("/")
    node = base
    for part in parts:
        node = node / part
    return node.read_text(encoding="utf-8")


def _all_template_relpaths_for(template_name: str) -> list[str]:
    """Return all relative file paths in the named template tree."""
    base = resources.files("atomic_agents.init") / "templates" / template_name
    results: list[str] = []

    def _walk(node: object, parts: list[str]) -> None:
        for child in node.iterdir():  # type: ignore[attr-defined]
            child_parts = parts + [child.name]
            try:
                list(child.iterdir())  # type: ignore[attr-defined]
                is_dir = True
            except (NotADirectoryError, OSError):
                is_dir = False
            if is_dir:
                _walk(child, child_parts)
            else:
                results.append("/".join(child_parts))

    _walk(base, [])
    return results


_REQUIRED_FILES = [
    "persona/IDENTITY.md",
    "persona/SOUL.md",
    "persona/USER.md",
    "tools.md",
    "model.md",
    "memory/INDEX.md",
    "wiki/INDEX.md",
]


# ---------------------------------------------------------------------------
# Tests 1-2: required files present
# ---------------------------------------------------------------------------


def test_researcher_template_has_all_required_files() -> None:
    """All seven required template files must be present in the researcher tree."""
    base = resources.files("atomic_agents.init") / "templates" / "researcher"
    for relpath in _REQUIRED_FILES:
        parts = relpath.split("/")
        node = base
        for part in parts:
            node = node / part
        assert node.is_file(), f"Missing required researcher template file: {relpath}"


def test_writer_template_has_all_required_files() -> None:
    """All seven required template files must be present in the writer tree."""
    base = resources.files("atomic_agents.init") / "templates" / "writer"
    for relpath in _REQUIRED_FILES:
        parts = relpath.split("/")
        node = base
        for part in parts:
            node = node / part
        assert node.is_file(), f"Missing required writer template file: {relpath}"


# ---------------------------------------------------------------------------
# Tests 3-4: no em dashes
# ---------------------------------------------------------------------------


def test_researcher_template_no_em_dashes() -> None:
    """No researcher template file should contain an em dash character (U+2014)."""
    for relpath in _all_template_relpaths_for("researcher"):
        content = _read_template_for("researcher", relpath)
        assert "—" not in content, (
            f"Em dash found in researcher template file: {relpath}"
        )


def test_writer_template_no_em_dashes() -> None:
    """No writer template file should contain an em dash character (U+2014)."""
    for relpath in _all_template_relpaths_for("writer"):
        content = _read_template_for("writer", relpath)
        assert "—" not in content, f"Em dash found in writer template file: {relpath}"


# ---------------------------------------------------------------------------
# Tests 5-7: schema headers match actual template h2 headers
# ---------------------------------------------------------------------------


def _assert_schema_matches_template(template_name: str) -> None:
    """For each file in TEMPLATE_SECTION_SCHEMA[template_name], verify that the
    extracted h2 headers from the actual template file are a superset of the
    schema headers (every schema header is present in the file).
    """
    from atomic_agents.init.wizard import _extract_h2_headers

    schema = C.TEMPLATE_SECTION_SCHEMA[template_name]
    for relpath, expected_headers in schema.items():
        content = _read_template_for(template_name, relpath)
        extracted = _extract_h2_headers(content)
        extracted_set = set(extracted)
        expected_set = set(expected_headers)
        missing = expected_set - extracted_set
        assert not missing, (
            f"Template {template_name}/{relpath} is missing schema h2 headers: "
            f"{sorted(missing)}. Extracted: {sorted(extracted_set)}"
        )


def test_researcher_schema_matches_actual_files() -> None:
    """Every h2 header in TEMPLATE_SECTION_SCHEMA['researcher'] must appear in the file."""
    _assert_schema_matches_template("researcher")


def test_writer_schema_matches_actual_files() -> None:
    """Every h2 header in TEMPLATE_SECTION_SCHEMA['writer'] must appear in the file."""
    _assert_schema_matches_template("writer")


def test_advisor_schema_matches_actual_files() -> None:
    """Every h2 header in TEMPLATE_SECTION_SCHEMA['advisor'] must appear in the file."""
    _assert_schema_matches_template("advisor")


# ---------------------------------------------------------------------------
# Tests 8-9: Operating mode section in IDENTITY.md
# ---------------------------------------------------------------------------


def test_researcher_identity_has_operating_mode_section() -> None:
    """researcher IDENTITY.md must have '## Operating mode' and mention 'hybrid'."""
    content = _read_template_for("researcher", "persona/IDENTITY.md")
    assert "## Operating mode" in content, (
        "researcher IDENTITY.md missing '## Operating mode' section"
    )
    assert "hybrid" in content, (
        "researcher IDENTITY.md must mention 'hybrid' in the Operating mode section"
    )


def test_writer_identity_has_operating_mode_section() -> None:
    """writer IDENTITY.md must have '## Operating mode' and mention 'reactive'."""
    content = _read_template_for("writer", "persona/IDENTITY.md")
    assert "## Operating mode" in content, (
        "writer IDENTITY.md missing '## Operating mode' section"
    )
    assert "reactive" in content, (
        "writer IDENTITY.md must mention 'reactive' in the Operating mode section"
    )


# ---------------------------------------------------------------------------
# Test 10: writer model.md defaults to Sonnet, not Opus
# ---------------------------------------------------------------------------


def test_writer_model_md_defaults_sonnet() -> None:
    """writer model.md default model must be claude-sonnet-4-6, not claude-opus-4-7."""
    content = _read_template_for("writer", "model.md")
    assert "claude-sonnet-4-6" in content, (
        "writer model.md must default to claude-sonnet-4-6 (P2 lock)"
    )


# ---------------------------------------------------------------------------
# Test 11: writer tools.md has drafts/ and revisions/ paths
# ---------------------------------------------------------------------------


def test_writer_tools_md_has_drafts_and_revisions_paths() -> None:
    """writer tools.md must reference both 'drafts/' and 'revisions/' write paths."""
    content = _read_template_for("writer", "tools.md")
    assert "drafts/" in content, (
        "writer tools.md must contain 'drafts/' in the Write paths section"
    )
    assert "revisions/" in content, (
        "writer tools.md must contain 'revisions/' in the Write paths section"
    )


# ---------------------------------------------------------------------------
# Test 12: researcher IDENTITY.md has Research integrity section
# ---------------------------------------------------------------------------


def test_researcher_identity_has_research_integrity_section() -> None:
    """researcher IDENTITY.md must contain a '## Research integrity' section."""
    content = _read_template_for("researcher", "persona/IDENTITY.md")
    assert "## Research integrity" in content, (
        "researcher IDENTITY.md missing '## Research integrity' section"
    )


# ---------------------------------------------------------------------------
# Test 13: researcher tools.md has raw/ in Read paths
# ---------------------------------------------------------------------------


def test_researcher_tools_md_has_raw_read_path() -> None:
    """researcher tools.md must reference 'raw/' in the Read paths section."""
    content = _read_template_for("researcher", "tools.md")
    assert "raw/" in content, (
        "researcher tools.md must contain 'raw/' in the Read paths section"
    )


# ---------------------------------------------------------------------------
# Tests 14-15: constants completeness
# ---------------------------------------------------------------------------


def test_template_preset_defaults_all_three_present() -> None:
    """TEMPLATE_PRESET_DEFAULTS must contain advisor, researcher, and writer keys,
    all mapping to PRESET_CAUTIOUS.
    """
    for template_name in ("advisor", "researcher", "writer"):
        assert template_name in C.TEMPLATE_PRESET_DEFAULTS, (
            f"TEMPLATE_PRESET_DEFAULTS missing key: {template_name}"
        )
        assert C.TEMPLATE_PRESET_DEFAULTS[template_name] == C.PRESET_CAUTIOUS, (
            f"TEMPLATE_PRESET_DEFAULTS['{template_name}'] must be PRESET_CAUTIOUS"
        )


def test_template_section_schema_all_three_populated() -> None:
    """TEMPLATE_SECTION_SCHEMA must have advisor, researcher, and writer, each with 8 files.

    Count is 8 after spec/51 added governance.md to every template (was 7).
    """
    for template_name in ("advisor", "researcher", "writer"):
        assert template_name in C.TEMPLATE_SECTION_SCHEMA, (
            f"TEMPLATE_SECTION_SCHEMA missing key: {template_name}"
        )
        file_count = len(C.TEMPLATE_SECTION_SCHEMA[template_name])
        assert file_count == 8, (
            f"TEMPLATE_SECTION_SCHEMA['{template_name}'] has {file_count} files, expected 8"
        )


def test_governance_md_renders_with_no_leftover_placeholder() -> None:
    """The scaffolded governance.md MUST substitute the agent name, leaving no
    literal placeholder token in the rendered output.

    Regression guard for the spec/51 governance.md templates, which were
    authored with the bare ``$AGENT_NAME`` token instead of the locked
    ``${agent_name}`` brace convention (the only placeholder form
    safe_substitute resolves against the ``agent_name`` variable). The bare
    form survives substitution untouched, so every ``init --from-template``
    agent shipped a governance.md titled ``$AGENT_NAME`` instead of its name.

    Strip-RED negative control: revert any template's first line to
    ``# Governance : $AGENT_NAME`` and this test fails on the ``$`` assertion
    (the literal token survives and the name is absent from the title line).

    The brace-syntax helper ``_extract_template_vars`` only matches ``${var}``
    and would NOT catch a bare ``$AGENT_NAME``; this test asserts on the
    RENDERED output instead, so it catches BOTH placeholder syntaxes.
    """
    from atomic_agents.init.wizard import (
        _default_template_vars,
        _render_file_to_string,
    )

    agent_name = "caldwell"
    for template_name in ("advisor", "researcher", "writer"):
        template_vars = _default_template_vars(agent_name, template_name)
        rendered = _render_file_to_string(
            template_name, ["governance.md"], template_vars
        )
        first_line = rendered.splitlines()[0]
        assert agent_name in first_line, (
            f"{template_name}/governance.md title did not substitute the agent "
            f"name: {first_line!r}"
        )
        # No unresolved placeholder of EITHER syntax may survive: no bare
        # ``$NAME`` token and no ``${var}`` brace token. (A literal ``$`` that
        # is part of prose, e.g. a price, is not a placeholder; the templates
        # contain none, so a blanket ``$`` scan is safe and maximally strict.)
        assert "$" not in rendered, (
            f"{template_name}/governance.md has an unresolved placeholder "
            f"(literal '$' survived rendering): "
            f"{[ln for ln in rendered.splitlines() if '$' in ln]}"
        )
