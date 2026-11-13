"""Formatting and analysis for study material.

The model writes the content; these helpers enforce the shape that makes a
guide actually useful for an exam — a required skeleton, a hard length budget,
an honest report of what is missing, and a deterministic writing-style check.
"""

from __future__ import annotations

import re
import statistics

# Sections a guide needs to be revisable rather than merely long. Missing ones
# are reported, never fatal: a short reading guide legitimately skips some.
REQUIRED_SECTIONS = (
    "what's on it",
    "key",
    "definitions",
    "self-test",
    "source",
)

# Phrases that make prose read as generated rather than written. Kept to
# unambiguous offenders so the check is useful instead of noisy.
STOCK_PHRASES = (
    "it is important to note",
    "it is worth noting",
    "in today's fast-paced",
    "in the ever-evolving",
    "navigating the complexities",
    "plays a crucial role",
    "plays a pivotal role",
    "a testament to",
    "delve into",
    "delving into",
    "underscores the importance",
    "multifaceted",
    "tapestry of",
    "landscape of",
    "furthermore",
    "moreover",
    "in conclusion",
    "in summary,",
    "overall,",
    "that being said",
    "at the end of the day",
)

_HEDGES = ("it could be argued", "one might say", "it can be seen that", "arguably")


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def estimate_pages(text: str, words_per_page: int = 450) -> float:
    return round(word_count(text) / words_per_page, 1)


def front_matter(meta: dict[str, object]) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if value in (None, "", []):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def missing_sections(markdown: str) -> list[str]:
    """Which expected sections a guide does not contain."""
    lowered = markdown.lower()
    headings = " \n".join(
        line.lstrip("# ").strip() for line in markdown.splitlines() if line.strip().startswith("#")
    ).lower()
    missing = []
    for section in REQUIRED_SECTIONS:
        if section not in headings and section not in lowered:
            missing.append(section)
    return missing


def has_source_map(markdown: str) -> bool:
    """A guide is trustworthy only if claims point back at a source."""
    return bool(re.search(r"(\[p\.\d+\]|slide \d+|\[deck|source map|\[\d+\])", markdown, re.IGNORECASE))


def budget_report(markdown: str, max_pages: float) -> tuple[float, bool]:
    pages = estimate_pages(markdown)
    return pages, pages <= max_pages


def sentence_split(text: str) -> list[str]:
    return [chunk.strip() for chunk in re.split(r"(?<=[.!?])\s+", text or "") if chunk.strip()]


def analyze_style(text: str) -> dict[str, object]:
    """Deterministic readability and 'sounds generated' findings.

    Not a detector and not a score: it names specific patterns the student can
    change, which is the part that actually improves the writing.
    """
    prose = re.sub(r"```.*?```", " ", text or "", flags=re.DOTALL)
    prose = re.sub(r"^\s*[-*#>].*$", " ", prose, flags=re.MULTILINE)
    sentences = sentence_split(prose)
    lengths = [len(sentence.split()) for sentence in sentences]
    words = word_count(prose)
    paragraphs = [block for block in re.split(r"\n\s*\n", prose) if block.strip()]

    lowered = prose.lower()
    stock_hits = [phrase for phrase in STOCK_PHRASES if phrase in lowered]
    hedge_hits = [phrase for phrase in _HEDGES if phrase in lowered]

    findings: list[str] = []
    if stock_hits:
        findings.append(
            "Stock connectives that read as filler: " + ", ".join(f"'{p}'" for p in stock_hits)
        )
    if hedge_hits:
        findings.append("Hedged claims with no example behind them: " + ", ".join(f"'{p}'" for p in hedge_hits))
    if len(sentences) >= 8 and lengths:
        spread = statistics.pstdev(lengths)
        if spread < 4:
            findings.append(
                f"Every sentence is about the same length (spread {spread:.1f} words). "
                "Mix a short sentence in; uniform rhythm is the loudest tell."
            )
        if statistics.mean(lengths) > 30:
            findings.append(f"Average sentence is {statistics.mean(lengths):.0f} words. Split the long ones.")
    if words:
        em_dashes = prose.count("—")
        if em_dashes > max(3, words // 200):
            findings.append(f"{em_dashes} em dashes in {words} words. Use commas or full stops instead.")
    if paragraphs and words / max(len(paragraphs), 1) > 180:
        findings.append("Paragraphs are very long. Break them where the idea changes.")
    if re.search(r"(?m)^\s*(\d+\.|[-*])\s", prose):
        findings.append("Bullet or numbered lists inside prose. Write it as sentences if this is an essay.")
    if re.search(r"\b(very|really|quite|extremely|significantly)\b", lowered):
        findings.append("Intensifiers (very/really/quite) are doing work an example should do.")
    if not findings:
        findings.append("No stock phrasing or rhythm problems found. Check the argument and the evidence.")

    return {
        "words": words,
        "sentences": len(sentences),
        "avg_sentence_words": round(statistics.mean(lengths), 1) if lengths else 0,
        "sentence_length_spread": round(statistics.pstdev(lengths), 1) if len(lengths) > 1 else 0,
        "paragraphs": len(paragraphs),
        "stock_phrases": stock_hits,
        "findings": findings,
    }


def practice_test_shape(markdown: str) -> list[str]:
    """Problems that make a self-test useless for revision."""
    problems = []
    if not re.search(r"(?im)^\s*#+\s*answer", markdown):
        problems.append("No answer key. Add a '## Answers' section so you can check yourself.")
    questions = len(re.findall(r"(?m)^\s*\d+[.)]\s", markdown))
    if questions < 5:
        problems.append(f"Only {questions} numbered questions found. Aim for at least 10.")
    if "<details>" not in markdown:
        problems.append("Answers are not collapsible. Wrap them in <details> so you can test yourself honestly.")
    return problems
