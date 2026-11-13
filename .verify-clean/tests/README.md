# Tests

One command, from a clean checkout:

```bash
uv run pytest
```

`uv sync` installs the `dev` group (pytest, pytest-asyncio, ruff).

The whole suite passes without the optional OCR extra. The one test that needs a
real OCR engine skips itself with a message naming the missing pieces; run it
with:

```bash
uv sync --extra ocr && uv run pytest
```

Every test writes only to pytest's `tmp_path` (via `CANVAS_STUDENT_DIR`) and
clears the course cache, so the suite is idempotent: running it twice in a row
gives the same result with no leftover state. No test reaches the network; the
Canvas API is replaced by `tests/conftest.py::FakeCanvas`.
