---
name: canvas-week-plan
description: Student weekly planner for Canvas LMS. Shows what is due, what is overdue, grades, and pending peer reviews, then proposes an order of work. Use when a student says "what's due", "plan my week", "weekly check", or wants to organise their coursework.
---

# Canvas Week Plan

Produce an actionable plan for the next week across every enrolled course.

## Steps

### 1. Get the plan

`get_my_week_plan(days=7)` — one call. It returns overdue, due today/tomorrow,
and upcoming items with course, due date and points, plus what is already
submitted. Pass `course_identifier` to focus on one course.

### 2. Check grades

`get_my_grade_breakdown(course_identifier)` per course. For a course with a
close grade boundary, add `target_grade=90` to see what the remaining work has
to average.

### 3. Check what is missing

`get_my_submission_status()` — submitted vs missing, with external-tool
assignments (Gradescope and similar) kept separate because Canvas cannot see
their submission state.

### 4. Check peer reviews

`get_my_peer_reviews_todo()` — reviews assigned to the student, not completed.

### 5. Present the plan

```
## Your week

### Do first
- <overdue item> — <course>, due <date>, <pts>  (why: overdue)
- <due today item>

### Then
- <upcoming, ordered by date then points>

### Not urgent
- <further out>

### Grades
- CS 225: 87.4% — next 20 pts could move it to ~89%

### Peer reviews
- 2 pending
```

Ordering rules: overdue first, then due-soonest, then highest points among the
same due date. Say plainly when something is already submitted — do not list it
as work.

### 6. Offer the next step

For the top item, offer to run the `canvas-assignment-work` skill (open the
workspace, read the rubric, draft it). For a test, offer `canvas-study-guide`.

## Tools used

| Tool | Purpose |
|------|---------|
| `get_my_week_plan` | Prioritised deadlines |
| `get_my_grade_breakdown` | Per-group grades, target maths |
| `get_my_submission_status` | Missing vs submitted |
| `get_my_peer_reviews_todo` | Pending peer reviews |
| `get_my_course_grades` | Quick grade overview |
