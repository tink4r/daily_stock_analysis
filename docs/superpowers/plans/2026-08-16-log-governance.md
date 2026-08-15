# Log Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound `logs/` disk use with midnight rotation plus retention purge, and stop writing full LLM prompts/responses (and OpenAI HTTP payloads) to debug logs.

**Architecture:** Keep stdlib logging. Add `purge_old_logs()` that classifies files by filename (legacy dated names, TimedRotating dated names, numeric size leftovers). Switch `setup_logging()` from startup-stamped `RotatingFileHandler` to `TimedRotatingFileHandler` on stable names (`{prefix}.log` / `{prefix}_debug.log`). Wire retention days through `Config`. Strip two DEBUG dumps in `src/analyzer.py` and quiet `openai` / `httpcore`.

**Tech Stack:** Python 3.10+, stdlib `logging` / `unittest`, existing `src/config.py` env loading.

**Spec:** `docs/superpowers/specs/2026-08-16-log-governance-design.md`

---

## File map

| File | Responsibility |
|------|----------------|
| `src/logging_config.py` | Quiet logger list, `purge_old_logs()`, midnight handlers, call purge at end of `setup_logging()` |
| `src/config.py` | `log_retention_days`, `log_debug_retention_days` from env |
| `main.py` | Pass `log_dir` + retention into `setup_logging()` |
| `server.py` | Pass `log_dir` + retention (today it omits `log_dir`) |
| `src/analyzer.py` | Remove full prompt/response DEBUG lines; keep INFO previews |
| `tests/test_log_governance.py` | Unit tests for purge classification and retention |
| `.env.example` | Document the two env vars |
| `docs/CHANGELOG.md` | Patch notes |
| `docs/full-guide.md` | Document `LOG_RETENTION_DAYS` / `LOG_DEBUG_RETENTION_DAYS` next to `LOG_DIR` |

Do **not** split `logging_config.py` (it is small). Do **not** change data sources, chip distribution, RSSHub, or Docker json-file logging.

**Filename rules (lock this in; do not use a naive `*.log.\\d+` glob):**

- Active (never delete): `{prefix}.log`, `{prefix}_debug.log`
- Legacy info: `{prefix}_YYYYMMDD.log`
- Legacy debug: `{prefix}_debug_YYYYMMDD.log`
- TimedRotating info: `{prefix}.log.YYYYMMDD` (exactly 8 digits)
- TimedRotating debug: `{prefix}_debug.log.YYYYMMDD` (exactly 8 digits)
- Size leftovers (always delete): `{prefix}.log.N` or `{prefix}_debug.log.N` or `{prefix}_YYYYMMDD.log.N` where `N` is 1–2 digits (the production leftover `stock_analysis_20260707.log.1` is this class)

An 8-digit suffix is a **date**, not a size backup. Deleting `stock_analysis.log.20260816` as if it were `.1` is a bug.

---

### Task 1: Purge helper (TDD)

**Files:**
- Create: `tests/test_log_governance.py`
- Modify: `src/logging_config.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_log_governance.py`:

