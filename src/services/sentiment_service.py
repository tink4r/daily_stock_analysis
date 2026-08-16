# -*- coding: utf-8 -*-
"""
雪球舆情情绪服务

说明：
- 集成到本项目内，直接抓取雪球公开搜索接口（可选携带 Cookie）
- 输出情绪分数与样本文本，供 LLM 使用
"""

from __future__ import annotations

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


class XueqiuSentimentService:
    """雪球舆情抓取服务（不做前置词典打分）。"""

    def __init__(self):
        cfg = get_config()
        self.enabled: bool = getattr(cfg, "xueqiu_sentiment_enabled", True)
        self.cookie: Optional[str] = getattr(cfg, "xueqiu_cookie", None)
        self.user_agent: str = getattr(cfg, "xueqiu_user_agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/121.0 Safari/537.36")
        self.max_posts: int = max(5, int(getattr(cfg, "xueqiu_sentiment_max_posts", 20)))
        self.kol_users: List[str] = [
            str(u).strip().lower() for u in (getattr(cfg, "xueqiu_kol_users", []) or []) if str(u).strip()
        ]

    def build_sentiment_context(self, stock_code: str, stock_name: str) -> str:
        if not self.enabled:
            return ""

        result = self._fetch_sentiment(stock_code, stock_name)

        if result.error:
            return (
                "### 💬 社区舆情（雪球）\n"
                f"- 抓取失败: {result.error}\n"
                "- 说明: 可配置 XUEQIU_COOKIE 后重试。"
            )

        lines = [
            "### 💬 社区舆情（雪球）",
            "- 说明: 本阶段不做词典情绪打分，由 LLM 结合全文上下文判断偏多/中性/偏空",
            f"- 样本量: {result.sample_count}",
        ]

        if result.highlights:
            lines.append("- 代表观点:")
            for idx, text in enumerate(result.highlights[:5], 1):
                lines.append(f"  {idx}. {text}")
        else:
            lines.append("- 未抓取到有效讨论文本")

        if self.kol_users:
            lines.append(f"- 大V关注名单: {', '.join(self.kol_users)}")
            if result.kol_highlights:
                lines.append("- 大V观点命中:")
                for idx, item in enumerate(result.kol_highlights[:5], 1):
                    lines.append(f"  {idx}. {item}")
            else:
                lines.append("- 大V观点命中: 暂无")

        return "\n".join(lines)

    def _fetch_sentiment(self, stock_code: str, stock_name: str) -> SentimentResult:
        # 先做域名解析预检，避免报错信息不清晰
        try:
            socket.getaddrinfo("xueqiu.com", 443)
        except socket.gaierror as e:
            msg = f"DNS解析失败（xueqiu.com）: {e}，请检查容器 DNS / 代理配置"
            logger.warning(f"[雪球舆情] {msg}")
            return SentimentResult(0, [], [], error=msg)

        session = requests.Session()
        headers = {
            "User-Agent": self.user_agent,
            "Referer": "https://xueqiu.com/",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        # 预热首页（让雪球设置基础 cookie）
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
            if resp.status_code != 200:
                return SentimentResult(0, [], [], error=f"HTTP {resp.status_code}")

            payload = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
            raw_list = payload.get("list") or payload.get("statuses") or []

            posts: List[Dict[str, str]] = []
            for item in raw_list:
                text = self._extract_text(item)
                if text:
                    author = self._extract_author(item)
                    posts.append({"text": text, "author": author})

            if not posts:
                return SentimentResult(0, [], [])

            highlights = [p["text"] for p in posts[:5]]
            kol_set = set(self.kol_users)
            kol_highlights: List[str] = []
            if kol_set:
                for p in posts:
                    author = p.get("author", "")
                    if author and author.lower() in kol_set:
                        kol_highlights.append(f"@{author}: {p['text'][:120]}")

            return SentimentResult(len(posts), highlights, kol_highlights)

        except requests.exceptions.Timeout as e:
            msg = f"请求超时: {e}"
            logger.warning(f"[雪球舆情] 抓取失败: {msg}")
            return SentimentResult(0, [], [], error=msg)
        except requests.exceptions.ConnectionError as e:
            msg = f"网络连接失败（可能为DNS/网络不可达）: {e}"
            logger.warning(f"[雪球舆情] 抓取失败: {msg}")
            return SentimentResult(0, [], [], error=msg)
        except Exception as e:
            logger.warning(f"[雪球舆情] 抓取失败: {e}")
            return SentimentResult(0, [], [], error=str(e))

    @staticmethod
    def _extract_text(item: Dict[str, Any]) -> str:
        text = item.get("text") or item.get("description") or item.get("title") or ""
        text = re.sub(r"<[^>]+>", "", str(text))
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
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
