# -*- coding: utf-8 -*-
"""
结构化财务情报服务（AkShare）

目标：
1. 获取业绩预告 / 业绩快报 / 业绩报表（结构化）
2. 输出给 LLM 的上下文采用 Markdown 表格 + 紧凑键值对
3. 严禁对数字做截断
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import pandas as pd

from src.config import get_config

logger = logging.getLogger(__name__)


class FinanceIntelService:
    """基于 AkShare 的结构化财务情报服务。"""

    def __init__(self):
        config = get_config()
        self.enabled: bool = getattr(config, "finance_structured_enabled", True)
        self.max_quarters: int = max(1, int(getattr(config, "finance_max_quarters", 6)))

    @staticmethod
    def _is_a_share_code(stock_code: str) -> bool:
        code = (stock_code or "").strip()
        return code.isdigit() and len(code) == 6

    def build_finance_context(self, stock_code: str, stock_name: str) -> str:
        """
        构建结构化财务上下文。

        Returns:
            Markdown 文本（可能为空字符串）
        """
        if not self.enabled:
            return ""

        if not self._is_a_share_code(stock_code):
            return f"### 📊 财务公告（结构化）\n- 当前仅支持 A 股代码，{stock_code} 跳过结构化财务抓取。"

        rows_yjyg, yjyg_date = self._fetch_latest_by_dates("stock_yjyg_em", stock_code)
        rows_yjkb, yjkb_date = self._fetch_latest_by_dates("stock_yjkb_em", stock_code)
        rows_yjbb, yjbb_date = self._fetch_latest_by_dates("stock_yjbb_em", stock_code)

        lines: List[str] = ["### 📊 财务公告（结构化 / AkShare）"]

        lines.extend(self._render_section(
            title="业绩预告",
            rows=rows_yjyg,
            asof=yjyg_date,
            preferred_columns=["股票代码", "股票简称", "预测指标", "业绩变动", "预告类型", "上年同期值", "公告日期"],
        ))

        lines.extend(self._render_section(
            title="业绩快报",
            rows=rows_yjkb,
            asof=yjkb_date,
            preferred_columns=["股票代码", "股票简称", "营业收入", "净利润", "每股收益", "净资产收益率", "公告日期"],
        ))

        lines.extend(self._render_section(
            title="业绩报表",
            rows=rows_yjbb,
            asof=yjbb_date,
            preferred_columns=["股票代码", "股票简称", "营业收入", "营业收入同比增长", "净利润", "净利润同比增长", "每股收益", "公告日期"],
        ))

        lines.extend(self._official_reference_links(stock_code))

        return "\n".join(lines).strip()

    def _fetch_latest_by_dates(self, func_name: str, stock_code: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """按季度日期倒序尝试 AkShare 接口，命中即返回。"""
        try:
            import akshare as ak
        except Exception as e:
            logger.warning(f"[财务情报] akshare 导入失败: {e}")
            return [], None

        func = getattr(ak, func_name, None)
        if not callable(func):
            logger.warning(f"[财务情报] akshare 未找到函数: {func_name}")
            return [], None

        for quarter_date in self._recent_quarter_dates(self.max_quarters):
            date_token = quarter_date.replace("-", "")
            try:
                try:
                    df = func(date=date_token)
                except TypeError:
                    df = func(date_token)

                if df is None or df.empty:
                    continue

                rows = self._filter_rows_by_code(df, stock_code)
                if rows:
                    logger.info(f"[财务情报] {func_name} 命中 {stock_code}，日期={date_token}，条数={len(rows)}")
                    return rows, quarter_date
            except Exception as e:
                logger.debug(f"[财务情报] {func_name}({date_token}) 失败: {e}")
                continue

        return [], None

    @staticmethod
    def _recent_quarter_dates(limit: int) -> List[str]:
        """返回最近 N 个季度末日期字符串（YYYY-MM-DD），按近到远。"""
        quarter_ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
        today = datetime.now().date()
        year = today.year
        dates: List[str] = []

        while len(dates) < limit:
            for m, d in reversed(quarter_ends):
                dt = datetime(year, m, d).date()
                if dt <= today:
                    dates.append(dt.strftime("%Y-%m-%d"))
                if len(dates) >= limit:
                    break
            year -= 1

        return dates

    @staticmethod
    def _norm_code(raw: Any) -> str:
        text = str(raw).strip()
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 6:
            return digits[-6:]
        return digits.zfill(6)

    def _filter_rows_by_code(self, df: pd.DataFrame, stock_code: str) -> List[Dict[str, Any]]:
        code_cols = [
            "股票代码", "代码", "证券代码", "股票代码", "symbol", "code", "SECUCODE", "SECURITY_CODE"
        ]
        target = self._norm_code(stock_code)

        hit_df = None
        for col in code_cols:
            if col in df.columns:
                mask = df[col].astype(str).map(self._norm_code) == target
                hit = df[mask]
                if not hit.empty:
                    hit_df = hit
                    break

        if hit_df is None:
            return []

        records = hit_df.to_dict(orient="records")
        return records[:3]

    def _render_section(
        self,
        title: str,
        rows: List[Dict[str, Any]],
        asof: Optional[str],
        preferred_columns: List[str],
    ) -> List[str]:
        lines = [f"\n#### {title}"]
        if asof:
            lines.append(f"- 数据期: {asof}")

        if not rows:
            lines.append("- 未检索到结构化记录")
            return lines

        table_rows = self._normalize_rows(rows, preferred_columns)
        headers = list(table_rows[0].keys())

        # 紧凑键值对（第一条，不截断数字）
        compact_kv = "；".join(f"{k}={self._cell_to_text(table_rows[0].get(k))}" for k in headers)
        lines.append(f"- 关键值: {compact_kv}")

        # Markdown 表格
        lines.append(self._to_markdown_table(headers, table_rows))
        return lines

    def _normalize_rows(self, rows: List[Dict[str, Any]], preferred_columns: List[str]) -> List[Dict[str, Any]]:
        all_cols: List[str] = []
        for row in rows:
            for col in row.keys():
                if col not in all_cols:
                    all_cols.append(col)

        selected = [c for c in preferred_columns if c in all_cols]
        if not selected:
            selected = all_cols[:10]

        normalized: List[Dict[str, Any]] = []
        for row in rows:
            normalized.append({col: row.get(col, "") for col in selected})
        return normalized

    @staticmethod
    def _cell_to_text(value: Any) -> str:
        if value is None:
            return ""
        # 不做截断，保留完整文本与数字表现
        if isinstance(value, float):
            # 避免 pandas 显示科学计数被截断
            return format(value, ".15g")
        return str(value)

    def _to_markdown_table(self, headers: List[str], rows: List[Dict[str, Any]]) -> str:
        head = "| " + " | ".join(headers) + " |"
        sep = "| " + " | ".join(["---"] * len(headers)) + " |"
        body = [
            "| " + " | ".join(self._escape_md(self._cell_to_text(row.get(h, ""))) for h in headers) + " |"
            for row in rows
        ]
        return "\n".join([head, sep] + body)

    @staticmethod
    def _escape_md(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ").strip()

    def _official_reference_links(self, stock_code: str) -> List[str]:
        """补充官方披露入口，便于 LLM 输出可核验引用。"""
        code = (stock_code or "").strip()
        if not code:
            return []

        cninfo_url = f"https://www.cninfo.com.cn/new/fulltextSearch?keyWord={quote(code)}"
        lines = [
            "\n#### 官方披露入口（请优先引用）",
            f"- 巨潮资讯（官方公告检索）: {cninfo_url}",
        ]

        if code.startswith("6"):
            lines.append("- 上交所公告入口: https://www.sse.com.cn/disclosure/listedinfo/announcement/")
        elif code.startswith(("0", "2", "3")):
            lines.append("- 深交所公告入口: https://www.szse.cn/disclosure/listed/")

        return lines
