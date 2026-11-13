"""What the running server actually exposes, and that the skills match it."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Every tool the student feature set is supposed to add.
NEW_TOOLS = {
    # reading course material
    "get_course_content_map",
    "read_course_material",
    "read_material_section",
    "read_page_text",
    # study guides
    "build_study_guide",
    "update_study_guide",
    "list_study_guides",
    "read_study_guide",
    "make_practice_test",
    # assignment workspace
    "start_assignment_work",
    "save_assignment_draft",
    "read_assignment_draft",
    "list_assignment_drafts",
    # notes / journal / voice
    "save_note",
    "search_notes",
    "list_notes",
    "read_note",
    "add_work_log_entry",
    "get_my_handoff",
    "save_voice_sample",
    "check_draft_style",
    # rubrics
    "get_my_assignment_rubric",
    "check_work_against_rubric",
    # deeper student reads
    "get_my_assignment_brief",
    "get_my_grade_breakdown",
    "get_my_week_plan",
    "get_my_feedback",
}

# The capabilities the user asked for, each mapped to the tools that provide it.
CAPABILITIES = {
    "read pdfs and slides": {
        "get_course_content_map",
        "read_course_material",
        "read_material_section",
    },
    "read course pages as text": {"read_page_text"},
    "compressed markdown study guides": {"build_study_guide", "update_study_guide"},
    "practice tests": {"make_practice_test"},
    "take notes": {"save_note", "search_notes", "read_note"},
    "work assignments alone": {
        "start_assignment_work",
        "save_assignment_draft",
        "add_work_log_entry",
        "get_my_handoff",
    },
    "sound human when writing": {"save_voice_sample", "check_draft_style"},
    "write to the rubric": {
        "get_my_assignment_rubric",
        "check_work_against_rubric",
        "build_study_guide",
    },
    "submit assignments": {"submit_assignment", "get_my_submission"},
    "grades, deadlines, feedback": {
        "get_my_assignment_brief",
        "get_my_grade_breakdown",
        "get_my_week_plan",
        "get_my_feedback",
    },
}

# Nothing from the educator half may come back.
EDUCATOR_MARKERS = (
    "grade_with_rubric",
    "list_submissions",
    "create_assignment",
    "delete_assignment",
    "bulk_update",
    "analytics",
    "accessibility",
    "ufixit",
    "execute_typescript",
    "check_enrollment",
    "content_migration",
    "peer_review_comments",
    "send_bulk",
)


async def _registered(mcp) -> set[str]:
    return {tool.name for tool in await mcp.list_tools(run_middleware=False)}


async def test_every_new_tool_is_reachable(mcp):
    names = await _registered(mcp)
    assert NEW_TOOLS <= names, f"missing: {sorted(NEW_TOOLS - names)}"


async def test_each_requested_capability_has_a_tool(mcp):
    names = await _registered(mcp)
    for capability, tools in CAPABILITIES.items():
        assert tools <= names, f"{capability} is missing {sorted(tools - names)}"


async def test_no_educator_tools_are_exposed(mcp):
    names = await _registered(mcp)
    leaked = {name for name in names if any(marker in name for marker in EDUCATOR_MARKERS)}
    assert not leaked, f"educator tools leaked back in: {sorted(leaked)}"


async def test_tool_count_is_pinned(mcp):
    names = await _registered(mcp)
    assert len(names) == 69, sorted(names)


async def test_new_tool_schemas_are_usable(mcp):
    readers = (await mcp.get_tool("read_course_material")).parameters
    assert set(readers["properties"]) == {"course_identifier", "file_id", "refresh"}
    assert {"course_identifier", "file_id"} <= set(readers["required"])

    submit = (await mcp.get_tool("submit_assignment")).parameters
    assert "from_draft" in submit["properties"]

    guide = (await mcp.get_tool("build_study_guide")).parameters
    assert {"title", "markdown", "max_pages"} <= set(guide["properties"])


def _skill_dirs() -> list[Path]:
    return sorted(path for path in (ROOT / "skills").iterdir() if path.is_dir())


def _front_matter(text: str) -> dict[str, str]:
    assert text.startswith("---"), "SKILL.md must start with YAML front matter"
    end = text.index("---", 3)
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


def test_skills_are_discoverable_and_well_formed():
    skills = _skill_dirs()
    assert {path.name for path in skills} == {
        "canvas-week-plan",
        "canvas-study-guide",
        "canvas-assignment-work",
    }
    for path in skills:
        text = (path / "SKILL.md").read_text(encoding="utf-8")
        meta = _front_matter(text)
        assert meta.get("name") == path.name, f"{path} name must match its directory"
        assert len(meta.get("description", "")) > 40, f"{path} needs a real description"


async def test_skills_only_reference_tools_that_exist(mcp):
    names = await _registered(mcp)
    for path in _skill_dirs():
        text = (path / "SKILL.md").read_text(encoding="utf-8")
        referenced = set()
        for line in text.splitlines():
            if line.strip().startswith("|"):
                referenced.update(re.findall(r"`([a-z][a-z0-9_]+)`", line))
        unknown = {tool for tool in referenced if tool not in names}
        assert not unknown, f"{path.name} references unknown tools: {sorted(unknown)}"


def test_env_template_documents_the_workspace_and_write_switches():
    text = (ROOT / "env.template").read_text(encoding="utf-8")
    assert "CANVAS_STUDENT_DIR" in text
    assert "STUDENT_WRITE_TOOLS" in text


def test_ocr_ships_as_a_normal_dependency():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project.optional-dependencies]" not in text, "nothing should be optional"
    dependencies = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "rapidocr" in dependencies
    assert "pypdfium2" in dependencies
    assert "pillow" in dependencies
    assert "pypdf" in dependencies
