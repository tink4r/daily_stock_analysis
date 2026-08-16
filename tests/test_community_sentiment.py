# -*- coding: utf-8 -*-
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.sentiment_service import cookie_is_configured, response_is_blocked


class TestCookieGate(unittest.TestCase):
    def test_none_and_blank_are_missing(self):
        self.assertFalse(cookie_is_configured(None))
        self.assertFalse(cookie_is_configured(""))
        self.assertFalse(cookie_is_configured("   "))

    def test_non_empty_is_present(self):
        self.assertTrue(cookie_is_configured("xq_a_token=abc"))


class TestWafDetection(unittest.TestCase):
    def test_json_200_is_not_blocked(self):
        self.assertFalse(response_is_blocked(200, "application/json; charset=utf-8", '{"list":[]}'))

    def test_html_200_is_blocked(self):
        body = '<meta name="aliyun_waf_aa" content="x">'
        self.assertTrue(response_is_blocked(200, "text/html", body))

    def test_json_content_type_with_waf_marker_is_blocked(self):
        self.assertTrue(response_is_blocked(200, "application/json", '{"_waf_bd8ce2ce37":"x"}'))

    def test_http_403_is_not_classified_as_waf_html(self):
        self.assertFalse(response_is_blocked(403, "text/html", "<html>"))


if __name__ == "__main__":
    unittest.main()
