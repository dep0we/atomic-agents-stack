"""Tests for ``atomic_agents/persona_link_md.py`` -- the ``persona.link.md``
parser (D-ER-4 + D-PP-5 of spec/33, PR 2 of #62).

Covers:

- **Happy paths**: valid file with required fields; operator markdown body
  below the code block ignored; full charset coverage in ``persona_id``.
- **Code-fence handling**: missing fence raises; first fence wins when two
  blocks are present.
- **YAML parse errors**: malformed YAML; non-mapping top-level.
- **Required fields**: missing ``kind`` / ``persona_id`` / unsupported kind.
- **Charset / traversal refusal**: leading dot, ``..``, path separator,
  control character, colon (proves the D-ER-4 ``kind:persona_id``
  single-scalar shape would have been rejected).
- **File-level**: ``parse_persona_link_md(Path)`` reads from disk; size cap
  defends against alias-bomb DoS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.exceptions import PersonaLinkInvalid
from atomic_agents.persona_link_md import (
    MAX_PERSONA_LINK_MD_BYTES,
    PersonaLink,
    parse_persona_link_md,
    parse_persona_link_md_text,
)

# ─────────────────────────────────────────────────────────────────────────────
# Happy paths


def test_valid_file_parses_to_expected_persona_link() -> None:
    text = (
        "# Persona link\n"
        "\n"
        "```yaml\n"
        "kind: shared\n"
        "persona_id: customer-support-v3\n"
        "```\n"
    )
    link = parse_persona_link_md_text(text)
    assert link == PersonaLink(kind="shared", persona_id="customer-support-v3")


def test_markdown_body_after_code_block_is_ignored() -> None:
    text = (
        "# Persona link\n"
        "\n"
        "```yaml\n"
        "kind: shared\n"
        "persona_id: customer-support-v3\n"
        "```\n"
        "\n"
        "## Why this agent links here\n"
        "\n"
        "Operator-authored prose explaining the link choice.\n"
        "Multiple paragraphs are fine -- the parser ignores everything\n"
        "outside the first fenced YAML block.\n"
    )
    link = parse_persona_link_md_text(text)
    assert link.kind == "shared"
    assert link.persona_id == "customer-support-v3"


def test_persona_id_full_charset_coverage_parses_cleanly() -> None:
    """Charset is ``[a-zA-Z0-9_.+@-]+``; exercise letters, digits, ``_``,
    ``.``, ``+``, ``@``, ``-`` all in one id."""
    text = "```yaml\nkind: shared\npersona_id: analyst.v2+test_3@fleet\n```\n"
    link = parse_persona_link_md_text(text)
    assert link.persona_id == "analyst.v2+test_3@fleet"


# ─────────────────────────────────────────────────────────────────────────────
# Code-fence handling


def test_no_yaml_code_block_raises() -> None:
    text = "# Persona link\n\nkind: shared\npersona_id: customer-support-v3\n"
    with pytest.raises(PersonaLinkInvalid, match="no YAML code block found"):
        parse_persona_link_md_text(text)


def test_two_code_blocks_uses_the_first() -> None:
    text = (
        "```yaml\n"
        "kind: shared\n"
        "persona_id: first-block-wins\n"
        "```\n"
        "\n"
        "Some prose between blocks.\n"
        "\n"
        "```yaml\n"
        "kind: shared\n"
        "persona_id: second-block-ignored\n"
        "```\n"
    )
    link = parse_persona_link_md_text(text)
    assert link.persona_id == "first-block-wins"


# ─────────────────────────────────────────────────────────────────────────────
# YAML parse errors


def test_malformed_yaml_in_code_block_raises() -> None:
    text = "```yaml\nkind: shared\npersona_id: [unterminated\n```\n"
    with pytest.raises(PersonaLinkInvalid, match="malformed YAML"):
        parse_persona_link_md_text(text)


def test_yaml_top_level_list_raises() -> None:
    text = "```yaml\n- kind: shared\n- persona_id: customer-support-v3\n```\n"
    with pytest.raises(PersonaLinkInvalid, match="YAML top-level must be a mapping"):
        parse_persona_link_md_text(text)


# ─────────────────────────────────────────────────────────────────────────────
# Required fields


def test_missing_kind_field_raises() -> None:
    text = "```yaml\npersona_id: customer-support-v3\n```\n"
    with pytest.raises(PersonaLinkInvalid, match="missing required field 'kind'"):
        parse_persona_link_md_text(text)


def test_missing_persona_id_field_raises() -> None:
    text = "```yaml\nkind: shared\n```\n"
    with pytest.raises(PersonaLinkInvalid, match="missing required field 'persona_id'"):
        parse_persona_link_md_text(text)


def test_unsupported_kind_template_raises() -> None:
    text = "```yaml\nkind: template\npersona_id: customer-support-v3\n```\n"
    with pytest.raises(PersonaLinkInvalid, match="is not supported"):
        parse_persona_link_md_text(text)


# ─────────────────────────────────────────────────────────────────────────────
# Charset / traversal refusal


def test_persona_id_leading_dot_raises() -> None:
    text = "```yaml\nkind: shared\npersona_id: .hidden\n```\n"
    with pytest.raises(PersonaLinkInvalid, match=r"must not start with '\.'"):
        parse_persona_link_md_text(text)


def test_persona_id_double_dot_raises() -> None:
    text = "```yaml\nkind: shared\npersona_id: customer..support\n```\n"
    with pytest.raises(PersonaLinkInvalid, match=r"must not contain '\.\.'"):
        parse_persona_link_md_text(text)


def test_persona_id_path_separator_raises() -> None:
    text = "```yaml\nkind: shared\npersona_id: customer/support\n```\n"
    with pytest.raises(PersonaLinkInvalid, match="path separators"):
        parse_persona_link_md_text(text)


def test_persona_id_control_character_raises() -> None:
    # Tab char embedded via YAML's ``\t`` escape -- PyYAML accepts the escape,
    # the parser's ``_CONTROL_CHARS`` (``[\x00-\x1f\x7f]``) then catches it
    # before the charset regex.
    text = '```yaml\nkind: shared\npersona_id: "customer\\tsupport"\n```\n'
    with pytest.raises(PersonaLinkInvalid, match="control characters"):
        parse_persona_link_md_text(text)


def test_persona_id_colon_rejected_by_charset() -> None:
    """Load-bearing per D-ER-4: the original ``kind:persona_id`` single-scalar
    shape (e.g. ``shared:customer-support``) would have been rejected by the
    charset; the two-field shape is structural, not stylistic."""
    text = '```yaml\nkind: shared\npersona_id: "shared:customer-support"\n```\n'
    with pytest.raises(PersonaLinkInvalid, match="must match"):
        parse_persona_link_md_text(text)


# ─────────────────────────────────────────────────────────────────────────────
# File-level


def test_parse_persona_link_md_reads_from_disk(tmp_path: Path) -> None:
    link_file = tmp_path / "persona.link.md"
    link_file.write_text(
        "# Persona link\n"
        "\n"
        "```yaml\n"
        "kind: shared\n"
        "persona_id: customer-support-v3\n"
        "```\n",
        encoding="utf-8",
    )
    link = parse_persona_link_md(link_file)
    assert link == PersonaLink(kind="shared", persona_id="customer-support-v3")


def test_file_exceeding_size_cap_raises(tmp_path: Path) -> None:
    link_file = tmp_path / "persona.link.md"
    # Write past the 256 KiB cap. Content body is irrelevant -- the stat-size
    # check fires before the parser opens the bytes.
    padding = "x" * (MAX_PERSONA_LINK_MD_BYTES + 1)
    link_file.write_text(padding, encoding="utf-8")
    with pytest.raises(PersonaLinkInvalid, match="size cap"):
        parse_persona_link_md(link_file)
