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