```python
# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.logging_config import purge_old_logs


class TestPurgeOldLogs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.logs = self.root / "logs"
        self.reports = self.root / "reports"
        self.logs.mkdir()
        self.reports.mkdir()
        self.now = datetime(2026, 8, 16, 12, 0, 0)
        self.prefix = "stock_analysis"

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    def test_purges_legacy_and_keeps_recent(self):
        old_info = (self.now - timedelta(days=8)).strftime("%Y%m%d")
        old_debug = (self.now - timedelta(days=4)).strftime("%Y%m%d")
        yesterday = (self.now - timedelta(days=1)).strftime("%Y%m%d")
        today = self.now.strftime("%Y%m%d")

        keep_info = self.logs / f"{self.prefix}_{yesterday}.log"
        keep_debug = self.logs / f"{self.prefix}_debug_{today}.log"
        drop_info = self.logs / f"{self.prefix}_{old_info}.log"
        drop_debug = self.logs / f"{self.prefix}_debug_{old_debug}.log"
        report_file = self.reports / "daily.md"
        unmatched = self.logs / "unrelated.log"
        active_info = self.logs / f"{self.prefix}.log"
        active_debug = self.logs / f"{self.prefix}_debug.log"
        rotated_info_keep = self.logs / f"{self.prefix}.log.{yesterday}"
        rotated_info_drop = self.logs / f"{self.prefix}.log.{old_info}"
        size_leftover = self.logs / f"{self.prefix}_{old_info}.log.1"

        for path in (
            keep_info,
            keep_debug,
            drop_info,
            drop_debug,
            report_file,
            unmatched,
            active_info,
            active_debug,
            rotated_info_keep,
            rotated_info_drop,
            size_leftover,
        ):
            self._touch(path)

        deleted = purge_old_logs(
            str(self.logs),
            log_prefix=self.prefix,
            retention_days=7,
            debug_retention_days=3,
            now=self.now,
        )

        self.assertGreaterEqual(deleted, 3)
        self.assertTrue(keep_info.exists())
        self.assertTrue(keep_debug.exists())
        self.assertTrue(report_file.exists())
        self.assertTrue(unmatched.exists())
        self.assertTrue(active_info.exists())
        self.assertTrue(active_debug.exists())
        self.assertTrue(rotated_info_keep.exists())
        self.assertFalse(drop_info.exists())
        self.assertFalse(drop_debug.exists())
        self.assertFalse(rotated_info_drop.exists())
        self.assertFalse(size_leftover.exists())

    def test_missing_dir_does_not_raise(self):
        deleted = purge_old_logs(
            str(self.root / "nope"),
            log_prefix=self.prefix,
            now=self.now,
        )
        self.assertEqual(deleted, 0)

    def test_unreadable_date_is_skipped(self):
        weird = self.logs / f"{self.prefix}_notadate.log"
        self._touch(weird)
        deleted = purge_old_logs(str(self.logs), log_prefix=self.prefix, now=self.now)
        self.assertEqual(deleted, 0)
        self.assertTrue(weird.exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest tests.test_log_governance -v
```

Expected: FAIL with `ImportError` (`cannot import name 'purge_old_logs'`) or `AttributeError`.

- [ ] **Step 3: Implement `purge_old_logs`**

In `src/logging_config.py`:

1. Add imports: `re`, `os`. Keep `datetime`, `Path`.
2. Add the helper and function below (English comments only). Place them **above** `setup_logging`.

```python
_DATE8 = re.compile(r"^(\d{8})$")
_SIZE_SUFFIX = re.compile(r"^(\d{1,2})$")


def _parse_yyyy_mm_dd(value: str):
    """Parse YYYYMMDD into a date, or None if invalid."""
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def purge_old_logs(
    log_dir: str,
    log_prefix: str = "stock_analysis",
    retention_days: int = 7,
    debug_retention_days: int = 3,
    now: Optional[datetime] = None,
) -> int:
    """
    Delete expired log files for one prefix. Never raises.

    Returns the number of files successfully deleted.
    """
    base = Path(log_dir)
    if not base.is_dir():
        return 0

    current = (now or datetime.now()).date()
    deleted = 0
    prefix = log_prefix

    for path in list(base.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        try:
            should_delete = _should_delete_log_file(
                name=name,
                prefix=prefix,
                current=current,
                retention_days=retention_days,
                debug_retention_days=debug_retention_days,
            )
            if should_delete is None:
                logging.warning("Skipping log file with unparseable date: %s", name)
                continue
            if should_delete:
                path.unlink()
                deleted += 1
        except OSError as exc:
            logging.warning("Failed to delete log file %s: %s", path, exc)
    return deleted


def _should_delete_log_file(
    name: str,
    prefix: str,
    current,
    retention_days: int,
    debug_retention_days: int,
):
    """
    Return True to delete, False to keep, None if the name looks dated but the date is invalid.
    """
    # Size leftovers: *.log.N or *_YYYYMMDD.log.N with 1-2 digit N
    size_match = re.fullmatch(
        rf"{re.escape(prefix)}(?:_debug)?(?:_\d{{8}})?\.log\.(\d{{1,2}})",
        name,
    )
    if size_match and _SIZE_SUFFIX.fullmatch(size_match.group(1)):
        # 8-digit TimedRotating suffixes are handled below, not here
        if len(size_match.group(1)) <= 2:
            return True

    patterns = (
        (rf"{re.escape(prefix)}_(\d{{8}})\.log$", False),
        (rf"{re.escape(prefix)}_debug_(\d{{8}})\.log$", True),
        (rf"{re.escape(prefix)}\.log\.(\d{{8}})$", False),
        (rf"{re.escape(prefix)}_debug\.log\.(\d{{8}})$", True),
    )
    for pattern, is_debug in patterns:
        match = re.fullmatch(pattern, name)
        if not match:
            continue
        parsed = _parse_yyyy_mm_dd(match.group(1))
        if parsed is None:
            return None
        limit = debug_retention_days if is_debug else retention_days
        return (current - parsed).days > limit

    return False
```

