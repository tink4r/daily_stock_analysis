# -*- coding: utf-8 -*-
"""
雪球舆情情绪服务

说明：
- 集成到本项目内，直接抓取雪球公开搜索接口（可选携带 Cookie）
- 输出情绪分数与样本文本，供 LLM 使用
"""

from __future__ import annotations

import json
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from src.config import get_config

logger = logging.getLogger(__name__)

WAF_MARKERS = ("aliyun_waf", "_waf_")


def cookie_is_configured(cookie: Optional[str]) -> bool:
    return bool(cookie and str(cookie).strip())


def response_is_blocked(status_code: int, content_type: str, body: str) -> bool:
    if status_code != 200:
        return False
    ct = (content_type or "").lower()
    text = body or ""
    if "application/json" not in ct:
        return True
    lowered = text[:2000].lower()
    return any(marker in lowered for marker in WAF_MARKERS)


@dataclass
class SentimentResult:
    sample_count: int
    highlights: List[str]
    kol_highlights: List[str]
    error: Optional[str] = None
    source: str = "none"
    reason: Optional[str] = None


def _extract_text(item: Dict[str, Any]) -> str:
    text = item.get("text") or item.get("description") or item.get("title") or ""
    text = re.sub(r"<[^>]+>", "", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_author(item: Dict[str, Any]) -> str:
    user = item.get("user") if isinstance(item, dict) else None
    if isinstance(user, dict):
        name = user.get("screen_name") or user.get("name") or user.get("nickname") or ""
        return str(name).strip()
    for key in ("screen_name", "user_name", "username", "author"):
        val = item.get(key) if isinstance(item, dict) else None
        if val:
            return str(val).strip()
    return ""


class XueqiuAdapter:
    """Fetch Xueqiu search posts; skip HTTP after WAF or when cookie is missing."""

    _blocked: bool = False

    def __init__(self):
        cfg = get_config()
        self.cookie: Optional[str] = getattr(cfg, "xueqiu_cookie", None)
        self.user_agent: str = getattr(
            cfg,
            "xueqiu_user_agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/121.0 Safari/537.36",
        )
        self.max_posts: int = max(5, int(getattr(cfg, "xueqiu_sentiment_max_posts", 20)))
        self.kol_users: List[str] = [
            str(u).strip().lower() for u in (getattr(cfg, "xueqiu_kol_users", []) or []) if str(u).strip()
        ]

    @classmethod
    def reset_block_flag(cls) -> None:
        cls._blocked = False

    @classmethod
    def is_blocked(cls) -> bool:
        return cls._blocked

    def fetch(self, stock_code: str, stock_name: str) -> SentimentResult:
        if not cookie_is_configured(self.cookie):
            return SentimentResult(0, [], [], source="none", reason="no_cookie")
        if self._blocked:
            return SentimentResult(0, [], [], source="none", reason="blocked")

        try:
            socket.getaddrinfo("xueqiu.com", 443)
        except socket.gaierror as e:
            msg = f"DNS解析失败（xueqiu.com）: {e}，请检查容器 DNS / 代理配置"
            logger.warning("[雪球舆情] %s", msg)
            return SentimentResult(0, [], [], error=msg, source="none", reason="network")

        session = requests.Session()
        headers = {
            "User-Agent": self.user_agent,
            "Referer": "https://xueqiu.com/",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        try:
            session.get("https://xueqiu.com/", headers=headers, timeout=8)
        except Exception:
            pass

        query = f"{stock_name} {stock_code}".strip()
        url = "https://xueqiu.com/query/v1/search/status.json"
        params = {
            "sortId": "1",
            "q": query,
            "count": str(self.max_posts),
            "page": "1",
        }

        try:
            resp = session.get(url, headers=headers, params=params, timeout=10)
            content_type = resp.headers.get("content-type", "")
            body = resp.text or ""
            if response_is_blocked(resp.status_code, content_type, body):
                XueqiuAdapter._blocked = True
                msg = "雪球被WAF拦截"
                logger.warning("[雪球舆情] %s", msg)
                return SentimentResult(0, [], [], error=msg, source="none", reason="blocked")

            if resp.status_code != 200:
                return SentimentResult(
                    0, [], [], error=f"HTTP {resp.status_code}", source="none", reason="network"
                )

            payload = resp.json() if "application/json" in content_type else {}
            raw_list = payload.get("list") or payload.get("statuses") or []

            posts: List[Dict[str, str]] = []
            for item in raw_list:
                text = _extract_text(item)
                if text:
                    author = _extract_author(item)
                    posts.append({"text": text, "author": author})

            if not posts:
                return SentimentResult(0, [], [], source="none", reason="empty")

            highlights = [p["text"] for p in posts[:5]]
            kol_set = set(self.kol_users)
            kol_highlights: List[str] = []
            if kol_set:
                for p in posts:
                    author = p.get("author", "")
                    if author and author.lower() in kol_set:
                        kol_highlights.append(f"@{author}: {p['text'][:120]}")

            return SentimentResult(len(posts), highlights, kol_highlights, source="xueqiu")

        except requests.exceptions.Timeout as e:
            msg = f"请求超时: {e}"
            logger.warning("[雪球舆情] 抓取失败: %s", msg)
            return SentimentResult(0, [], [], error=msg, source="none", reason="network")
        except requests.exceptions.ConnectionError as e:
            msg = f"网络连接失败（可能为DNS/网络不可达）: {e}"
            logger.warning("[雪球舆情] 抓取失败: %s", msg)
            return SentimentResult(0, [], [], error=msg, source="none", reason="network")
        except Exception as e:
            logger.warning("[雪球舆情] 抓取失败: %s", e)
            return SentimentResult(0, [], [], error=str(e), source="none", reason="network")


class EastmoneyAdapter:
    """Fetch per-stock Eastmoney Guba posts. Never raises to caller."""

    def __init__(self):
        cfg = get_config()
        self.max_posts: int = max(5, int(getattr(cfg, "xueqiu_sentiment_max_posts", 20)))
        self.timeout_seconds: float = 10.0

    def fetch(self, stock_code: str, stock_name: str) -> SentimentResult:
        try:
            posts = self._fetch_guba_posts(stock_code)[: self.max_posts]
            if not posts:
                return SentimentResult(0, [], [], error="东方财富未返回讨论文本", source="none", reason="empty")
            return SentimentResult(len(posts), posts[:5], [], source="eastmoney")
        except Exception as e:
            logger.warning(f"[社区舆情] 东方财富抓取失败: {e}")
            return SentimentResult(0, [], [], error=str(e), source="none", reason="network")

    def _fetch_guba_posts(self, stock_code: str) -> List[str]:
        code = (stock_code or "").strip()
        url = f"https://guba.eastmoney.com/list,{code},f_1.html"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/121.0 Safari/537.36"
            ),
            "Referer": f"https://guba.eastmoney.com/list,{code}.html",
        }
        response = requests.get(url, headers=headers, timeout=self.timeout_seconds)
        response.raise_for_status()

        marker = "var article_list="
        marker_index = response.text.find(marker)
        if marker_index < 0:
            raise ValueError("东方财富股吧页面未包含帖子数据")

        payload_text = response.text[marker_index + len(marker):].lstrip()
        payload, _ = json.JSONDecoder().raw_decode(payload_text)
        rows = payload.get("re") or []

        texts: List[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get("post_title") or row.get("post_content") or ""
            text = re.sub(r"<[^>]+>", "", str(value))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                texts.append(text)
        return texts


class CommunitySentimentService:
    """Community posts for the LLM: Xueqiu if cookie works, else Eastmoney."""

    def __init__(self):
        cfg = get_config()
        self.enabled: bool = getattr(cfg, "xueqiu_sentiment_enabled", True)
        self.fallback_enabled: bool = getattr(cfg, "community_sentiment_fallback_enabled", True)
        self.cookie: Optional[str] = getattr(cfg, "xueqiu_cookie", None)
        self.kol_users: List[str] = [
            str(u).strip().lower() for u in (getattr(cfg, "xueqiu_kol_users", []) or []) if str(u).strip()
        ]
        self._xueqiu = XueqiuAdapter()
        self._eastmoney = EastmoneyAdapter()

    def build_sentiment_context(self, stock_code: str, stock_name: str) -> str:
        if not self.enabled and not self.fallback_enabled:
            return ""

        xq = SentimentResult(0, [], [], source="none")
        if self.enabled:
            xq = self._xueqiu.fetch(stock_code, stock_name)
            if xq.reason == "no_cookie":
                logger.info("[社区舆情] 未配置 XUEQIU_COOKIE，跳过雪球")

        if xq.source == "xueqiu" and xq.highlights:
            return self._format_block(xq, xueqiu_note=None)

        em = SentimentResult(0, [], [], source="none")
        if self.fallback_enabled:
            em = self._eastmoney.fetch(stock_code, stock_name)
            if em.source == "eastmoney" and em.highlights:
                return self._format_block(em, xueqiu_note=self._xueqiu_unavailable_line(xq))

        return self._format_block(em if em.error or em.reason else xq, xueqiu_note=None, extra_errors=[xq, em])

    def _xueqiu_unavailable_line(self, xq: SentimentResult) -> str:
        return "雪球不可用已改用东方财富"

    def _format_block(
        self,
        result: SentimentResult,
        xueqiu_note: Optional[str],
        extra_errors: Optional[List[SentimentResult]] = None,
    ) -> str:
        source_label = {"xueqiu": "雪球", "eastmoney": "东方财富", "none": "无"}.get(result.source, result.source)
        lines = [
            "### 💬 社区舆情",
            f"- 来源: {source_label}",
            "- 说明: 本阶段不做词典情绪打分，由 LLM 结合全文上下文判断偏多/中性/偏空",
            f"- 样本量: {result.sample_count}",
        ]
        if xueqiu_note:
            lines.append(f"- {xueqiu_note}")
        if extra_errors:
            for item in extra_errors:
                if item and (item.error or item.reason):
                    label = "雪球" if item is extra_errors[0] else "东方财富"
                    detail = item.error or item.reason
                    lines.append(f"- {label}: {detail}")
        if result.highlights:
            lines.append("- 代表观点:")
            for idx, text in enumerate(result.highlights[:5], 1):
                lines.append(f"  {idx}. {text}")
        elif not extra_errors:
            if result.error:
                lines.append(f"- 抓取失败: {result.error}")
            else:
                lines.append("- 未抓取到有效讨论文本")
        if result.source == "xueqiu" and self.kol_users:
            lines.append(f"- 大V关注名单: {', '.join(self.kol_users)}")
            if result.kol_highlights:
                lines.append("- 大V观点命中:")
                for idx, item in enumerate(result.kol_highlights[:5], 1):
                    lines.append(f"  {idx}. {item}")
            else:
                lines.append("- 大V观点命中: 暂无")
        return "\n".join(lines)


XueqiuSentimentService = CommunitySentimentService
