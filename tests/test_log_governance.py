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
        invalid_date = self.logs / f"{self.prefix}_20261301.log"
        self._touch(invalid_date)
        with self.assertLogs("root", level="WARNING") as logs:
            deleted = purge_old_logs(str(self.logs), log_prefix=self.prefix, now=self.now)
        self.assertEqual(deleted, 0)
        self.assertTrue(invalid_date.exists())
        self.assertTrue(
            any("Skipping log file with unparseable date" in record.message for record in logs.records)
        )


if __name__ == "__main__":
    unittest.main()
