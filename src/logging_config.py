# -*- coding: utf-8 -*-
"""
===================================
日志配置模块 - 统一的日志系统初始化
===================================

职责：
1. 提供统一的日志格式和配置常量
2. 支持控制台 + 文件（常规/调试）三层日志输出
3. 自动降低第三方库日志级别
"""

import logging
import re
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import List, Optional

# ============================================================
# 日志格式常量
# ============================================================

LOG_FORMAT = '%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# 默认需要降低日志级别的第三方库
DEFAULT_QUIET_LOGGERS = [
    'urllib3',
    'sqlalchemy',
    'google',
    'httpx',
    'httpcore',
    'openai',
]

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
    size_match = re.fullmatch(
        rf"{re.escape(prefix)}(?:_debug)?(?:_\d{{8}})?\.log\.(\d{{1,2}})",
        name,
    )
    if size_match and _SIZE_SUFFIX.fullmatch(size_match.group(1)):
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


def setup_logging(
    log_prefix: str = "app",
    log_dir: str = "./logs",
    console_level: Optional[int] = None,
    debug: bool = False,
    extra_quiet_loggers: Optional[List[str]] = None,
    retention_days: int = 7,
    debug_retention_days: int = 3,
) -> None:
    """
    Initialize the unified logging system.

    Configures three outputs:
    1. Console: level from debug or console_level
    2. Info file: {log_prefix}.log, midnight local rotation
    3. Debug file: {log_prefix}_debug.log, midnight local rotation

    Args:
        log_prefix: Active log filename prefix (e.g. "api_server" -> api_server.log)
        log_dir: Log directory, default ./logs
        console_level: Optional console log level (takes precedence over debug)
        debug: If True, console outputs DEBUG
        extra_quiet_loggers: Extra third-party loggers to quiet
        retention_days: TimedRotatingFileHandler backupCount for info logs
        debug_retention_days: TimedRotatingFileHandler backupCount for debug logs
    """
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
        str(log_file),
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
        utc=False,
    )
    file_handler.suffix = "%Y%m%d"
    file_handler.extMatch = re.compile(r"^\d{8}$")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(file_handler)

    debug_handler = TimedRotatingFileHandler(
        str(debug_log_file),
        when="midnight",
        interval=1,
        backupCount=debug_retention_days,
        encoding="utf-8",
        utc=False,
    )
    debug_handler.suffix = "%Y%m%d"
    debug_handler.extMatch = re.compile(r"^\d{8}$")
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(debug_handler)

    quiet_loggers = DEFAULT_QUIET_LOGGERS.copy()
    if extra_quiet_loggers:
        quiet_loggers.extend(extra_quiet_loggers)

    for logger_name in quiet_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    deleted = purge_old_logs(
        log_dir=str(log_path),
        log_prefix=log_prefix,
        retention_days=retention_days,
        debug_retention_days=debug_retention_days,
    )
    if deleted:
        logging.info("Purged %s expired log file(s)", deleted)

    logging.info(f"日志系统初始化完成，日志目录: {log_path.absolute()}")
    logging.info(f"常规日志: {log_file}")
    logging.info(f"调试日志: {debug_log_file}")