Important: the size leftover regex must **not** treat `stock_analysis.log.20260816` as size leftover. `(\d{1,2})` only matches 1–2 digits, so 8-digit dates fall through to the dated patterns.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m unittest tests.test_log_governance -v
```

Expected: `OK` (3 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_log_governance.py src/logging_config.py
git commit -m "Add log retention purge for dated and leftover log files. #patch"
```

---

### Task 2: Midnight rotation and quiet loggers

**Files:**
- Modify: `src/logging_config.py`
- Modify: `tests/test_log_governance.py`

- [ ] **Step 1: Write a failing test for handler setup**

Append to `tests/test_log_governance.py`:

```python
import logging
from logging.handlers import TimedRotatingFileHandler

from src.logging_config import setup_logging


class TestSetupLogging(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self._tmp.name) / "logs"
        self.logs.mkdir()

    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            handler.close()
            root.removeHandler(handler)
        self._tmp.cleanup()

    def test_uses_timed_rotating_stable_names(self):
        setup_logging(
            log_prefix="stock_analysis",
            log_dir=str(self.logs),
            retention_days=7,
            debug_retention_days=3,
        )
        root = logging.getLogger()
        timed = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
        self.assertEqual(len(timed), 2)
        names = {Path(h.baseFilename).name for h in timed}
        self.assertEqual(names, {"stock_analysis.log", "stock_analysis_debug.log"})
        suffixes = {h.suffix for h in timed}
        self.assertEqual(suffixes, {"%Y%m%d"})
```

- [ ] **Step 2: Run the new test to verify it fails**

```bash
python -m unittest tests.test_log_governance.TestSetupLogging -v
```

Expected: FAIL (`TypeError: unexpected keyword argument 'retention_days'` and/or assertion on `TimedRotatingFileHandler` count 0).

- [ ] **Step 3: Change `setup_logging`**

In `src/logging_config.py`:

1. Replace `from logging.handlers import RotatingFileHandler` with `TimedRotatingFileHandler`.
2. Extend `DEFAULT_QUIET_LOGGERS` to:

```python
DEFAULT_QUIET_LOGGERS = [
    "urllib3",
    "sqlalchemy",
    "google",
    "httpx",
    "httpcore",
    "openai",
]
```

(`httpx` is already present; add `httpcore` and `openai`.)

3. Change signature and body of `setup_logging` to:

```python
def setup_logging(
    log_prefix: str = "app",
    log_dir: str = "./logs",
    console_level: Optional[int] = None,
    debug: bool = False,
    extra_quiet_loggers: Optional[List[str]] = None,
    retention_days: int = 7,
    debug_retention_days: int = 3,
) -> None:
    if console_level is not None:
        level = console_level
    else:
        level = logging.DEBUG if debug else logging.INFO

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    log_file = log_path / f"{log_prefix}.log"
    debug_log_file = log_path / f"{log_prefix}_debug.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    if root_logger.handlers:
        root_logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(console_handler)

    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
        utc=False,
    )
    file_handler.suffix = "%Y%m%d"
    file_handler.extMatch = re.compile(r"^\d{8}(\.\w+)?$", re.ASCII)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(file_handler)

    debug_handler = TimedRotatingFileHandler(
        debug_log_file,
        when="midnight",
        interval=1,
        backupCount=debug_retention_days,
        encoding="utf-8",
        utc=False,
    )
    debug_handler.suffix = "%Y%m%d"
    debug_handler.extMatch = re.compile(r"^\d{8}(\.\w+)?$", re.ASCII)
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(debug_handler)

    quiet_loggers = DEFAULT_QUIET_LOGGERS.copy()
    if extra_quiet_loggers:
        quiet_loggers.extend(extra_quiet_loggers)
    for logger_name in quiet_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    purge_old_logs(
        str(log_path),
        log_prefix=log_prefix,
        retention_days=retention_days,
        debug_retention_days=debug_retention_days,
    )

    logging.info("Log system initialized, directory: %s", log_path.absolute())
    logging.info("Info log: %s", log_file)
    logging.info("Debug log: %s", debug_log_file)
```

