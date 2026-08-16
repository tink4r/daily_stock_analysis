# -*- coding: utf-8 -*-
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.sentiment_service import (
    XueqiuAdapter,
    SentimentResult,
    cookie_is_configured,
    response_is_blocked,
)


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


def _xq_cfg(**kwargs):
    data = dict(
        xueqiu_sentiment_enabled=True,
        xueqiu_cookie="xq_a_token=abc",
        xueqiu_user_agent="test-ua",
        xueqiu_sentiment_max_posts=20,
        xueqiu_kol_users=["kol1"],
        community_sentiment_fallback_enabled=True,
    )
    data.update(kwargs)
    return SimpleNamespace(**data)


class TestXueqiuAdapter(unittest.TestCase):
    def setUp(self):
        XueqiuAdapter.reset_block_flag()

    def tearDown(self):
        XueqiuAdapter.reset_block_flag()

    def test_skips_http_when_cookie_missing(self):
        with patch("src.services.sentiment_service.get_config", return_value=_xq_cfg(xueqiu_cookie=None)):
            adapter = XueqiuAdapter()
        with patch("src.services.sentiment_service.requests.Session") as session_cls:
            result = adapter.fetch("000938", "紫光股份")
        session_cls.assert_not_called()
        self.assertIsInstance(result, SentimentResult)
        self.assertEqual(result.source, "none")
        self.assertEqual(result.reason, "no_cookie")
        self.assertEqual(result.sample_count, 0)

    def test_parses_json_posts(self):
        with patch("src.services.sentiment_service.get_config", return_value=_xq_cfg()):
            adapter = XueqiuAdapter()
        search = MagicMock()
        search.status_code = 200
        search.headers = {"content-type": "application/json"}
        search.text = '{"list":[]}'
        search.json.return_value = {
            "list": [
                {"text": "看好业绩", "user": {"screen_name": "kol1"}},
                {"text": "一般", "user": {"screen_name": "other"}},
            ]
        }
        home = MagicMock()
        session = MagicMock()
        session.get.side_effect = [home, search]
        with patch("src.services.sentiment_service.requests.Session", return_value=session):
            with patch("src.services.sentiment_service.socket.getaddrinfo"):
                result = adapter.fetch("000938", "紫光股份")
        self.assertEqual(result.source, "xueqiu")
        self.assertEqual(result.sample_count, 2)
        self.assertEqual(result.highlights[0], "看好业绩")
        self.assertTrue(any("kol1" in x for x in result.kol_highlights))
        self.assertIsNone(result.error)

    def test_waf_sets_process_skip(self):
        with patch("src.services.sentiment_service.get_config", return_value=_xq_cfg()):
            adapter = XueqiuAdapter()
        waf = MagicMock()
        waf.status_code = 200
        waf.headers = {"content-type": "text/html"}
        waf.text = '<meta name="aliyun_waf_aa" content="x">'
        session = MagicMock()
        session.get.side_effect = [MagicMock(), waf]
        with patch("src.services.sentiment_service.requests.Session", return_value=session):
            with patch("src.services.sentiment_service.socket.getaddrinfo"):
                first = adapter.fetch("000938", "紫光股份")
                second = adapter.fetch("000988", "华工科技")
        self.assertEqual(first.reason, "blocked")
        self.assertEqual(first.source, "none")
        self.assertEqual(second.reason, "blocked")
        self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
