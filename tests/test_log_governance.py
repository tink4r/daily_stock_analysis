# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.logging_config import _should_delete_log_file, purge_old_logs


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


if __name__ == "__main__":
    unittest.main()
