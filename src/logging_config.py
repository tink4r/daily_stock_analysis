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
from logging.handlers import RotatingFileHandler
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
) -> None:
    """
    统一的日志系统初始化

    配置三层日志输出：
    1. 控制台：根据 debug 参数或 console_level 设置级别
    2. 常规日志文件：INFO 级别，10MB 轮转，保留 5 个备份
    3. 调试日志文件：DEBUG 级别，50MB 轮转，保留 3 个备份

    Args:
        log_prefix: 日志文件名前缀（如 "api_server" -> api_server_20240101.log）
        log_dir: 日志文件目录，默认 ./logs
        console_level: 控制台日志级别（可选，优先于 debug 参数）
        debug: 是否启用调试模式（控制台输出 DEBUG 级别）
        extra_quiet_loggers: 额外需要降低日志级别的第三方库列表
    """
    # 确定控制台日志级别
    if console_level is not None:
        level = console_level
    else:
        level = logging.DEBUG if debug else logging.INFO

    # 创建日志目录
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # 日志文件路径（按日期分文件）
    today_str = datetime.now().strftime('%Y%m%d')
    log_file = log_path / f"{log_prefix}_{today_str}.log"
    debug_log_file = log_path / f"{log_prefix}_debug_{today_str}.log"

    # 配置根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # 根 logger 设为 DEBUG，由 handler 控制输出级别

    # 清除已有 handler，避免重复添加
    if root_logger.handlers:
        root_logger.handlers.clear()

    # Handler 1: 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(console_handler)

    # Handler 2: 常规日志文件（INFO 级别，10MB 轮转）
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(file_handler)

    # Handler 3: 调试日志文件（DEBUG 级别，包含所有详细信息）
    debug_handler = RotatingFileHandler(
        debug_log_file,
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=3,
        encoding='utf-8'
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root_logger.addHandler(debug_handler)

    # 降低第三方库的日志级别
    quiet_loggers = DEFAULT_QUIET_LOGGERS.copy()
    if extra_quiet_loggers:
        quiet_loggers.extend(extra_quiet_loggers)

    for logger_name in quiet_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    # 输出初始化完成信息
    logging.info(f"日志系统初始化完成，日志目录: {log_path.absolute()}")
    logging.info(f"常规日志: {log_file}")
    logging.info(f"调试日志: {debug_log_file}")
