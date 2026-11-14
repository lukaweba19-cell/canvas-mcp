"""Rubrics: read them from Canvas, or from the assignment prompt, and use them.

Canvas stores a rubric as structured data when the instructor attached one. When
they did not, the criteria are usually pasted into the assignment description as
a table or a bulleted list that still says how the marks are allocated. Both are
parsed here into one shape so the rest of the server can answer three questions:

1. What is this assignment actually marked on? (criteria, points, rating levels)
2. Does this draft/guide/submission cover each criterion? (a keyword-coverage
   verdict, never a grade)
3. If nothing exists, say so plainly rather than showing an empty section.

The coverage check is deliberately mechanical and honest about it: it reports
which criteria the text does not mention, with the words it looked for. It is a
checklist aid, not an assessment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import extract, student_store

_STOPWORDS = frozenset(
    """
    a an and are as at be been being but by can could did do does for from had has
    have how if in into is it its may might must no not of on or our shall should
    so some such than that the their them then there these they this those to too
    up us use used using was we were what when where which while who why will with
    within would you your about above after again all also any because before
    below between both during each few more most other over same very via
    """.split()
)

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'-]{2,}")
_POINTS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:pts?|points?)?", re.IGNORECASE)
_POINTS_AFTER = re.compile(
    r"(?:\(|\[|—|–|-|:)?\s*(?:worth\s+)?(\d+(?:\.\d+)?)\s*(?:pts?|points?)\b",
    re.IGNORECASE,
)
_RUBRIC_HEADING = re.compile(r"(?im)^[#*\s]*rubric\b.*$")
_TABLE = re.compile(r"(?is)<table\b.*?</table>")
_ROW = re.compile(r"(?is)<tr\b.*?</tr>")
_CELL = re.compile(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>")
_SPACED_ROW = re.compile(
    r"(?m)^(?P<desc>[^\t|]{3,}?)\s{2,}(?P<points>\d+(?:\.\d+)?)\s*(?:pts?|points?)?\s*$",
    re.IGNORECASE,
)


def html_tables_to_text(html: str) -> str:
    """Turn <table> markup into pipe rows before the HTML is flattened.

    ``html_to_text`` reduces every cell to its own line, which is right for prose
    and useless for a rubric table: the criterion and its points would no longer
    be on the same line.
    """

    def convert(match: re.Match[str]) -> str:
        rows = []
        for row in _ROW.finditer(match.group(0)):
            cells = [
                re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell)).strip()
                for cell in _CELL.findall(row.group(0))
            ]
            if cells:
                rows.append("| " + " | ".join(cells) + " |")
        return "\n" + "\n".join(rows) + "\n"

    return _TABLE.sub(convert, html or "")
_ANY_HEADING = re.compile(r"(?m)^\s*(?:#{1,6}\s|\*\*[A-Z])")


@dataclass(frozen=True)
class Rating:
    """One level of a criterion ("20 pts: precise and arguable")."""

    points: float | None
    description: str


@dataclass(frozen=True)
class Criterion:
    """One thing the assignment is marked on."""

    description: str
    points: float | None = None
    ratings: tuple[Rating, ...] = ()

    def plain(self) -> str:
        """Description with markup and trailing point values stripped."""
        text = re.sub(r"<[^>]+>", " ", self.description)
        return re.sub(r"\s+", " ", text).strip()


@dataclass
class Rubric:
    """A parsed rubric and where it came from."""

    criteria: tuple[Criterion, ...] = ()
    source: str = "none"
    title: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.criteria

    @property
    def total_points(self) -> float:
        return sum(criterion.points or 0 for criterion in self.criteria)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source": self.source,
            "criteria": [
                {
                    "description": criterion.description,
                    "points": criterion.points,
                    "ratings": [
                        {"points": rating.points, "description": rating.description}
                        for rating in criterion.ratings
                    ],
                }
                for criterion in self.criteria
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Rubric:
        criteria = []
        for item in payload.get("criteria") or []:
            criteria.append(
                Criterion(
                    description=str(item.get("description", "")),
                    points=item.get("points"),
                    ratings=tuple(
                        Rating(
                            points=rating.get("points"),
                            description=str(rating.get("description", "")),
                        )
                        for rating in item.get("ratings") or []
                    ),
                )
            )
        return cls(
            criteria=tuple(criteria),
            source=str(payload.get("source", "canvas")),
            title=str(payload.get("title", "")),
        )

    def render_markdown(self, title: str = "", fence: bool = False) -> str:
        """Render for disk (fence=False) or for a model-facing tool result."""
        from .untrusted_content import fence_untrusted_inline

        def show(text: str) -> str:
            return fence_untrusted_inline(text, "rubric criterion") if fence else text

        heading = title or self.title or "Rubric"
        lines = [
            f"# Rubric — {heading}",
            f"Source: {self.source} ({len(self.criteria)} criteria, "
            f"{self.total_points:g} points)",
            "",
        ]
        for criterion in self.criteria:
            points = f" — {criterion.points:g} pts" if criterion.points is not None else ""
            lines.append(f"## {show(criterion.plain())}{points}")
            for rating in criterion.ratings:
                level = f"{rating.points:g} pts: " if rating.points is not None else ""
                if rating.description:
                    lines.append(f"- {level}{show(rating.description)}")
            if criterion.ratings:
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"


@dataclass
class CriterionVerdict:
    """How one criterion fared against a piece of writing."""

    criterion: Criterion
    ratio: float
    verdict: str
    missing: list[str] = field(default_factory=list)




def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def from_canvas(assignment: dict[str, Any]) -> Rubric:
    """Parse Canvas's structured rubric object from an assignment payload."""
    raw = assignment.get("rubric") or {}
    if isinstance(raw, dict):
        items = raw.get("criteria") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    criteria: list[Criterion] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        description = str(
            item.get("description") or item.get("long_description") or ""
        ).strip()
        if not description:
            continue
        ratings = tuple(
            Rating(
                points=_number(rating.get("points")),
                description=str(rating.get("description") or "").strip(),
            )
            for rating in item.get("ratings") or []
            if isinstance(rating, dict)
        )
        criteria.append(
            Criterion(
                description=description,
                points=_number(item.get("points")),
                ratings=ratings,
            )
        )
    return Rubric(
        criteria=tuple(criteria),
        source="canvas" if criteria else "none",
        title=str(assignment.get("name") or ""),
    )


