# -*- coding: utf-8 -*-
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config, get_config
from src.services.sentiment_service import (
    CommunitySentimentService,
    EastmoneyAdapter,
    XueqiuAdapter,
    SentimentResult,
    XueqiuSentimentService,
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


class TestEastmoneyAdapter(unittest.TestCase):
    def test_fetches_posts_from_embedded_article_list(self):
        with patch("src.services.sentiment_service.get_config", return_value=_xq_cfg()):
            adapter = EastmoneyAdapter()

        response = MagicMock(
            status_code=200,
            text=(
                '<script>var article_list={"re":['
                '{"post_title":"今天放量","post_content":"","user_nickname":"a"},'
                '{"post_title":"","post_content":"看空","user_nickname":"b"}'
                "]};</script>"
            ),
        )
        response.raise_for_status.return_value = None
        with patch("src.services.sentiment_service.requests.get", return_value=response) as request_get:
            result = adapter.fetch("000938", "紫光股份")

        request_get.assert_called_once()
        self.assertIn("list,000938", request_get.call_args.args[0])
        self.assertEqual(result.source, "eastmoney")
        self.assertEqual(result.sample_count, 2)
        self.assertEqual(result.highlights[0], "今天放量")
        self.assertEqual(result.highlights[1], "看空")
        self.assertIsNone(result.error)

    def test_exception_becomes_error(self):
        with patch("src.services.sentiment_service.get_config", return_value=_xq_cfg()):
            adapter = EastmoneyAdapter()
        with patch("src.services.sentiment_service.requests.get", side_effect=RuntimeError("boom")):
            result = adapter.fetch("000938", "紫光股份")
        self.assertEqual(result.source, "none")
        self.assertEqual(result.sample_count, 0)
        self.assertIn("boom", result.error or "")


class TestFallbackConfig(unittest.TestCase):
    def tearDown(self):
        Config.reset_instance()

    def test_env_false(self):
        Config.reset_instance()
        with patch.dict(os.environ, {"COMMUNITY_SENTIMENT_FALLBACK_ENABLED": "false"}, clear=False):
            cfg = get_config()
            self.assertFalse(cfg.community_sentiment_fallback_enabled)


class TestCommunityOrchestrator(unittest.TestCase):
    def setUp(self):
        XueqiuAdapter.reset_block_flag()

    def tearDown(self):
        XueqiuAdapter.reset_block_flag()

    def _service(self, **cfg):
        with patch("src.services.sentiment_service.get_config", return_value=_xq_cfg(**cfg)):
            return CommunitySentimentService()

    def test_no_cookie_uses_eastmoney_and_skips_xueqiu_http(self):
        svc = self._service(xueqiu_cookie=None)
        xq = SentimentResult(0, [], [], source="none", reason="no_cookie")
        em = SentimentResult(1, ["股吧观点"], [], source="eastmoney")
        with patch.object(svc._xueqiu, "fetch", return_value=xq) as xq_fetch:
            with patch.object(svc._eastmoney, "fetch", return_value=em) as em_fetch:
                text = svc.build_sentiment_context("000938", "紫光股份")
        xq_fetch.assert_called_once()
        em_fetch.assert_called_once()
        self.assertIn("东方财富", text)
        self.assertIn("股吧观点", text)
        self.assertIn("雪球不可用", text)
        self.assertNotIn("未抓取到有效讨论文本", text)

    def test_xueqiu_posts_skip_eastmoney(self):
        svc = self._service()
        xq = SentimentResult(1, ["雪球观点"], [], source="xueqiu")
        with patch.object(svc._xueqiu, "fetch", return_value=xq):
            with patch.object(svc._eastmoney, "fetch") as em_fetch:
                text = svc.build_sentiment_context("000938", "紫光股份")
        em_fetch.assert_not_called()
        self.assertIn("雪球", text)
        self.assertIn("雪球观点", text)

    def test_both_empty_lists_reasons(self):
        svc = self._service()
        xq = SentimentResult(0, [], [], source="none", reason="empty", error="雪球空列表")
        em = SentimentResult(0, [], [], source="none", reason="empty", error="东方财富未返回讨论文本")
        with patch.object(svc._xueqiu, "fetch", return_value=xq):
            with patch.object(svc._eastmoney, "fetch", return_value=em):
                text = svc.build_sentiment_context("000938", "紫光股份")
        self.assertIn("样本量: 0", text)
        self.assertIn("雪球", text)
        self.assertIn("东方财富", text)

    def test_blocked_xueqiu_uses_eastmoney(self):
        svc = self._service()
        xq = SentimentResult(0, [], [], source="none", reason="blocked", error="雪球被WAF拦截")
        em = SentimentResult(1, ["股吧观点"], [], source="eastmoney")
        with patch.object(svc._xueqiu, "fetch", return_value=xq):
            with patch.object(svc._eastmoney, "fetch", return_value=em):
                text = svc.build_sentiment_context("000938", "紫光股份")
        self.assertIn("东方财富", text)
        self.assertIn("雪球不可用", text)

    def test_eastmoney_error_does_not_raise(self):
        svc = self._service(xueqiu_cookie=None)
        xq = SentimentResult(0, [], [], source="none", reason="no_cookie")
        em = SentimentResult(0, [], [], error="boom", source="none", reason="network")
        with patch.object(svc._xueqiu, "fetch", return_value=xq):
            with patch.object(svc._eastmoney, "fetch", return_value=em):
                text = svc.build_sentiment_context("000938", "紫光股份")
        self.assertIn("boom", text)

    def test_both_disabled_returns_empty(self):
        svc = self._service(xueqiu_sentiment_enabled=False, community_sentiment_fallback_enabled=False)
        self.assertEqual(svc.build_sentiment_context("000938", "紫光股份"), "")

    def test_alias_exists(self):
        self.assertIs(XueqiuSentimentService, CommunitySentimentService)


if __name__ == "__main__":
    unittest.main()
