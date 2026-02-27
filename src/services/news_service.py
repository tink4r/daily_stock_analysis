# -*- coding: utf-8 -*-
"""
RSSHub 新闻情报服务

职责：
1. 从 RSSHub 拉取最新新闻/行业分析
2. 当摘要过短时，调用 Jina Reader 补全正文摘要
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse
import xml.etree.ElementTree as ET

import requests

from src.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class NewsItem:
    title: str
    link: str
    published: str
    summary: str


class RssNewsService:
    def __init__(self):
        cfg = get_config()
        self.enabled: bool = getattr(cfg, "rsshub_enabled", True)
        self.base_url: str = (getattr(cfg, "rsshub_base_url", "http://rsshub:1200") or "").rstrip("/")
        self.route_templates: List[str] = getattr(cfg, "rsshub_route_templates", []) or []
        self.stock_route_templates: List[str] = getattr(cfg, "rsshub_stock_route_templates", []) or []
        self.market_route_templates: List[str] = getattr(cfg, "rsshub_market_route_templates", []) or []
        self.max_items: int = max(1, int(getattr(cfg, "rsshub_max_items", 8)))

        self.jina_enabled: bool = getattr(cfg, "jina_reader_enabled", False)
        self.jina_base_url: str = getattr(cfg, "jina_reader_base_url", "https://r.jina.ai/http://")
        self.jina_api_key: Optional[str] = getattr(cfg, "jina_reader_api_key", None)
        self.jina_min_snippet_len: int = max(20, int(getattr(cfg, "jina_reader_min_snippet_len", 80)))

        # 路由稳定性控制（抗反爬/抗抖动）
        self._route_fail_counts: Dict[str, int] = {}
        self._route_last_fail_ts: Dict[str, float] = {}
        self._route_fail_threshold: int = 3
        self._route_cooldown_seconds: int = 15 * 60

    def build_news_context(self, stock_code: str, stock_name: str, scene: str = "stock") -> str:
        if not self.enabled:
            return ""

        route_templates = self._resolve_route_templates(scene)
        items = self._fetch_news_items(stock_code, stock_name, route_templates, scene=scene)
        if not items:
            return "### 📰 最新新闻 / 行业分析（RSSHub）\n- 未抓取到有效新闻条目"

        lines = ["### 📰 最新新闻 / 行业分析（RSSHub）"]
        for idx, item in enumerate(items[: self.max_items], 1):
            lines.append(f"{idx}. {item.title}")
            if item.published:
                lines.append(f"   - 时间: {item.published}")
            if item.link:
                lines.append(f"   - 链接: {item.link}")
            if item.summary:
                lines.append(f"   - 摘要: {item.summary}")

        return "\n".join(lines)

    def _resolve_route_templates(self, scene: str) -> List[str]:
        scene_lower = (scene or "stock").lower()
        if scene_lower == "market":
            routes = self.market_route_templates or self.route_templates
        else:
            routes = self.stock_route_templates or self.route_templates
        return [r for r in routes if r]

    def _fetch_news_items(
        self,
        stock_code: str,
        stock_name: str,
        route_templates: List[str],
        scene: str = "stock",
    ) -> List[NewsItem]:
        if not route_templates:
            return []

        route_vars = self._build_route_vars(stock_code, stock_name)
        all_items: List[NewsItem] = []
        route_success_count = 0
        route_skip_count = 0
        for route_tpl in route_templates:
            if self._should_skip_route(route_tpl):
                route_skip_count += 1
                logger.info(f"[RSSHub] 路由冷却中，暂时跳过: {route_tpl}")
                continue

            route = self._safe_format_route(route_tpl, route_vars)
            url = f"{self.base_url}{route if route.startswith('/') else '/' + route}"
            try:
                resp = self._request_with_retry(url)
                if resp.status_code != 200:
                    self._record_route_failure(route_tpl)
                    logger.info(f"[RSSHub] {url} 返回 HTTP {resp.status_code}")
                    continue
                items = self._parse_feed(resp.text)
                if items:
                    route_success_count += 1
                    self._record_route_success(route_tpl)
                    logger.info(f"[RSSHub] 命中路由 {route_tpl}，抓取 {len(items)} 条")
                    all_items.extend(items)
                else:
                    self._record_route_failure(route_tpl)
            except Exception as e:
                self._record_route_failure(route_tpl)
                logger.info(f"[RSSHub] 路由 {route_tpl} 抓取失败: {e}")

        logger.info(
            f"[RSSHub] {stock_name}({stock_code}) scene={scene} 路由尝试 {len(route_templates)} 条，"
            f"命中 {route_success_count} 条，跳过 {route_skip_count} 条，原始新闻 {len(all_items)} 条"
        )

        if not all_items:
            return []

        ranked_items = self._rank_and_filter_items(all_items, stock_code, stock_name, scene)
        selected = ranked_items[: self.max_items]
        logger.info(
            f"[RSSHub] {stock_name}({stock_code}) 排序后 {len(ranked_items)} 条，"
            f"Top-N 输出 {len(selected)} 条（RSSHUB_MAX_ITEMS={self.max_items}）"
        )
        self._enhance_with_jina(selected)
        return selected

    def _build_route_vars(self, stock_code: str, stock_name: str) -> Dict[str, str]:
        code = (stock_code or "").strip()
        name = (stock_name or "").strip()
        return {
            "code": code,
            "name": quote(name),
            "xq_id": self._to_xueqiu_symbol_id(code),
        }

    def _request_with_retry(self, url: str) -> requests.Response:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                return requests.get(url, timeout=12)
            except Exception as e:
                last_error = e
                # 轻量随机抖动，减少短时间集中失败
                time.sleep(0.4 + random.random() * 0.8 + attempt * 0.2)
        if last_error:
            raise last_error
        raise RuntimeError("RSSHub request failed without explicit exception")

    def _should_skip_route(self, route_tpl: str) -> bool:
        fail_count = self._route_fail_counts.get(route_tpl, 0)
        if fail_count < self._route_fail_threshold:
            return False
        last_ts = self._route_last_fail_ts.get(route_tpl, 0)
        return (time.time() - last_ts) < self._route_cooldown_seconds

    def _record_route_failure(self, route_tpl: str) -> None:
        self._route_fail_counts[route_tpl] = self._route_fail_counts.get(route_tpl, 0) + 1
        self._route_last_fail_ts[route_tpl] = time.time()

    def _record_route_success(self, route_tpl: str) -> None:
        self._route_fail_counts.pop(route_tpl, None)
        self._route_last_fail_ts.pop(route_tpl, None)

    @staticmethod
    def _safe_format_route(route_tpl: str, route_vars: Dict[str, str]) -> str:
        class _DefaultDict(dict):
            def __missing__(self, key):
                return ""

        return route_tpl.format_map(_DefaultDict(route_vars))

    @staticmethod
    def _to_xueqiu_symbol_id(stock_code: str) -> str:
        """转换为雪球路由要求的代码格式：SH600519 / SZ002182。"""
        code = (stock_code or "").strip().upper()
        if not code:
            return ""
        if code.startswith(("SH", "SZ", "BJ")):
            return code
        if not code.isdigit() or len(code) != 6:
            return code
        if code.startswith(("6", "9")):
            return f"SH{code}"
        return f"SZ{code}"

    def _rank_and_filter_items(
        self,
        items: List[NewsItem],
        stock_code: str,
        stock_name: str,
        scene: str,
    ) -> List[NewsItem]:
        deduped = self._dedupe_items(items)
        if not deduped:
            logger.info(f"[RSSHub] {stock_name}({stock_code}) 去重后 0 条")
            return []

        scene_lower = (scene or "stock").lower()
        related_count = len(deduped)
        if scene_lower == "stock":
            keywords = self._build_stock_keywords(stock_code, stock_name)
            related = [it for it in deduped if self._is_stock_related(it, keywords)]
            related_count = len(related)
            # 若严格筛选后为空，降级为不过滤但仍排序，避免上下文完全空白
            target_items = related if related else deduped
        else:
            keywords = []
            target_items = deduped

        logger.info(
            f"[RSSHub] {stock_name}({stock_code}) 去重后 {len(deduped)} 条，"
            f"相关性命中 {related_count} 条，参与排序 {len(target_items)} 条"
        )

        scored: List[Tuple[float, NewsItem]] = []
        for item in target_items:
            score = self._score_item(item, stock_code, stock_name, keywords, scene_lower)
            scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored]

    def _dedupe_items(self, items: List[NewsItem]) -> List[NewsItem]:
        seen: Dict[str, bool] = {}
        output: List[NewsItem] = []
        for item in items:
            title_key = re.sub(r"\s+", "", (item.title or "").lower())
            link_key = (item.link or "").strip().lower()
            key = f"{title_key}|{link_key}"
            if key in seen:
                continue
            seen[key] = True
            output.append(item)
        return output

    def _build_stock_keywords(self, stock_code: str, stock_name: str) -> List[str]:
        code = (stock_code or "").strip()
        name = (stock_name or "").strip()
        keywords = [k for k in [name, code] if k]
        # 常见代码形态补充（如 sh600519/sz000001）
        if code and code.isdigit() and len(code) == 6:
            if code.startswith(("6", "9")):
                keywords.append(f"sh{code}")
            else:
                keywords.append(f"sz{code}")
        return list(dict.fromkeys([k.lower() for k in keywords]))

    def _is_stock_related(self, item: NewsItem, keywords: List[str]) -> bool:
        if not keywords:
            return True
        text = f"{item.title} {item.summary}".lower()
        return any(k and k in text for k in keywords)

    def _score_item(
        self,
        item: NewsItem,
        stock_code: str,
        stock_name: str,
        keywords: List[str],
        scene: str,
    ) -> float:
        text = f"{item.title} {item.summary}".lower()
        title_text = (item.title or "").lower()
        score = 0.0

        if scene == "stock":
            for kw in keywords:
                if not kw:
                    continue
                if kw in text:
                    score += 2.0
                if kw in title_text:
                    score += 1.5

            # 代码命中额外加权
            code = (stock_code or "").lower()
            if code and code in text:
                score += 2.5
            name = (stock_name or "").lower()
            if name and name in title_text:
                score += 2.0

        score += self._source_weight(item.link)
        score += self._recency_weight(item.published)
        return score

    def _source_weight(self, link: str) -> float:
        if not link:
            return 0.0
        try:
            host = (urlparse(link).netloc or "").lower()
        except Exception:
            return 0.0

        # 轻量来源权重：更权威/时效源略微优先
        if any(k in host for k in ["cls.cn", "wallstreetcn.com", "sina.com.cn"]):
            return 1.2
        if any(k in host for k in ["xueqiu.com", "eastmoney.com", "cnstock.com"]):
            return 0.8
        return 0.3

    def _recency_weight(self, published: str) -> float:
        dt = self._parse_datetime(published)
        if not dt:
            return 0.0

        now = datetime.now(timezone.utc)
        age_hours = max(0.0, (now - dt).total_seconds() / 3600.0)
        if age_hours <= 24:
            return 1.2
        if age_hours <= 72:
            return 0.8
        if age_hours <= 168:
            return 0.4
        return 0.1

    def _parse_datetime(self, value: str) -> Optional[datetime]:
        text = (value or "").strip()
        if not text:
            return None

        # RFC2822 / RSS pubDate
        try:
            dt = parsedate_to_datetime(text)
            if dt is not None:
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass

        # 常见 ISO 格式
        try:
            iso = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _parse_feed(self, xml_text: str) -> List[NewsItem]:
        items: List[NewsItem] = []
        root = ET.fromstring(xml_text)

        # RSS2: channel/item
        for item in root.findall(".//channel/item"):
            title = self._text(item.find("title"))
            link = self._text(item.find("link"))
            published = self._text(item.find("pubDate"))
            summary = self._clean_summary(self._text(item.find("description")))
            if title:
                items.append(NewsItem(title=title, link=link, published=published, summary=summary))

        # Atom: entry
        if not items:
            for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
                title = self._text(entry.find("{http://www.w3.org/2005/Atom}title"))
                link = ""
                link_node = entry.find("{http://www.w3.org/2005/Atom}link")
                if link_node is not None:
                    link = link_node.attrib.get("href", "")
                published = self._text(entry.find("{http://www.w3.org/2005/Atom}updated"))
                summary = self._clean_summary(
                    self._text(entry.find("{http://www.w3.org/2005/Atom}summary"))
                    or self._text(entry.find("{http://www.w3.org/2005/Atom}content"))
                )
                if title:
                    items.append(NewsItem(title=title, link=link, published=published, summary=summary))

        return items

    @staticmethod
    def _text(node) -> str:
        if node is None or node.text is None:
            return ""
        return str(node.text).strip()

    @staticmethod
    def _clean_summary(text: str) -> str:
        text = re.sub(r"<[^>]+>", "", text or "")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _enhance_with_jina(self, items: List[NewsItem]) -> None:
        if not self.jina_enabled:
            return

        for item in items:
            if len(item.summary) >= self.jina_min_snippet_len:
                continue
            if not item.link:
                continue

            text = self._fetch_jina_reader(item.link)
            if text:
                item.summary = text

    def _fetch_jina_reader(self, url: str) -> str:
        reader_url = self._build_reader_url(url)
        headers = {}
        if self.jina_api_key:
            headers["Authorization"] = f"Bearer {self.jina_api_key}"

        try:
            resp = requests.get(reader_url, headers=headers, timeout=12)
            if resp.status_code != 200:
                return ""
            text = resp.text.strip()
            text = re.sub(r"\s+", " ", text)
            return text[:1200]
        except Exception as e:
            logger.debug(f"[JinaReader] 抓取失败: {e}")
            return ""

    def _build_reader_url(self, article_url: str) -> str:
        base = (self.jina_base_url or "https://r.jina.ai/http://").rstrip("/")
        if base.endswith("/http:") or base.endswith("/https:"):
            return f"{base}//{article_url.replace('://', '/') }"
        if base.endswith("/http"):
            return f"{base}://{article_url.replace('://', '/') }"
        if base.endswith("http://") or base.endswith("https://"):
            return f"{base}{article_url}"
        if base.endswith("r.jina.ai"):
            return f"{base}/http://{article_url.replace('https://', '').replace('http://', '')}"
        return f"{base}/{article_url}"