Update the docstring to describe midnight rotation + retention purge (English).

Setting `suffix` **and** `extMatch` together is required. If `extMatch` stays the default `%Y-%m-%d` regex, `backupCount` will not delete `%Y%m%d` backups.

- [ ] **Step 4: Run tests**

```bash
python -m unittest tests.test_log_governance -v
python -m py_compile src/logging_config.py
```

Expected: all tests `OK`; `py_compile` silent.

- [ ] **Step 5: Commit**

```bash
git add src/logging_config.py tests/test_log_governance.py
git commit -m "Rotate logs at midnight and quiet OpenAI HTTP debug loggers. #patch"
```

---

### Task 3: Config and call sites

**Files:**
- Modify: `src/config.py` (dataclass ~181–183 and `from_env` ~489–490)
- Modify: `main.py:405`
- Modify: `server.py:33-37`

- [ ] **Step 1: Add fields on `Config`**

In `src/config.py` under `# === 日志配置 ===`:

```python
    log_dir: str = "./logs"
    log_level: str = "INFO"
    log_retention_days: int = 7
    log_debug_retention_days: int = 3
```

In `from_env` next to `log_level=...`:

```python
            log_dir=os.getenv("LOG_DIR", "./logs"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_retention_days=int(os.getenv("LOG_RETENTION_DAYS", "7")),
            log_debug_retention_days=int(os.getenv("LOG_DEBUG_RETENTION_DAYS", "3")),
```

- [ ] **Step 2: Pass retention from `main.py`**

Replace `main.py:405`:

```python
    setup_logging(
        log_prefix="stock_analysis",
        debug=args.debug,
        log_dir=config.log_dir,
        retention_days=config.log_retention_days,
        debug_retention_days=config.log_debug_retention_days,
    )
```

- [ ] **Step 3: Pass `log_dir` and retention from `server.py`**

Replace the `setup_logging(...)` block:

```python
setup_logging(
    log_prefix="api_server",
    log_dir=config.log_dir,
    console_level=level,
    extra_quiet_loggers=["uvicorn", "fastapi"],
    retention_days=config.log_retention_days,
    debug_retention_days=config.log_debug_retention_days,
)
```

- [ ] **Step 4: Syntax check**

```bash
python -m py_compile src/config.py main.py server.py
python -m unittest tests.test_log_governance -v
```

Expected: compile silent; tests still `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/config.py main.py server.py
git commit -m "Wire log retention days from env into logging setup. #patch"
```

---

### Task 4: Stop dumping full LLM payloads

**Files:**
- Modify: `src/analyzer.py:945-972`

- [ ] **Step 1: Confirm the two DEBUG lines exist**

```bash
python -c "from pathlib import Path; t=Path('src/analyzer.py').read_text(encoding='utf-8'); assert '完整 Prompt' in t; assert '完整响应' in t"
```

Expected: no output (asserts pass).

- [ ] **Step 2: Remove DEBUG dumps, keep INFO previews**

Replace the prompt-logging block (lines 945–948) with:

```python
            prompt_preview = prompt[:500] + "..." if len(prompt) > 500 else prompt
            logger.info(f"[LLM Prompt 预览]\n{prompt_preview}")
```

Replace the response-logging block (lines 969–972) with:

```python
            response_preview = response_text[:300] + "..." if len(response_text) > 300 else response_text
            logger.info(f"[LLM返回 预览]\n{response_preview}")
```

