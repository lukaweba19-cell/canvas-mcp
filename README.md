# Canvas MCP

A student-focused Model Context Protocol server for Canvas LMS. It acts as the
signed-in student: it reads courses, material, assignments, quizzes, grades,
discussions and messages, keeps a local study workspace, and can make a small
set of writes that only ever touch the student's own work.

Authentication is the student's Canvas **browser session**, captured from Chrome.
There is no API token. Many institutions disable student token creation, while
Canvas's own web UI uses the same `/api/v1` API with a session cookie.

## Quick start

```bash
uv sync
cp env.template .env         # set CANVAS_API_URL at minimum
uv run canvas-mcp-server     # streamable HTTP on 127.0.0.1:8819/mcp
```

Point an MCP client at `http://127.0.0.1:8819/mcp`. For clients that spawn the
server themselves:

```bash
uv run canvas-mcp-server --transport stdio
```

On first use the server opens Chrome with a dedicated profile; sign in to your
institution's Canvas once, and the session is stored owner-only under
`~/.canvas-mcp/session.json`. On a headless machine set `CANVAS_AUTH_NO_BROWSER=true`
and capture the session elsewhere first. Cleartext HTTP is refused except for
loopback development (`CANVAS_ALLOW_INSECURE_HTTP=true`).

## What is supported

75 tools, grouped roughly as:

- **Courses and material** — courses, modules, pages, files, announcements,
  discussions, messages. PDF, PPTX, DOCX, XLSX, IPYNB, transcripts, Rails zips
  and legacy Office files can be read as text; image-only PDFs use built-in OCR.
- **Assignments and grades** — briefs, rubrics, submissions, feedback, grade
  breakdowns, week plans and upcoming work.
- **Study workspace** — study guides, practice tests, notes, journal, drafts,
  voice samples and local style checks.
- **Quizzes** — quiz discovery, details and the student's own attempts, plus an
  opt-in Classic Quizzes attempt flow (below).
- **Writes** — submitting an assignment, commenting on your own submission and
  marking a module item done, each with a preview/confirmation token, narrowed
  by `STUDENT_WRITE_TOOLS` and by per-course instructor policy.

Content maps and week plans report each file's real extraction status, so an
unread or unsupported file is never called readable.

## Quizzes

### Reading — always on

- `list_quizzes(course_identifier=None)` — one course or all active courses.
  Each quiz shows due date, availability window, time limit, attempts used and
  allowed, points, engine (Classic or New Quizzes), whether it is published,
  and the student's attempts/scores. Planner-only quiz due items are included,
  so a quiz that never appears in the Classic listing still shows up as work to
  do.
- `get_quiz_details(course_identifier, quiz_id)` — rules, description,
  access-code requirement, question count and attempt history.
- `get_my_quiz_attempts(course_identifier, quiz_id)` — attempts, scores,
  submission state and remaining attempts.

Reading is never gated by the taking flag.

### Taking — opt-in, off by default

Set:

```dotenv
CANVAS_ENABLE_QUIZ_TAKING=true
```

This is separate from `STUDENT_WRITE_TOOLS`; enabling quiz reading never enables
taking. With the flag off, the three taking tools are still registered so they
can explain how to enable them, and they make no Canvas write.

With the flag on and a **Classic Quiz**:

1. `start_quiz_attempt(course_identifier, quiz_id, access_code="")` — consumes
   one attempt and returns the quiz submission id, attempt number, validation
   token and questions.
2. `answer_quiz_questions(..., quiz_submission_id, validation_token, attempt, answers)`
   — saves answers (`[{"id": ..., "answer": ...}]`); repeatable. This does not
   submit.
3. `submit_quiz_attempt(..., quiz_submission_id, validation_token, attempt)` —
   completes and grades the attempt. This cannot be undone.

Keep the returned id/token/attempt between calls. Attempts are subject to the
usual per-course instructor policy for agent writes.

### New Quizzes — refused honestly

Canvas exposes **no student start/answer/submit API for New Quizzes**. Those
quizzes are listed and can be read, but `start_quiz_attempt`,
`answer_quiz_questions` and `submit_quiz_attempt` return a clear refusal telling
the student to open the quiz in Canvas. No attempt is created and no fake
success is returned.

## Configuration

See `env.template` for the full list. The settings that change behaviour most:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CANVAS_API_URL` | required | Institution Canvas API base, `https://.../api/v1` |
| `CANVAS_SESSION_FILE` | `~/.canvas-mcp/session.json` | Captured browser session |
| `CANVAS_AUTH_NO_BROWSER` | `false` | Never open Chrome; explain how to capture a session |
| `CANVAS_ALLOW_INSECURE_HTTP` | `false` | Loopback HTTP only, for development |
| `STUDENT_WRITE_TOOLS` | submit, comment, module done | Which Canvas write tools exist |
| `CANVAS_ENABLE_QUIZ_TAKING` | `false` | Allow Classic quiz attempts (reading is unaffected) |
| `CANVAS_STUDENT_DIR` | `~/.canvas-mcp/student` | Local notes, drafts, guides and journal |
| `COURSE_AGENT_POLICY_DEFAULT` | `allow` | Posture when a course states no policy |

An instructor can deny agent writes in a course by putting `agent_writes: deny`
in the syllabus; quiz taking obeys the same policy.

## Tests

```bash
uv run pytest -q
uv run ruff check src tests
```

The suite includes an end-to-end fake Canvas over real streamable-HTTP
transport: it covers the full tool registry, cookie-session authentication,
CSRF on writes, session-expiry handling for every Canvas-backed tool, offline
download isolation, quiz discovery/attempts, a complete Classic
start/answer/submit cycle with the flag on, the flag-off refusal, and the
New Quizzes refusal. No real institution's Canvas is part of the suite.

## Limits

- New Quizzes cannot be taken through Canvas's API (above).
- This server does not record or upload media; attach media in Canvas itself.
- Downloads are only available on a local stdio server, because they write to
  the server's disk.
- The captured session is a real logged-in session: anyone who can read the
  session file can act as the student. It is stored owner-only and never sent
  to non-Canvas hosts.

## License

MIT. See `LICENSE`.
