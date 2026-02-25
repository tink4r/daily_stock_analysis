# -*- coding: utf-8 -*-
"""
RSSHub 新闻情报服务

职责：
1. 从 RSSHub 拉取最新新闻/行业分析
2. 当摘要过短时，调用 Jina Reader 补全正文摘要
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote
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
        self.max_items: int = max(1, int(getattr(cfg, "rsshub_max_items", 8)))

        self.jina_enabled: bool = getattr(cfg, "jina_reader_enabled", False)
        self.jina_base_url: str = getattr(cfg, "jina_reader_base_url", "https://r.jina.ai/http://")
        self.jina_api_key: Optional[str] = getattr(cfg, "jina_reader_api_key", None)
        self.jina_min_snippet_len: int = max(20, int(getattr(cfg, "jina_reader_min_snippet_len", 80)))

    def build_news_context(self, stock_code: str, stock_name: str) -> str:
        if not self.enabled:
            return ""

        items = self._fetch_news_items(stock_code, stock_name)
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

    def _fetch_news_items(self, stock_code: str, stock_name: str) -> List[NewsItem]:
        if not self.route_templates:
            return []

        for route_tpl in self.route_templates:
            route = route_tpl.format(code=stock_code, name=quote(stock_name))
            url = f"{self.base_url}{route if route.startswith('/') else '/' + route}"
            try:
                resp = requests.get(url, timeout=12)
                if resp.status_code != 200:
                    logger.debug(f"[RSSHub] {url} 返回 HTTP {resp.status_code}")
                    continue
                items = self._parse_feed(resp.text)
                if items:
                    logger.info(f"[RSSHub] 命中路由 {route_tpl}，抓取 {len(items)} 条")
                    self._enhance_with_jina(items)
                    return items
            except Exception as e:
                logger.debug(f"[RSSHub] 路由 {route_tpl} 抓取失败: {e}")

        return []

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
