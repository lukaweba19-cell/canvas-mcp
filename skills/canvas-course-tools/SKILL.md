---
name: canvas-course-tools
description: Find and read anything in a Canvas course — courses, modules, pages, files, assignments, discussions, announcements, messages, your own submissions and notes. Use when a student asks "what's in this course", "show me the modules/pages/announcements", "where is that file", "what did I submit", "check my messages", or wants a note saved, read or searched.
---

# Canvas Course Tools

Everything that is not already covered by `canvas-week-plan` (what is due),
`canvas-study-guide` (turn material into a guide) or `canvas-assignment-work`
(work one assignment end to end). Reach for this skill to find and read course
content, keep notes, and handle the small write actions around coursework.

## 1. Find the course first

- `list_courses()` — every course you are enrolled in, with ids and codes.
- `get_my_enrollments()` — enrollment state and role per course.
- `get_my_profile()` — who Canvas thinks you are (useful when outputs look odd).
- `search_canvas_tools(query=...)` — when you are unsure which tool exists,
  search the tool list instead of guessing a name.

Every other tool takes `course_identifier` as a course code or the numeric id,
so resolve the course once and reuse it.

## 2. Map the course

- `get_course_details(course_identifier)` — name, term, workflow state.
- `get_course_structure(course_identifier)` — modules, items and pages at once.
- `get_course_content_overview(course_identifier)` — counts and highlights
  (pages, modules, syllabus) without dumping every body.
- `get_front_page(course_identifier)` / `get_page_content(...)` /
  `get_page_details(...)` — the front page, a page's text, or its metadata.
- `list_pages(course_identifier)` — page titles and urls.
- `list_modules(course_identifier)` / `list_module_items(course_identifier, module_id)`
  — the weekly structure and each item.
- `list_announcements(course_identifier)` — instructor announcements.

For actual study text, do not stop at the map: `get_course_content_map` tells
you which files are readable, then `read_course_material` +
`read_material_section` (`canvas-study-guide`) pulls the text out.

## 3. Files

- `list_course_files(course_identifier, search_term=...)` — every file with a
  real content status: `readable — N words extracted`, `unread`,
  `NOT readable — image-only / empty / unsupported format / unsupported encoding
  / legacy .doc / file too large`. "Too large" means over the 60 MB extraction
  cap: download it and open it locally.
- `read_course_material(course_identifier, file_id, full_text=True)` — extract
  a file's text and get it back in the same call: PDF, scanned PDF via built-in
  OCR, PPTX with speaker notes, DOCX, XLSX, IPYNB, transcripts, and source
  files including Ruby/Rails (`.rb`, `.erb`, `.rake`). A PPTX always reads per
  slide. A PDF reads per slide (`Slide 1`, `Slide 2`, ...) only when every page
  repeats one landscape, slide-sized page box and the text is slide-sparse — a
  landscape report, table or wide handout stays `p.N`, so labels are never
  invented. `read_material_section(section="Slide 4")` works on either. Leave
  `full_text` off for a section index of a long deck; add `max_chars` to raise
  the budget. Long files stop at the first section that does not fit and the
  note counts the unread slides/pages by name.
- `download_course_file(course_identifier, file_id)` — only for a file this
  server cannot read (e.g. an archive), and only on a local stdio server; it
  writes to the server's disk.

A `NOT readable` status is information, not a failure: say what it is and what
would make it readable (conversion, a transcript, a better scan).

## 4. Assignments and your own work

- `list_assignments(course_identifier)` — every assignment with due dates.
- `get_rubric(course_identifier, assignment_id)` /
  `get_rubric_assessment(...)` — raw Canvas rubric and any rubric assessment
  attached to your submission. For the student-facing view use
  `get_my_assignment_rubric` (`canvas-assignment-work`).
- `get_my_submission(course_identifier, assignment_id)` — what you submitted.
- `list_assignment_drafts(course_identifier)` — drafts saved in the local
  workspace (`read_assignment_draft` reads one).
- `comment_on_my_submission(...)` — add a comment to your own submission
  (requires confirmation; the preview comes first).
- `mark_module_item_done(course_identifier, module_id, item_id)` — tick off an
  item you have finished.

## 5. Discussions

- `list_discussion_topics(course_identifier)` — topics and their ids.
- `get_discussion_topic_details(...)` — prompt, points, due date, entries count.
- `list_discussion_entries(...)` / `get_discussion_entry_details(...)` —
  entries and one entry's body.
- `get_discussion_with_replies(...)` — a whole thread in one call, for reading
  before replying.
- `post_discussion_entry(...)` / `reply_to_discussion_entry(...)` — write your
  own post or reply. Both preview first and need the confirmation token.

Never write a discussion post for the student without them approving the text.

## 6. Messages

- `list_conversations(...)` — Canvas inbox threads.
- `get_conversation_details(conversation_id)` — one thread, with messages.
- `get_unread_count()` — how much is waiting.
- `mark_conversations_read(conversation_id)` — clear it once handled.

Reading is safe; there is no send-message tool, so never promise to email or
message an instructor through Canvas.

## 7. Notes and study guides you already made

- `save_note(title, content, tags, course_identifier, kind=...)` — a note tied to
  a course (kinds include `source-summary`, `lecture`, `journal`).
- `search_notes(query)` / `list_notes()` / `read_note(title)` — find and read
  them again; search before writing a duplicate.
- `list_study_guides(course_identifier)` / `read_study_guide(course_identifier, title)`
  / `update_study_guide(...)` — reuse and revise a saved guide instead of
  building a second one.

## 8. Report honestly

End with: what you looked at, what you could not read and why (image-only,
legacy `.doc`, unsupported encoding, no transcript), and what is still missing
from the course (no rubric, no slides, empty module). Do not imply you read
something you only listed.

## Tools used

| Tool | Purpose |
|------|---------|
| `list_courses` | Courses you are enrolled in |
| `get_my_enrollments` | Enrollment and role per course |
| `get_my_profile` | Your Canvas identity |
| `search_canvas_tools` | Find the right tool by description |
| `get_course_details` | Course metadata |
| `get_course_structure` | Modules, items and pages together |
| `get_course_content_overview` | Counts and syllabus highlights |
| `get_front_page` | Course front page text |
| `list_pages` / `get_page_details` / `get_page_content` | Pages and their text |
| `list_modules` / `list_module_items` | Weekly structure |
| `list_announcements` | Instructor announcements |
| `list_course_files` | Files with a real readability status |
| `download_course_file` | Save an unreadable file locally |
| `list_assignments` | Assignments and due dates |
| `get_rubric` / `get_rubric_assessment` | Raw rubric data from Canvas |
| `get_my_submission` | What you submitted |
| `list_assignment_drafts` | Locally saved drafts |
| `comment_on_my_submission` | Comment on your own submission |
| `mark_module_item_done` | Tick off a module item |
| `list_discussion_topics` / `get_discussion_topic_details` | Discussion prompts |
| `list_discussion_entries` / `get_discussion_entry_details` | Entries and their text |
| `get_discussion_with_replies` | A full thread |
| `post_discussion_entry` / `reply_to_discussion_entry` | Your own posts |
| `list_conversations` / `get_conversation_details` | Canvas inbox |
| `get_unread_count` / `mark_conversations_read` | Inbox state |
| `save_note` / `search_notes` / `list_notes` / `read_note` | Notes |
| `list_study_guides` / `read_study_guide` / `update_study_guide` | Saved guides |
