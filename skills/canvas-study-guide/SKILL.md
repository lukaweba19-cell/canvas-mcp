---
name: canvas-study-guide
description: Build exam-focused study guides from a Canvas course's slides, PDFs and pages. Use when a student says "make me a study guide", "I have a test on X", "summarise the lectures", or wants notes compressed to only what they need to pass.
---

# Canvas Study Guide

Turn a course's actual material into a short, exam-weighted markdown guide the
student can revise from — not a summary of everything, only what carries marks.

## Steps

### 1. Find what exists

`get_course_content_map(course_identifier)`.

This lists every module item and file with whether its text is readable. Note
which files are `readable` and which are `NOT readable as text` (images, video,
Panopto/Kaltura links). Those cannot be studied this way and must be named in
the coverage report.

### 2. Read the material

For each relevant file: `read_course_material(course_identifier, file_id)`.

That returns an index (slides/pages with word counts), not the text. Then read
the sections that matter: `read_material_section(course_identifier, file_id,
section="Slide 12")`.

Pages: `read_page_text(course_identifier, page_url_or_id)`.
Assignments (past and current): `get_assignment_details` / `get_my_assignment_brief`.
Past exams, if posted as files, are the highest-value source — read those first.

Slide **speaker notes** are included in the extraction. They usually contain the
professor's own explanation of what matters; weight them heavily.

### 3. Decide what is examinable

Before writing, collect:
- What the exam covers and what is excluded (from announcements, syllabus, pages)
- Anything explicitly flagged: "this will be on the exam", "important", "remember"
- What repeats across decks, or appears in learning objectives
- The weight of the assessment (`get_my_grade_breakdown`) — a 5% quiz does not
  deserve the same compression effort as a 30% final

### 4. Write the guide

Call `build_study_guide(course_identifier, title, markdown=..., sources=...,
exam_date=..., max_pages=3)`. With empty markdown it returns the required
skeleton — fill that in.

Rules that make the guide useful:
- **One idea per bullet.** No paragraphs.
- **Cut** history, anecdotes, admin, and anything the course did not flag.
- **Keep** definitions, formulas, procedures, worked examples, and traps.
- Every claim carries a source marker: `[deck 4, slide 12]` or `[reading 2, p.7]`.
- A hard page budget (default 3). If it is over, cut again — do not raise the
  budget to fit more in.
- Section 7 is a self-test; answers go inside `<details>`.

If a section has no source behind it, delete it. A short honest guide beats a
long one with invented content.

### 5. Practice test

`make_practice_test(course_identifier, title, markdown=..., num_questions=20)`.

Questions must come from the guide, mix recall with application, and include an
`## Answers` section wrapped in `<details>`. The tool reports missing answers or
too few questions.

### 6. Record what you did

- `save_note(title, content, tags, course_identifier, kind="source-summary")` for
  per-source notes worth keeping.
- `add_work_log_entry(summary=..., course_identifier=..., needs_you=...)` so
  `get_my_handoff` can report the guide on the student's return.

### 7. Report coverage honestly

End with: what was read, what could not be read (and why), how many pages the
guide is, and which sections are thin. Never imply a complete guide when some
material was unreadable.

## Tools used

| Tool | Purpose |
|------|---------|
| `get_course_content_map` | What exists and is readable |
| `read_course_material` | Extract a file, get its index |
| `read_material_section` | Read one slide/page |
| `read_page_text` | Course page as plain text |
| `get_assignment_details`, `get_my_assignment_brief` | Past exams, prompts, rubrics |
| `get_my_grade_breakdown` | How much the assessment is worth |
| `build_study_guide` | Save/normalise the `.md` guide |
| `make_practice_test` | Self-test with answer key |
| `save_note`, `add_work_log_entry` | Record decisions and progress |
