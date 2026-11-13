"""Course material: the index, disk cache reuse/invalidation, and honest LTI labels."""

from __future__ import annotations

import pytest
from fixtures import build_pptx
from helpers import call

from canvas_mcp.tools import materials as materials_module

COURSE = {"id": 225, "course_code": "CS225", "name": "Algorithms"}

FILES = [
    {"id": 900, "display_name": "Lecture 3.pptx", "filename": "Lecture 3.pptx"},
    {"id": 901, "display_name": "week3-lecture.vtt", "filename": "week3-lecture.vtt"},
    {"id": 902, "display_name": "intro.mp4", "filename": "intro.mp4"},
]

MODULES = [
    {
        "id": 1,
        "name": "Week 3",
        "items": [
            {"id": 11, "type": "File", "title": "Lecture 3 slides", "content_id": 900},
            {"id": 12, "type": "File", "title": "Intro video file", "content_id": 902},
            {
                "id": 13,
                "type": "ExternalTool",
                "title": "Week 3 Lecture (Panopto)",
                "external_url": "https://panopto.example/launch",
            },
            {
                "id": 14,
                "type": "ExternalUrl",
                "title": "Optional reading",
                "external_url": "https://example.com/reading",
            },
            {"id": 15, "type": "Quiz", "title": "Quiz 3", "content_id": 77},
            {"id": 16, "type": "Page", "title": "Notes", "page_url": "notes"},
        ],
    }
]

SLIDES = build_pptx(
    [
        {"text": "Base case first", "notes": "Exam favourite"},
        {"text": "Recursion depth"},
    ]
)


@pytest.fixture
def course(canvas):
    """A course with slides, a caption track, a video, and an LTI item."""
    canvas.route("get", "/courses/225/files/900", {
        "id": 900,
        "display_name": "Lecture 3.pptx",
        "url": "https://files.example/900",
    })
    canvas.route("get", "/courses/225/files", FILES)
    canvas.route("get", "/courses/225/modules", MODULES)
    canvas.route("get", "/courses/225", COURSE)
    return canvas


@pytest.fixture
def downloads(monkeypatch):
    """Count how many times the file bytes are actually fetched."""
    calls: list[str] = []

    async def fake(url: str):
        calls.append(url)
        return SLIDES, None

    monkeypatch.setattr(materials_module, "_download_course_file_bytes", fake)
    return calls


async def test_content_map_marks_lti_and_offers_the_transcript(course, mcp):
    report = await call(mcp, "get_course_content_map", course_identifier=225)

    panopto = next(line for line in report.splitlines() if "Panopto" in line)
    assert "NOT readable" in panopto
    # The item cannot be read, but a transcript does exist, so say which file.
    assert "file_id=901" in panopto
    assert "week3-lecture.vtt" in panopto

    external = next(line for line in report.splitlines() if "Optional reading" in line)
    assert "NOT readable" in external
    assert "https://example.com/reading" in external

    quiz = next(line for line in report.splitlines() if "[Quiz]" in line)
    assert "NOT readable" in quiz


async def test_content_map_never_marks_a_video_file_readable(course, mcp):
    report = await call(mcp, "get_course_content_map", course_identifier=225)

    video = next(line for line in report.splitlines() if "intro.mp4" in line)
    assert "NOT readable as text" in video

    slides = next(line for line in report.splitlines() if "Lecture 3.pptx" in line)
    assert "readable" in slides


async def test_content_map_lists_captions_as_readable(course, mcp):
    report = await call(mcp, "get_course_content_map", course_identifier=225)

    assert "## Captions and transcripts" in report
    assert "readable (captions/transcript)" in report


async def test_read_course_material_returns_an_index_not_the_whole_deck(course, downloads, mcp):
    result = await call(mcp, "read_course_material", course_identifier=225, file_id=900)

    assert "Lecture 3.pptx" in result
    assert "Sections:" in result
    assert "Slide 1" in result
    assert "read_material_section" in result
    assert downloads == ["https://files.example/900"]


async def test_second_read_is_served_from_cache(course, downloads, mcp):
    first = await call(mcp, "read_course_material", course_identifier=225, file_id=900)
    second = await call(mcp, "read_course_material", course_identifier=225, file_id=900)

    assert len(downloads) == 1, "the file must not be downloaded twice"
    assert first.splitlines()[0] == second.splitlines()[0]
    assert "Cache:" in second


async def test_refresh_invalidates_the_cache(course, downloads, mcp):
    await call(mcp, "read_course_material", course_identifier=225, file_id=900)
    await call(
        mcp, "read_course_material", course_identifier=225, file_id=900, refresh=True
    )

    assert downloads == ["https://files.example/900", "https://files.example/900"]


async def test_read_material_section_returns_the_slide(course, downloads, mcp):
    await call(mcp, "read_course_material", course_identifier=225, file_id=900)

    result = await call(
        mcp, "read_material_section", course_identifier=225, file_id=900, section="Slide 1"
    )

    assert "Base case first" in result
    assert "Exam favourite" in result, "speaker notes must survive into the section"


async def test_unknown_section_lists_what_exists(course, downloads, mcp):
    await call(mcp, "read_course_material", course_identifier=225, file_id=900)

    result = await call(
        mcp, "read_material_section", course_identifier=225, file_id=900, section="Slide 99"
    )

    assert "No section 'Slide 99'" in result
    assert "Slide 1" in result


async def test_reading_a_section_before_extraction_says_what_to_do(course, mcp):
    result = await call(
        mcp, "read_material_section", course_identifier=225, file_id=900, section="Slide 1"
    )
    assert "read_course_material" in result


async def test_non_numeric_file_id_is_refused(course, mcp):
    result = await call(mcp, "read_course_material", course_identifier=225, file_id="../../etc")
    assert "numeric Canvas file ID" in result


async def test_unreadable_file_type_says_what_to_do_instead(course, downloads, mcp, monkeypatch):
    course.route("get", "/courses/225/files/902", {
        "id": 902, "display_name": "intro.mp4", "url": "https://files.example/902",
    })

    async def video_bytes(url: str):
        return b"\x00\x01\x02video", None

    monkeypatch.setattr(materials_module, "_download_course_file_bytes", video_bytes)

    result = await call(mcp, "read_course_material", course_identifier=225, file_id=902)

    assert "cannot be read as text at all" in result
    assert ".vtt" in result, "a video must point at its transcript, not just fail"
    assert "get_course_content_map" in result


async def test_read_page_text_returns_prose_and_caches_it(course, mcp):
    course.route("get", "/courses/225/pages/notes", {
        "title": "Week 3 notes",
        "body": "<h1>Hash maps</h1><p>Expected O(1) lookup.</p><script>x()</script>",
    })

    result = await call(mcp, "read_page_text", course_identifier=225, page_url_or_id="notes")

    assert "Hash maps" in result
    assert "Expected O(1) lookup." in result
    assert "Cached:" in result
    assert "x()" not in result, "script contents must not read as prose"