def _table_rows(block: str) -> list[list[str]]:
    """Markdown/plain table rows, skipping separator lines."""
    rows = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", cell or "-") for cell in cells):
            continue
        rows.append(cells)
    return rows


def _criterion_from_line(line: str) -> Criterion | None:
    """'- Thesis (20 pts): be specific' -> Criterion('Thesis', 20)."""
    text = line.strip().lstrip("-*•·").strip()
    if not text or len(text) < 3:
        return None
    match = _POINTS_AFTER.search(text)
    if not match:
        return None
    points = _number(match.group(1))
    description = text[: match.start()] + " " + text[match.end() :]
    description = re.sub(r"[()\[\]]", " ", description)
    description = re.sub(r"\s*:\s*", ": ", description)
    description = re.sub(r"\s{2,}", " ", description).strip(" :-—–\t")
    description = re.sub(r"^:\s*", "", description).strip()
    if not description:
        return None
    return Criterion(description=description, points=points)


def from_prompt(text: str) -> Rubric:
    """Parse a rubric that was pasted into the assignment description.

    Two shapes are recognised, because they are what instructors actually post:
    a table with a points column, and a bulleted/numbered list where each line
    carries "(N pts)" or "N points".
    """
    if not text or not text.strip():
        return Rubric()

    block = text
    heading = _RUBRIC_HEADING.search(text)
    if heading:
        rest = text[heading.end() :]
        following = _ANY_HEADING.search(rest)
        block = rest[: following.start()] if following else rest

    criteria: list[Criterion] = []

    rows = _table_rows(block)
    if len(rows) >= 2:
        header = [cell.lower() for cell in rows[0]]
        has_points_header = any("point" in cell or "pts" in cell or "weight" in cell for cell in header)
        for cells in rows[1:] if has_points_header else rows:
            if len(cells) < 2:
                continue
            description = cells[0]
            points = None
            for cell in cells[1:]:
                found = _POINTS.search(cell)
                if found:
                    points = _number(found.group(1))
                    break
            if not description or points is None:
                continue
            extras = [
                cell for cell in cells[1:] if not _POINTS.search(cell) and cell.strip()
            ]
            if extras:
                description = f"{description} ({extras[0]})"
            criteria.append(Criterion(description=description, points=points))

    if not criteria:
        for line in block.splitlines():
            criterion = _criterion_from_line(line)
            if criterion is not None:
                criteria.append(criterion)

    if not criteria:
        for match in _SPACED_ROW.finditer(block):
            criteria.append(
                Criterion(
                    description=match.group("desc").strip(),
                    points=_number(match.group("points")),
                )
            )

    if len(criteria) < 2:
        return Rubric()
    return Rubric(criteria=tuple(criteria), source="prompt")


