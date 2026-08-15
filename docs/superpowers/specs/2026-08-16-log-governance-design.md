# Log Governance Design

Date: 2026-08-16  
Project: daily_stock_analysis  
Status: Approved for spec; implementation plan not started

## Problem

The API process (`stock-server`) is long-lived. `setup_logging()` stamps filenames with the process start date, so a process started on 2026-08-13 still writes `stock_analysis_20260813.log` days later. Date-stamped files are never deleted. Combined with DEBUG dumps of full LLM prompts and OpenAI HTTP `Request options`, `logs/` has grown to about 260MB.

User-facing impact: disk growth and noisy, sensitive debug logs. Stock query quality is out of scope.

## Goals

- Keep log disk usage bounded.
- Keep enough history to debug a stock query (model, timing, short preview).
- Do not persist full LLM system/user prompts or full model responses.
- Deleting old logs is intentional and irreversible.

## Non-goals

- Data-source order, chip-distribution fallback, LLM empty-response retries
- Public port / RSSHub exposure
- Docker json-file log driver (already `max-size: 10m`, `max-file: 3`)
- `reports/` and `data/`

## Retention policy

| Kind | Default | Env override |
|------|---------|----------------|
| Info / regular logs | 7 days | `LOG_RETENTION_DAYS` |
| Debug logs | 3 days | `LOG_DEBUG_RETENTION_DAYS` |

Success criteria:

1. After restart, `logs/` drops from about 260MB to a few days of files.
2. A stock query writes model name, prompt length, elapsed time, and a short preview at INFO.
3. Debug logs do not contain full prompts, full responses, or OpenAI `json_data` payloads.

## Architecture

Three small units, no new logging framework.

### 1. Date-based rolling (`src/logging_config.py`)

Replace the current pattern: `RotatingFileHandler` on a filename computed once at startup (`stock_analysis_YYYYMMDD.log`).

Use `TimedRotatingFileHandler` (midnight, local time; compose already sets `TZ=Asia/Shanghai`):

- Active files: `logs/stock_analysis.log`, `logs/stock_analysis_debug.log`
- After midnight, rotate to dated backups (handler suffix `%Y%m%d`, producing names like `stock_analysis.log.20260816`)
- Do not attach a second size-based `RotatingFileHandler` on the same files. A single busy day may grow large; retention still caps how many days remain.
- Set `backupCount` to the retention window (7 for info, 3 for debug) so TimedRotating does not keep extra dated backups. `purge_old_logs` still removes legacy dated names.

Long-running `stock-server` will then switch files at midnight instead of freezing the start date.

### 2. Expired-file purge

Add `purge_old_logs(log_dir, ...)` and call it at the end of `setup_logging()`.

Delete only matches under `logs/`:

- `stock_analysis_YYYYMMDD.log` and `stock_analysis_debug_YYYYMMDD.log` (legacy names)
- `stock_analysis.log.YYYYMMDD`, `stock_analysis_debug.log.YYYYMMDD` (TimedRotating names)
- size-rotation leftovers: `stock_analysis*.log.1`, `.2`, ...

Do not delete unmatched files. Do not walk `reports/` or `data/`.

Parse dates from filenames; if a date cannot be parsed, skip and log WARNING.

### 3. Noise reduction

In `setup_logging()` set these loggers to WARNING: `openai`, `httpx`, `httpcore` (in addition to existing `urllib3`, `sqlalchemy`, `google`).

In `src/analyzer.py` remove:

- `logger.debug` of the full prompt
- `logger.debug` of the full LLM response

Keep INFO: model name, prompt length, news flag, elapsed time, response length, and existing previews (prompt 500 chars, response 300 chars).

## Data flow

1. Process start → `setup_logging()`
2. Attach console + timed file handlers
3. `purge_old_logs()` removes files older than retention
4. Runtime writes only the active files
5. Midnight: TimedRotating renames the active file
6. Next process start (or analyzer restart) purges again

No extra cron. Analyzer and server both call `setup_logging`, so either restart cleans the shared `logs/` volume.

## Error handling

- Purge I/O or permission errors: log WARNING, skip that file, do not fail process startup
- Midnight rotation failure: keep writing the current file; next start retries
- Filename mismatch: leave the file
- `purge_old_logs` is callable on its own for a manual run; the default hook is `setup_logging()` only

## Testing

Unit tests (no live server), new file `tests/test_log_governance.py`:

- Given a fake `logs/` tree with dated info/debug files and a file under `reports/`, assert: info older than 7 days deleted, debug older than 3 days deleted, yesterday/today kept, `reports/` untouched, unmatched names kept

Syntax: `python -m py_compile` on touched files.

Manual after deploy: `du -sh logs`; grep debug log for `完整 Prompt` / `json_data` (should be absent).

## Rollout

- Code change requires rebuild and restart of `stock-server` and `stock-analyzer` so `setup_logging` runs and purge executes
- Optional env: `LOG_RETENTION_DAYS=7`, `LOG_DEBUG_RETENTION_DAYS=3`
- Rollback: revert image. Deleted log files cannot be restored (accepted)

## Files expected to change

- `src/logging_config.py` — timed rotation, quiet loggers, purge
- `src/analyzer.py` — drop full prompt/response DEBUG
- `src/config.py` — load `LOG_RETENTION_DAYS` and `LOG_DEBUG_RETENTION_DAYS` the same way other int env knobs are loaded, and pass them into `setup_logging`
- `tests/test_log_governance.py` — new
- `.env.example` — document the two env vars

## Resolved decisions

- Retention: 7 / 3 days
- Approach: extend current logging module, not loguru
- Cleanup: on `setup_logging`, not host cron
- Scope: logging only