Do not change the INFO lines for model name, prompt length, news flag, elapsed time, or response length.

- [ ] **Step 3: Verify the strings are gone and the file compiles**

```bash
python -c "from pathlib import Path; t=Path('src/analyzer.py').read_text(encoding='utf-8'); assert '完整 Prompt' not in t; assert '=== End Prompt ===' not in t; assert '=== End Response ===' not in t"
python -m py_compile src/analyzer.py
```

Expected: both commands silent.

- [ ] **Step 4: Commit**

```bash
git add src/analyzer.py
git commit -m "Stop writing full LLM prompts and responses to debug logs. #patch"
```

---

### Task 5: Docs

**Files:**
- Modify: `.env.example` (append a logging section at the end)
- Modify: `docs/full-guide.md` table at the `LOG_DIR` row (~216)
- Modify: `docs/CHANGELOG.md` (new `[3.0.6]` section at the top after the intro)

- [ ] **Step 1: `.env.example`**

Append:

```
# ===========================================
# Logging
# ===========================================
# LOG_DIR=./logs
# Keep info logs this many days (midnight-rotated files + legacy dated names)
# LOG_RETENTION_DAYS=7
# Keep debug logs this many days
# LOG_DEBUG_RETENTION_DAYS=3
```

- [ ] **Step 2: `docs/full-guide.md`**

In the 其他配置 table, after `LOG_DIR`, add:

```
| `LOG_RETENTION_DAYS` | 常规日志保留天数 | `7` |
| `LOG_DEBUG_RETENTION_DAYS` | 调试日志保留天数 | `3` |
```

- [ ] **Step 3: `docs/CHANGELOG.md`**

Insert after the Keep a Changelog paragraph:

```
## [3.0.6] - 2026-08-16

### 优化
- 日志改为按自然日午夜滚动，避免长驻进程一直写入启动当天的文件
- 启动时清理过期日志（常规默认 7 天，debug 默认 3 天，可用 `LOG_RETENTION_DAYS` / `LOG_DEBUG_RETENTION_DAYS` 配置）
- 不再将完整 LLM Prompt/响应写入 debug 日志；OpenAI/httpx/httpcore 降为 WARNING
```

- [ ] **Step 4: Commit**

```bash
git add .env.example docs/full-guide.md docs/CHANGELOG.md
git commit -m "Document log retention settings and changelog. #patch"
```

---

## Deploy notes (not a code task)

After merge, on the server: rebuild/restart `stock-server` and `stock-analyzer` so `setup_logging()` runs. Then:

```bash
du -sh /www/wwwroot/daily_stock_analysis/logs
ls /www/wwwroot/daily_stock_analysis/logs | head
grep -R "完整 Prompt\|json_data" /www/wwwroot/daily_stock_analysis/logs/stock_analysis_debug.log || true
```

Expect a much smaller `logs/` directory and no full-prompt / `json_data` lines in the new debug file. Deleted files cannot be restored.

Rollback: revert the image/commit. Already-deleted logs stay gone.

---

## Spec coverage check

| Spec item | Task |
|-----------|------|
| TimedRotating midnight, stable names, `%Y%m%d` suffix | Task 2 |
| `backupCount` = retention window | Task 2 |
| No second size RotatingFileHandler | Task 2 |
| `purge_old_logs` on `setup_logging` | Task 2 (call) + Task 1 (impl) |
| Legacy dated names + TimedRotating names + `.log.1` leftovers | Task 1 |
| Skip unmatched; do not touch `reports/` | Task 1 |
| Invalid date → WARNING skip | Task 1 (`None` branch) |
| Purge errors do not crash startup | Task 1 (`OSError` → warning) |
| Quiet `openai`, `httpx`, `httpcore` | Task 2 |
| Remove full prompt/response DEBUG | Task 4 |
| Keep INFO previews 500/300 | Task 4 |
| Config env knobs | Task 3 |
| `.env.example` | Task 5 |
| Unit tests | Task 1–2 |
| `py_compile` | Tasks 2–4 |
| Rebuild/restart for purge to run | Deploy notes |

No extra cron. No loguru. No data-source work.