def from_assignment(assignment: dict[str, Any]) -> Rubric:
    """Best rubric for an assignment: Canvas data first, then the prompt text."""
    canvas = from_canvas(assignment)
    description = assignment.get("description") or ""
    prompt = from_prompt(extract.html_to_text(html_tables_to_text(description)))

    if canvas.is_empty:
        return prompt
    if prompt.is_empty:
        return canvas

    def norm(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"\(.*?\)", "", text)).strip().lower()

    known = [norm(criterion.plain()) for criterion in canvas.criteria]
    known = [name for name in known if len(name) >= 3]

    def is_new(criterion: Criterion) -> bool:
        name = norm(criterion.plain())
        if not name:
            return False
        return not any(name in existing or existing in name for existing in known)

    extra = [criterion for criterion in prompt.criteria if is_new(criterion)]
    if not extra:
        return canvas
    return Rubric(
        criteria=canvas.criteria + tuple(extra),
        source="canvas+prompt",
        title=canvas.title,
    )




def save(
    rubric: Rubric,
    course_identifier: str | int,
    assignment_id: str | int,
    title: str | None = None,
) -> Path:
    """Store the rubric beside the assignment so later sessions can use it."""
    workspace = student_store.assignment_dir(course_identifier, assignment_id, title)
    markdown = workspace / "rubric.md"
    student_store.write_text(markdown, rubric.render_markdown())
    student_store.write_text(
        workspace / "rubric.json",
        json.dumps(rubric.to_dict(), indent=2, ensure_ascii=False) + "\n",
    )
    return markdown


def load(
    course_identifier: str | int, assignment_id: str | int
) -> Rubric | None:
    """Read a previously saved rubric, or None when none was stored."""
    workspace = student_store.find_assignment_dir(course_identifier, assignment_id)
    if workspace is None:
        return None
    for name in ("rubric.json", "rubric.md"):
        path = workspace / name
        if not path.is_file():
            continue
        try:
            if name.endswith(".json"):
                return Rubric.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return None




def keywords(text: str) -> set[str]:
    """Content words used to decide whether a criterion is mentioned."""
    words = {word.lower().strip("'-") for word in _WORD.findall(text or "")}
    return {word for word in words if word and word not in _STOPWORDS and len(word) > 3}


def check(rubric: Rubric, text: str) -> list[CriterionVerdict]:
    """Per-criterion verdicts for a draft, guide or submission.

    Honest by construction: it says whether the writing *mentions* what a
    criterion asks about, and names the words it could not find. It cannot judge
    quality, so the wording never claims to.
    """
    present = keywords(text)
    verdicts: list[CriterionVerdict] = []
    for criterion in rubric.criteria:
        wanted = keywords(criterion.plain())
        if not wanted:
            verdicts.append(CriterionVerdict(criterion, 0.0, "uncheckable", []))
            continue
        found = wanted & present
        ratio = len(found) / len(wanted)
        missing = sorted(wanted - present)[:6]
        if ratio >= 0.5:
            verdict = "addressed"
        elif ratio >= 0.25:
            verdict = "partial"
        else:
            verdict = "not found"
        verdicts.append(CriterionVerdict(criterion, ratio, verdict, missing))
    return verdicts


def render_check(
    rubric: Rubric, verdicts: list[CriterionVerdict], subject: str, fence: bool = True
) -> str:
    """Render the criterion-by-criterion report."""
    from .untrusted_content import fence_untrusted_inline

    def show(text: str) -> str:
        return fence_untrusted_inline(text, "rubric criterion") if fence else text

    addressed = sum(1 for verdict in verdicts if verdict.verdict == "addressed")
    lines = [
        f"Rubric check — {subject}",
        f"Source: {rubric.source} | {len(verdicts)} criteria | "
        f"{addressed} addressed, {sum(1 for v in verdicts if v.verdict == 'partial')} partial, "
        f"{sum(1 for v in verdicts if v.verdict == 'not found')} not found",
        "",
    ]
    for verdict in verdicts:
        points = (
            f" ({verdict.criterion.points:g} pts)"
            if verdict.criterion.points is not None
            else ""
        )
        lines.append(f"- [{verdict.verdict}] {show(verdict.criterion.plain())}{points}")
        if verdict.missing:
            lines.append("    not mentioned: " + ", ".join(verdict.missing))
    lines.append(
        "\nThis is a keyword check, not a grade: it shows which criteria the text "
        "never engages. Rewrite or add evidence where it says 'not found'."
    )
    return "\n".join(lines)
