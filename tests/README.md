# Tests

One command, from a clean checkout:

```bash
uv run pytest
```

`uv sync` installs the runtime dependencies — OCR, PDF and Office reading are
all built in, not extras — plus the `dev` group (pytest, pytest-asyncio, ruff).

Every test runs on a normal install, including the one that OCRs a rendered
image; nothing is skipped:

```bash
uv sync && uv run pytest
```

Every test writes only to pytest's `tmp_path` (via `CANVAS_STUDENT_DIR`) and
clears the course cache, so the suite is idempotent: running it twice in a row
gives the same result with no leftover state. No test reaches the network; the
Canvas API is replaced by `tests/conftest.py::FakeCanvas`.
