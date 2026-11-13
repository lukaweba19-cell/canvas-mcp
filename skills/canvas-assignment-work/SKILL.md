---
name: canvas-assignment-work
description: Work an assignment end to end for a student — read the prompt and rubric, draft in their voice, keep notes, and submit only after they approve. Use when a student says "do my assignment", "work on this while I'm busy", "draft this for me", or "what's left for me to do".
---

# Canvas Assignment Work

Run one assignment from prompt to submission prep. The agent does the reading,
structuring and drafting; the student makes the decisions and approves anything
that goes into Canvas.

## Steps

### 1. Open the workspace

`start_assignment_work(course_identifier, assignment_id)`.

Writes `brief.md` and `rubric.md` into the assignment folder and returns the
prompt, due dates, submission types, allowed file types, attempts remaining and
current state. If it says **group assignment**, stop: agent submission is
refused by design.

Then read the rubric: `get_my_assignment_rubric(course_identifier, assignment_id)`
returns the criteria, their points and every rating level, from Canvas's attached
rubric or from one pasted into the prompt, and saves it beside the assignment so
later sessions and other tools can use it. If it reports **no rubric found**,
that is the true state — say so and ask the student what the marks are for
instead of inventing criteria.

### 2. Read everything before writing

- `get_my_assignment_brief` if you need the rubric and feedback together
- `read_course_material` / `read_material_section` for any source files the
  prompt names or the modules provide
- `get_my_feedback(course_identifier)` — last time's rubric comments are the
  best guide to what this instructor rewards
- `get_syllabus` — deadlines, late policy, formatting rules
- `get_my_assignment_rubric` — the criteria the marks come from

### 3. Plan, then draft

Save an outline first: `save_assignment_draft(..., kind="outline")`.
Then the draft: `save_assignment_draft(..., kind="draft")`. Every save is a new
version; nothing is overwritten, so the student can compare.

Match the student's own voice:
- `save_voice_sample(course_identifier=..., assignment_ids=...)` on their past
  submissions, once
- `check_draft_style(course_identifier=..., assignment_id=...)` before calling it
  done — it flags stock phrasing ("it is important to note", "moreover"), uniform
  sentence length, bullets inside prose, and unsupported hedges

Every `save_assignment_draft` reports rubric coverage ("2/3 criteria
referenced; not found: ..."). For the full criterion-by-criterion verdict use
`check_work_against_rubric` on the draft, on the submission, or on pasted text.
It is a keyword checklist, not a grade: fix what it reports as not found, then
re-check. A draft that ignores a criterion cannot score it.

Do not write in a voice the student would not recognise as theirs, and do not pad
to hit a word count. If the draft needs a fact the material does not contain, put
it in a note as a question instead of inventing it.

### 4. Keep the student in the loop

- `save_note(..., kind="question")` for anything only they can answer
- `save_note(..., kind="decision")` for choices you made on their behalf
- `add_work_log_entry(summary=..., needs_you=..., next_step=...)` after each session

When they come back, `get_my_handoff()` reports what was done, what is blocked,
questions still open, and drafts waiting for review.

### 5. Submit — only with approval

`submit_assignment(course_identifier, assignment_id, submission_type="online_text_entry", from_draft=true)`.

That returns a **preview and a single-use token; nothing is submitted**. Show the
preview to the student. Only after they explicitly approve, call again with the
same arguments plus `confirmation_token`.

- The token expires in 5 minutes, is single-use, and stops matching if the draft
  or attempt count changed — if it is rejected, re-preview, do not retry blindly.
- Submitting spends an attempt the student may not get back.
- Never submit a quiz, a group assignment, or anything the student has not seen.
- If a course blocks agent writes (its syllabus says `agent_writes: deny`), relay
  that reason and stop.

### 6. Finish

`add_work_log_entry` with what was submitted and what remains. If anything about
the submission was uncertain (a lost reply, an upload the tool could not
confirm), say so plainly and tell the student to check Canvas before retrying.

## Tools used

| Tool | Purpose |
|------|---------|
| `start_assignment_work` | Workspace, brief, rubric |
| `get_my_assignment_rubric` | Criteria, points, rating levels (Canvas or prompt) |
| `check_work_against_rubric` | Per-criterion verdict on draft/submission/text |
| `get_my_assignment_brief` | Prompt + rubric + state in one call |
| `read_course_material`, `read_material_section` | Source material |
| `get_my_feedback` | Rubric feedback from past work |
| `save_assignment_draft`, `read_assignment_draft` | Versioned drafting |
| `save_voice_sample`, `check_draft_style` | Sound like the student |
| `save_note`, `list_notes`, `add_work_log_entry`, `get_my_handoff` | Notes and continuity |
| `submit_assignment` | Preview, then submit on approval |
