# -*- coding: utf-8 -*-
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config, get_config
from src.logging_config import DEFAULT_QUIET_LOGGERS, _should_delete_log_file, purge_old_logs, setup_logging


class TestShouldDeleteLogFile(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 16).date()
        self.prefix = "stock_analysis"

    def _decide(self, name):
        return _should_delete_log_file(
            name=name,
            prefix=self.prefix,
            current=self.now,
            retention_days=7,
            debug_retention_days=3,
        )

    def test_keeps_recent_legacy_info(self):
        self.assertFalse(self._decide("stock_analysis_20260815.log"))
        self.assertFalse(self._decide("stock_analysis_20260809.log"))

    def test_deletes_old_legacy_info(self):
        self.assertTrue(self._decide("stock_analysis_20260808.log"))

    def test_keeps_recent_legacy_debug(self):
        self.assertFalse(self._decide("stock_analysis_debug_20260813.log"))

    def test_deletes_old_legacy_debug(self):
        self.assertTrue(self._decide("stock_analysis_debug_20260812.log"))

    def test_timed_rotating_names(self):
        self.assertFalse(self._decide("stock_analysis.log.20260815"))
        self.assertTrue(self._decide("stock_analysis.log.20260808"))
        self.assertTrue(self._decide("stock_analysis_debug.log.20260812"))
        self.assertFalse(self._decide("stock_analysis_debug.log.20260813"))

    def test_size_rotation_leftovers_deleted(self):
        self.assertTrue(self._decide("stock_analysis.log.1"))
        self.assertTrue(self._decide("stock_analysis_debug.log.2"))
        self.assertTrue(self._decide("stock_analysis_20260815.log.1"))

    def test_keeps_active_and_unrelated(self):
        self.assertFalse(self._decide("stock_analysis.log"))
        self.assertFalse(self._decide("stock_analysis_debug.log"))
        self.assertFalse(self._decide("other_app_20260101.log"))
        self.assertFalse(self._decide("notes.txt"))

    def test_invalid_date_returns_none(self):
        self.assertIsNone(self._decide("stock_analysis_20261399.log"))


class TestPurgeOldLogs(unittest.TestCase):
    def test_purges_only_expired_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            reports = Path(tmp) / "reports"
            logs.mkdir()
            reports.mkdir()
            now = datetime(2026, 8, 16)
            files = {
                "stock_analysis_20260815.log": "keep-info",
                "stock_analysis_20260808.log": "drop-info",
                "stock_analysis_debug_20260813.log": "keep-debug",
                "stock_analysis_debug_20260812.log": "drop-debug",
                "stock_analysis.log.1": "drop-size",
                "unrelated.log": "keep-unrelated",
            }
            for name, content in files.items():
                (logs / name).write_text(content, encoding="utf-8")
            report = reports / "daily.md"
            report.write_text("do not touch", encoding="utf-8")

            deleted = purge_old_logs(
                str(logs),
                log_prefix="stock_analysis",
                retention_days=7,
                debug_retention_days=3,
                now=now,
            )
            self.assertEqual(deleted, 3)
            self.assertTrue((logs / "stock_analysis_20260815.log").exists())
            self.assertFalse((logs / "stock_analysis_20260808.log").exists())
            self.assertTrue((logs / "stock_analysis_debug_20260813.log").exists())
            self.assertFalse((logs / "stock_analysis_debug_20260812.log").exists())
            self.assertFalse((logs / "stock_analysis.log.1").exists())
            self.assertTrue((logs / "unrelated.log").exists())
            self.assertEqual(report.read_text(encoding="utf-8"), "do not touch")

    def test_purge_skips_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            victim = logs / "stock_analysis_20260801.log"
            victim.write_text("x", encoding="utf-8")
            original_unlink = Path.unlink

            def boom(self, *args, **kwargs):
                if self.name == "stock_analysis_20260801.log":
                    raise OSError("locked")
                return original_unlink(self, *args, **kwargs)

            Path.unlink = boom
            try:
                deleted = purge_old_logs(
                    str(logs),
                    now=datetime(2026, 8, 16),
                )
            finally:
                Path.unlink = original_unlink
            self.assertEqual(deleted, 0)
            self.assertTrue(victim.exists())


class TestLogRetentionConfig(unittest.TestCase):
    def tearDown(self):
        Config.reset_instance()

    def test_env_override(self):
        Config.reset_instance()
        with patch.dict(
            os.environ,
            {"LOG_RETENTION_DAYS": "10", "LOG_DEBUG_RETENTION_DAYS": "2"},
            clear=False,
        ):
            cfg = get_config()
            self.assertEqual(cfg.log_retention_days, 10)
            self.assertEqual(cfg.log_debug_retention_days, 2)


class TestSetupLogging(unittest.TestCase):
    def _close_handlers(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)

    def tearDown(self):
        self._close_handlers()

    def test_uses_timed_rotating_undated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_logging(
                log_prefix="stock_analysis",
                log_dir=tmp,
                retention_days=7,
                debug_retention_days=3,
            )
            root = logging.getLogger()
            timed = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
            self.assertEqual(len(timed), 2)
            names = {Path(h.baseFilename).name for h in timed}
            self.assertEqual(names, {"stock_analysis.log", "stock_analysis_debug.log"})
            for handler in timed:
                self.assertEqual(handler.when, "MIDNIGHT")
                self.assertFalse(handler.utc)
            info_handler = next(h for h in timed if Path(h.baseFilename).name == "stock_analysis.log")
            debug_handler = next(
                h for h in timed if Path(h.baseFilename).name == "stock_analysis_debug.log"
            )
            self.assertEqual(info_handler.backupCount, 7)
            self.assertEqual(debug_handler.backupCount, 3)
            self._close_handlers()

    def test_quiet_loggers_include_openai(self):
        self.assertIn("openai", DEFAULT_QUIET_LOGGERS)
        self.assertIn("httpcore", DEFAULT_QUIET_LOGGERS)
        self.assertIn("httpx", DEFAULT_QUIET_LOGGERS)
        with tempfile.TemporaryDirectory() as tmp:
            setup_logging(log_prefix="stock_analysis", log_dir=tmp)
            self.assertEqual(logging.getLogger("openai").level, logging.WARNING)
            self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)
            self._close_handlers()

    def test_setup_purges_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "stock_analysis_20260101.log"
            old.write_text("gone", encoding="utf-8")
            setup_logging(
                log_prefix="stock_analysis",
                log_dir=tmp,
                retention_days=7,
                debug_retention_days=3,
            )
            self.assertFalse(old.exists())
            self._close_handlers()


if __name__ == "__main__":
    unittest.main()
