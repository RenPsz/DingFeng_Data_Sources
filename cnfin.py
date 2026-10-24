# -*- coding: utf-8 -*-
"""
cnfin.py —— 中国金融数据统一接入层（单文件、零强依赖、自带容灾）
================================================================================

面向 A 股 / 指数 / ETF 的行情、财务报表、中国宏观经济（含国家统计局口径）数据，
把「多个免费数据源 + 自动降级 + 重试 + 熔断 + 本地缓存 + 统一字段」封装进一个文件。
拷贝这一个 .py 到任何项目即可使用，核心功能只依赖 requests + pandas。

--------------------------------------------------------------------------------
快速上手
--------------------------------------------------------------------------------
    from cnfin import CNFin

    fin = CNFin()

    fin.quote("600000")                       # 实时快照（可传列表）
    fin.quote(["600000", "000001", "sh000001"])

    fin.kline("600000", start="2024-01-01")   # 日线，默认前复权
    fin.kline("600000", period="week", adjust="hfq", limit=100)
    fin.kline("510300")                       # ETF 一样支持
    fin.kline("000001", is_index=True)        # 上证指数

    fin.minute("600000", freq=5)              # 5 分钟线

    fin.stock_list()                          # 全市场股票列表

    fin.financial_summary("600000")           # 关键财务指标（营收/净利/ROE/EPS…）
    fin.financial_statement("600000", "income")     # 利润表
    fin.financial_statement("600000", "balance")    # 资产负债表
    fin.financial_statement("600000", "cashflow")   # 现金流量表
    fin.dividend("600000")                    # 分红送转

    fin.macro("cpi")                          # 宏观：CPI 同比
    fin.macro("ppi"), fin.macro("gdp"), fin.macro("pmi")
    fin.macro("m2"), fin.macro("lpr"), fin.macro("social_financing")
    fin.macro_catalog()                       # 支持的宏观指标清单

    fin.trade_days("2024-01-01", "2024-12-31")     # 交易日历
    fin.health()                              # 一键体检：每个源的可用性/耗时

--------------------------------------------------------------------------------
命令行
--------------------------------------------------------------------------------
    python cnfin.py health
    python cnfin.py quote 600000 000001
    python cnfin.py kline 600000 --start 2024-01-01 --period day --adjust qfq
    python cnfin.py fin 600000
    python cnfin.py macro cpi --limit 12
    python cnfin.py demo           # 跑一遍全功能冒烟测试

--------------------------------------------------------------------------------
数据源与能力矩阵（★ 为主源，☆ 为备用，× 表示该源不提供）
--------------------------------------------------------------------------------
    能力            tencent  sina  eastmoney  baostock  akshare
    实时行情          ★       ☆       ☆          ×         ☆
    日/周/月K线       ★       ☆       ☆          ☆         ☆
    分钟K线           ★       ☆       ☆          ☆         ☆
    股票列表          ×       ×       ☆          ☆         ☆
    财务报表          ×       ×       ★          ×         ☆
    财务指标          ×       ×       ★          ☆         ☆
    分红送转          ×       ×       ★          ×         ☆
    宏观数据          ×       ×       ★          ×         ★
    交易日历          ★(推导)  ×       ×          ☆         ☆

设计原则：
  1. 能力优先，源其次 —— 调用方永远只面对统一字段，不关心底层是谁。
  2. 失败自动降级 —— 每个能力配一条源优先级链，单个源超时/被墙/改版不影响结果。
  3. 熔断保护 —— 某源连续失败会进入冷却期，避免每次调用都白等超时。
  4. 结果可追溯 —— 返回的 DataFrame.attrs 里带 source / capability / elapsed_ms /
     cached，last_trace() 提供逐源成败明细，方便做数据血缘和问题定位。
  5. 绝不静默造假 —— 所有源都失败时抛 AllSourcesFailed，附带每个源的失败原因。

作者备注：本文件对「东财 push2 行情域名被运营商/代理拦截」做了特殊处理，
会自动切换直连重试；若你的网络环境访问东财正常，东财会自动成为行情备源。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Sequence

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

__version__ = "1.1.0"
__all__ = [
    "CNFin", "Symbol", "get_client",
    "CNFinError", "SourceError", "SourceBlocked", "NoDataError", "AllSourcesFailed",
    "MACRO_CATALOG",
]

log = logging.getLogger("cnfin")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[cnfin] %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.WARNING)


# ==============================================================================
# 0. 常量
# ==============================================================================

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
]

_EM_HEADERS = {"Referer": "https://quote.eastmoney.com/"}
_EM_DATA_HEADERS = {"Referer": "https://data.eastmoney.com/"}
_EM_F10_HEADERS = {"Referer": "https://emweb.securities.eastmoney.com/"}
_SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}

_EM_KLT = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60",
           "day": "101", "week": "102", "month": "103"}
_EM_FQT = {"none": "0", "qfq": "1", "hfq": "2"}
_BS_ADJ = {"none": "3", "qfq": "2", "hfq": "1"}

# 东财 datacenter 宏观报表：key -> (reportName, 中文名, 默认数值列, 备注)
MACRO_CATALOG: dict[str, dict[str, Any]] = {
    "cpi":             {"report": "RPT_ECONOMY_CPI",               "name": "居民消费价格指数(CPI)",
                        "value": "NATIONAL_SAME",   "unit": "同比%", "freq": "月"},
    "ppi":             {"report": "RPT_ECONOMY_PPI",               "name": "工业生产者出厂价格指数(PPI)",
                        "value": "BASE_SAME",       "unit": "同比%", "freq": "月"},
    "gdp":             {"report": "RPT_ECONOMY_GDP",               "name": "国内生产总值(GDP)",
                        "value": "SUM_SAME",        "unit": "同比%", "freq": "季"},
    "pmi":             {"report": "RPT_ECONOMY_PMI",               "name": "制造业采购经理指数(PMI)",
                        "value": "MAKE_INDEX",      "unit": "指数",  "freq": "月"},
    "m2":              {"report": "RPT_ECONOMY_CURRENCY_SUPPLY",   "name": "货币供应量",
                        "value": "BASIC_CURRENCY",  "unit": "亿元", "freq": "月"},
    "money_supply":    {"report": "RPT_ECONOMY_CURRENCY_SUPPLY",   "name": "货币供应量",
                        "value": "BASIC_CURRENCY",  "unit": "亿元", "freq": "月"},
    "lpr":             {"report": "RPTA_WEB_RATE",                 "name": "贷款市场报价利率(LPR)",
                        "value": "LPR1Y",           "unit": "%",    "freq": "日",
                        "host": "web", "sort": "TRADE_DATE"},
    "social_financing": {"report": None,                           "name": "社会融资规模增量",
                        "value": "社会融资规模增量", "unit": "亿元", "freq": "月",
                        "fallback_ak": "macro_china_shrzgm"},
    "trade":           {"report": "RPT_ECONOMY_CUSTOMS",           "name": "进出口总额",
                        "value": "EXIT_BASE",       "unit": "万美元", "freq": "月"},
    "industrial":      {"report": "RPT_ECONOMY_INDUS_GROW",        "name": "工业增加值",
                        "value": "BASE_SAME",       "unit": "同比%", "freq": "月"},
    "fixed_asset":     {"report": "RPT_ECONOMY_ASSET_INVEST",      "name": "固定资产投资",
                        "value": "BASE_SAME",       "unit": "同比%", "freq": "月"},
    "retail":          {"report": "RPT_ECONOMY_TOTAL_RETAIL",      "name": "社会消费品零售总额",
                        "value": "RETAIL_TOTAL_SAME", "unit": "同比%", "freq": "月"},
    "house_price":     {"report": "RPT_ECONOMY_HOUSE_PRICE",       "name": "70城住宅价格指数",
                        "value": "FIRST_COMHOUSE_SAME", "unit": "同比%", "freq": "月"},
    "forex":           {"report": "RPT_ECONOMY_GOLD_CURRENCY",     "name": "外汇储备/黄金储备",
                        "value": "FOREX",           "unit": "亿美元", "freq": "月"},
    "rmb_loan":        {"report": "RPT_ECONOMY_RMB_LOAN",          "name": "新增人民币贷款",
                        "value": "RMB_LOAN",        "unit": "亿元", "freq": "月"},
    "deposit_reserve": {"report": "RPT_ECONOMY_DEPOSIT_RESERVE",   "name": "存款准备金率(大型机构)",
                        "value": "INTEREST_RATE_BB", "unit": "%",   "freq": "不定期"},
    "tax":             {"report": "RPT_ECONOMY_TAX",               "name": "全国税收收入",
                        "value": "TAX_INCOME",      "unit": "亿元", "freq": "月"},
    "fdi":             {"report": "RPT_ECONOMY_FDI",               "name": "外商直接投资(同比)",
                        "value": "ACTUAL_FOREIGN_SAME", "unit": "同比%", "freq": "月",
                        "note": "东财该报表 2023 年后停止更新"},
    "boom_index":      {"report": "RPT_ECONOMY_BOOM_INDEX",        "name": "企业景气指数",
                        "value": "BOOM_INDEX",      "unit": "指数",  "freq": "季"},
}

# 东财 F10 财务字段 -> 统一英文字段
_FIN_FIELD_MAP = {
    "EPSJB": "eps",                  # 基本每股收益
    "EPSKCJB": "eps_diluted",        # 扣非每股收益
    "BPS": "bps",                    # 每股净资产
    "TOTALOPERATEREVE": "revenue",   # 营业总收入
    "TOTALOPERATEREVETZ": "revenue_yoy",
    "PARENTNETPROFIT": "net_profit",  # 归母净利润
    "PARENTNETPROFITTZ": "net_profit_yoy",
    "ROEJQ": "roe",                  # 加权 ROE
    "XSMLL": "gross_margin",         # 销售毛利率
    "XSJLL": "net_margin",           # 销售净利率
    "ZCFZL": "debt_ratio",           # 资产负债率
    "MGJYXJJE": "ocf_per_share",     # 每股经营现金流
    "TOTAL_SHARE": "total_share",
}

_STATEMENT_REPORTS = {
    "balance":  "RPT_DMSK_FN_BALANCE",
    "income":   "RPT_DMSK_FN_INCOME",
    "cashflow": "RPT_DMSK_FN_CASHFLOW",
}

# 三大报表里最常用的字段（东财原始 key），方便直接取数
STATEMENT_FIELDS = {
    "income": {
        "TOTAL_OPERATE_INCOME": "营业总收入", "OPERATE_INCOME": "营业收入",
        "TOTAL_OPERATE_COST": "营业总成本", "OPERATE_COST": "营业成本",
        "SALE_EXPENSE": "销售费用", "MANAGE_EXPENSE": "管理费用",
        "FINANCE_EXPENSE": "财务费用", "OPERATE_PROFIT": "营业利润",
        "TOTAL_PROFIT": "利润总额", "INCOME_TAX": "所得税费用",
        "PARENT_NETPROFIT": "归母净利润", "DEDUCT_PARENT_NETPROFIT": "扣非归母净利润",
        "RESEARCH_EXPENSE": "研发费用", "BASIC_EPS": "基本每股收益",
    },
    "balance": {
        "TOTAL_ASSETS": "资产总计", "TOTAL_LIABILITIES": "负债合计",
        "TOTAL_EQUITY": "所有者权益合计", "MONETARYFUNDS": "货币资金",
        "ACCOUNTS_RECE": "应收账款", "INVENTORY": "存货",
        "FIXED_ASSET": "固定资产", "CIP": "在建工程",
        "GOODWILL": "商誉", "SHORT_LOAN": "短期借款",
        "LONG_LOAN": "长期借款", "DEBT_ASSET_RATIO": "资产负债率(%)",
    },
    "cashflow": {
        "NETCASH_OPERATE": "经营活动现金流量净额",
        "NETCASH_INVEST": "投资活动现金流量净额",
        "NETCASH_FINANCE": "筹资活动现金流量净额",
        "CCE_ADD": "现金及现金等价物净增加额",
        "SALES_SERVICES": "销售商品/提供劳务收到的现金",
        "CONSTRUCT_LONG_ASSET": "购建固定资产等支付的现金",
    },
}


def _report_type_from_date(d: pd.Series) -> pd.Series:
    """由报告期推报告类型，比字段映射更可靠（不同报表 DATE_TYPE_CODE 含义不一致）"""
    month = pd.to_datetime(d, errors="coerce").dt.month
    return month.map({3: "一季报", 6: "中报", 9: "三季报", 12: "年报"}).fillna("其他")



# ==============================================================================
# 1. 异常
# ==============================================================================

class CNFinError(Exception):
    """cnfin 基础异常"""


class SourceError(CNFinError):
    """单个数据源出错（网络/解析/字段变更）"""


class SourceBlocked(SourceError):
    """数据源被 WAF / 运营商 / 代理拦截"""


class NoDataError(CNFinError):
    """请求成功但没有数据（可能是代码错误或停牌）"""


class AllSourcesFailed(CNFinError):
    """所有候选源都失败"""

    def __init__(self, capability: str, errors: dict[str, str]):
        self.capability = capability
        self.errors = errors
        detail = "; ".join(f"{k}: {v[:120]}" for k, v in errors.items())
        super().__init__(f"[{capability}] 所有数据源均失败 -> {detail}")


# ==============================================================================
# 2. 证券代码规范化
# ==============================================================================

_SUFFIX_MARKET = {"SH": "SH", "SS": "SH", "SZ": "SZ", "BJ": "BJ", "BSE": "BJ",
                  "HK": "HK", "OF": "OF"}


@dataclass(frozen=True)
class Symbol:
    """统一的证券标识，负责在各数据源代码格式之间转换"""

    code: str            # 600000
    market: str = "SH"   # SH / SZ / BJ / HK
    is_index: bool = False
    raw: str = ""

    # ---------------- 解析 ----------------
    @classmethod
    def parse(cls, s: str | "Symbol", is_index: bool | None = None) -> "Symbol":
        if isinstance(s, Symbol):
            return cls(s.code, s.market, s.is_index if is_index is None else is_index, s.raw)
        raw = str(s).strip()
        if not raw:
            raise ValueError("证券代码不能为空")
        up = raw.upper().replace(" ", "")
        market: str | None = None
        code = up

        # 600000.SH / 600000.SS
        m = re.fullmatch(r"(\d{5,6})\.([A-Z]{2})", up)
        if m:
            code, market = m.group(1), _SUFFIX_MARKET.get(m.group(2))
            if market is None:
                raise ValueError(f"无法识别的市场后缀: {raw}")
        else:
            # sh600000 / sh.600000 / SH600000
            m = re.fullmatch(r"(SH|SZ|BJ|SS|BSE)\.?(\d{5,6})", up)
            if m:
                market, code = _SUFFIX_MARKET.get(m.group(1)), m.group(2)
            else:
                m = re.fullmatch(r"(\d{5,6})\.(SH|SZ|BJ)", up)
                if m:
                    code, market = m.group(1), m.group(2)
                elif re.fullmatch(r"\d{6}", up):
                    code = up
                elif re.fullmatch(r"\d{5}", up):
                    code, market = up, "HK"
                else:
                    raise ValueError(f"无法识别的证券代码: {raw}")

        if market is None:
            market = cls._infer_market(code, is_index)

        return cls(code=code, market=market,
                   is_index=bool(is_index) if is_index is not None else cls._looks_index(code, market),
                   raw=raw)

    @staticmethod
    def _infer_market(code: str, is_index: bool | None) -> str:
        if len(code) == 5:
            return "HK"
        head = code[0]
        if is_index:
            # 指数：000xxx/000001 -> 上证；399xxx -> 深证
            return "SH" if code.startswith("000") else "SZ"
        if head == "6" or code.startswith("5") or code.startswith("11"):
            return "SH"
        if head in ("0", "3") or code.startswith("15") or code.startswith("16") \
                or code.startswith("12"):
            return "SZ"
        if code.startswith(("4", "8", "920")):
            return "BJ"
        return "SH"

    @staticmethod
    def _looks_index(code: str, market: str) -> bool:
        # 常见指数代码：上证 000xxx / 深证 399xxx / 中证 000300 等
        if market == "SH" and code.startswith("000"):
            return False  # 000001 裸码默认当平安银行，指数需显式 is_index=True
        return False

    # ---------------- 各源格式 ----------------
    @property
    def tencent(self) -> str:
        return f"{self.market.lower()}{self.code}"

    @property
    def sina(self) -> str:
        return f"{self.market.lower()}{self.code}"

    @property
    def em_secid(self) -> str:
        m = {"SH": "1", "SZ": "0", "BJ": "0", "HK": "116"}.get(self.market, "1")
        return f"{m}.{self.code}"

    @property
    def secucode(self) -> str:
        return f"{self.code}.{self.market}"

    @property
    def baostock(self) -> str:
        return f"{self.market.lower()}.{self.code}"

    def __str__(self) -> str:
        return self.secucode


def _sym(s: str | Symbol, is_index: bool | None = None) -> Symbol:
    return Symbol.parse(s, is_index)


# ==============================================================================
# 3. HTTP 客户端（重试 / 限速 / 代理失效自动回退直连）
# ==============================================================================

class _HttpClient:
    """
    轻量 HTTP 客户端：
      - 自动重试（指数退避）
      - 每 host 最小请求间隔（限速，降低被封风险）
      - 代理异常时自动回退到直连（解决「本地代理挡住国内财经站点」的常见坑）
    """

    def __init__(self, timeout: float = 10.0, retries: int = 1,
                 min_interval: float = 0.12, trust_env: bool = True,
                 dead_cooldown: int = 600, proxy: str | None = None):
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self.dead_cooldown = dead_cooldown
        self._proxy = proxy
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._sessions: dict[bool, requests.Session] = {}
        self._trust_env = trust_env
        self._host_fails: dict[str, int] = {}
        self._host_dead: dict[str, float] = {}

    def _session(self, trust_env: bool) -> requests.Session:
        if trust_env not in self._sessions:
            s = requests.Session()
            s.trust_env = trust_env
            # 显式代理只应用于代理会话；直连回退必须真正直连。
            if trust_env and self._proxy:
                s.proxies.update({"http": self._proxy, "https": self._proxy})
            adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            s.headers.update({"Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"})
            self._sessions[trust_env] = s
        return self._sessions[trust_env]

    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last.get(host, 0.0))
            self._last[host] = time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def request(self, url: str, *, headers: dict | None = None, params: dict | None = None,
                data: dict | None = None, method: str = "GET", timeout: float | None = None,
                allow_direct_retry: bool = True) -> requests.Response:
        host = re.sub(r"^https?://", "", url).split("/")[0]
        h = {"User-Agent": _UA_POOL[int(time.time()) % len(_UA_POOL)]}
        if headers:
            h.update(headers)

        with self._lock:
            dead = self._host_dead.get(host, 0) > time.monotonic()
        if dead:
            raise SourceBlocked(f"{host} 近期连续连接失败，冷却中（可用 reset_circuit() 重置）")

        attempts = self.retries + 1
        last_err: Exception | None = None
        trust_modes = [self._trust_env]
        if allow_direct_retry and self._trust_env:
            trust_modes.append(False)

        for i in range(attempts):
            blocked = False
            for trust_env in trust_modes:
                try:
                    self._throttle(host)
                    resp = self._session(trust_env).request(
                        method, url, headers=h, params=params, data=data,
                        timeout=self.timeout if timeout is None else timeout)
                    if resp.status_code == 403:
                        raise SourceBlocked(f"HTTP 403 被拦截: {url[:90]}")
                    if resp.status_code == 456:
                        raise SourceBlocked(f"HTTP 456 反爬限制: {url[:90]}")
                    resp.raise_for_status()
                    with self._lock:
                        self._host_fails.pop(host, None)
                    return resp
                except SourceBlocked as e:
                    last_err, blocked = e, True
                    continue
                except requests.exceptions.RequestException as e:
                    last_err = e
                    continue
            # WAF/反爬通常不会因立即重试而恢复，但已尝试代理与直连两条链路。
            if blocked:
                break
            if i + 1 < attempts:
                time.sleep(0.3 * (2 ** i))

        self._mark_host_fail(host)
        message = f"{type(last_err).__name__}: {str(last_err)[:160]}"
        if isinstance(last_err, SourceBlocked):
            raise SourceBlocked(message)
        raise SourceError(message)

    def _mark_host_fail(self, host: str) -> None:
        with self._lock:
            n = self._host_fails.get(host, 0) + 1
            self._host_fails[host] = n
            if n >= 2:
                self._host_dead[host] = time.monotonic() + self.dead_cooldown
                log.debug("主机 %s 进入冷却 %ss", host, self.dead_cooldown)

    def clear_dead_hosts(self) -> None:
        with self._lock:
            self._host_fails.clear()
            self._host_dead.clear()

    def get_json(self, url: str, **kw) -> Any:
        return self.request(url, **kw).json()

    def get_text(self, url: str, encoding: str | None = None, **kw) -> str:
        r = self.request(url, **kw)
        if encoding:
            r.encoding = encoding
        return r.text


_HTTP = _HttpClient()


def set_proxy(proxy: str | None = None, trust_env: bool | None = None) -> None:
    """设置后续 CNFin 实例的默认代理；不会修改已经创建的实例。"""
    global _HTTP
    _HTTP = _HttpClient(proxy=proxy, trust_env=True if trust_env is None else trust_env)


# ==============================================================================
# 4. 本地缓存
# ==============================================================================

class _Cache:
    """JSON DataFrame 缓存：完整键摘要隔离、TTL、原子替换，不执行反序列化代码。"""

    TTL = {"quote": 10, "kline": 3600, "minute": 60, "financial": 86400,
           "macro": 21600, "list": 86400, "calendar": 86400}
    FORMAT = 1

    def __init__(self, path: str | Path | None = None, enabled: bool = True):
        self.enabled = enabled
        self.root = Path(path) if path else Path.home() / ".cnfin" / "cache"
        if enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.enabled = False

    @staticmethod
    def _digest(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _file(self, key: str) -> Path:
        label = re.sub(r"[^0-9A-Za-z_.\-]", "_", key)[:40]
        return self.root / f"cnfin-{label}-{self._digest(key)}.json"

    def get(self, key: str, ttl: int) -> pd.DataFrame | None:
        if not self.enabled or ttl <= 0:
            return None
        f = self._file(key)
        try:
            if not f.exists() or time.time() - f.stat().st_mtime >= ttl:
                return None
            with f.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if payload.get("format") != self.FORMAT or payload.get("key") != self._digest(key):
                return None
            frame = payload["frame"]
            obj = pd.DataFrame(frame["data"], columns=frame["columns"])
            for column, dtype in zip(obj.columns, payload.get("dtypes", [])):
                try:
                    if dtype.startswith("datetime64"):
                        obj[column] = pd.to_datetime(obj[column], errors="coerce")
                    elif dtype[0] in "iufb":
                        obj[column] = pd.to_numeric(obj[column], errors="coerce").astype(dtype)
                except (TypeError, ValueError):
                    # 类型恢复失败不阻断读取；数值调用方仍会统一做 coercion。
                    pass
            obj.attrs = dict(payload.get("attrs") or {})
            obj.attrs["cached"] = True
            return obj
        except (OSError, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
            return None

    def set(self, key: str, df: pd.DataFrame) -> None:
        if not self.enabled or not isinstance(df, pd.DataFrame):
            return
        tmp_name: str | None = None
        try:
            frame = json.loads(df.to_json(orient="split", index=False, date_format="iso"))
            payload = {
                "format": self.FORMAT,
                "key": self._digest(key),
                "dtypes": [str(dtype) for dtype in df.dtypes],
                "attrs": dict(df.attrs),
                "frame": frame,
            }
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.root,
                prefix=".cnfin-", suffix=".tmp", delete=False,
            ) as fh:
                tmp_name = fh.name
                json.dump(payload, fh, ensure_ascii=False, allow_nan=False, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._file(key))
        except (OSError, ValueError, TypeError):
            if tmp_name:
                try:
                    Path(tmp_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self) -> int:
        n = 0
        if self.root.exists():
            for f in self.root.glob("cnfin-*.json"):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        return n


# ==============================================================================
# 5. 数据源适配器
# ==============================================================================

class BaseSource:
    name = "base"
    capabilities: tuple[str, ...] = ()

    def __init__(self, http: _HttpClient | None = None):
        self.http = http or _HttpClient()

    # 子类实现
    def quote(self, symbols: list[Symbol]) -> pd.DataFrame:
        raise NotImplementedError

    def kline(self, sym: Symbol, period: str, adjust: str,
              start: str | None, end: str | None, limit: int | None) -> pd.DataFrame:
        raise NotImplementedError


def _to_num(s: Any) -> float:
    try:
        if s is None or s == "" or s == "-":
            return float("nan")
        return float(s)
    except (TypeError, ValueError):
        return float("nan")


# ---------------------------------------------------------------- 腾讯
class TencentSource(BaseSource):
    """
    腾讯财经 qt.gtimg.cn / web.ifzq.gtimg.cn
    实测：速度快、无需鉴权、支持股票/指数/ETF/可转债、支持前复权/后复权、
          日/周/月/分钟线全覆盖。是目前最稳的免费行情源。
    """

    name = "tencent"
    capabilities = ("quote", "kline", "minute", "calendar")

    def quote(self, symbols: list[Symbol]) -> pd.DataFrame:
        rows: list[dict] = []
        for chunk in _chunks(symbols, 40):
            codes = ",".join(s.tencent for s in chunk)
            txt = self.http.get_text(f"https://qt.gtimg.cn/q={codes}", encoding="gbk")
            for line in txt.split(";"):
                if "=" not in line:
                    continue
                varname = line.split("=")[0].strip()          # v_sz000001
                payload = line.split('="', 1)[-1].strip().strip('"')
                f = payload.split("~")
                if len(f) < 50 or not f[2]:
                    continue
                code = f[2]
                mkt = varname[2:4].upper() if len(varname) >= 6 else "SH"
                if mkt not in ("SH", "SZ", "BJ"):
                    mkt = "SH"
                rows.append({
                    "code": code,
                    "name": f[1],
                    "market": mkt,
                    "price": _to_num(f[3]),
                    "prev_close": _to_num(f[4]),
                    "open": _to_num(f[5]),
                    "high": _to_num(f[33]),
                    "low": _to_num(f[34]),
                    "change": _to_num(f[31]),
                    "pct_chg": _to_num(f[32]),
                    "volume": _to_num(f[6]) * 100,        # 手 -> 股
                    "amount": _to_num(f[37]) * 1e4,       # 万元 -> 元
                    "turnover": _to_num(f[38]),
                    "pe_ttm": _to_num(f[39]),
                    "amplitude": _to_num(f[43]),
                    "float_mv": _to_num(f[44]) * 1e8,     # 亿 -> 元
                    "total_mv": _to_num(f[45]) * 1e8,
                    "pb": _to_num(f[46]),
                    "volume_ratio": _to_num(f[49]) if len(f) > 49 else float("nan"),
                    "ts": _parse_ts(f[30]),
                })
        if not rows:
            raise NoDataError("腾讯行情返回空")
        return pd.DataFrame(rows)

    def kline(self, sym: Symbol, period: str, adjust: str,
              start: str | None, end: str | None, limit: int | None) -> pd.DataFrame:
        period = {"daily": "day", "d": "day", "weekly": "week", "monthly": "month"}.get(period, period)
        if period not in ("day", "week", "month"):
            raise SourceError(f"腾讯K线不支持 period={period}")
        if adjust not in ("none", "qfq", "hfq"):
            raise SourceError(f"腾讯K线不支持 adjust={adjust}")
        adj = {"none": "", "qfq": "qfq", "hfq": "hfq"}[adjust]

        # 腾讯单次最多返回约 640 根日线，长区间需要按 end 向前翻页。
        PAGE_CAP = 640
        start_s = _fmt_date(start) if start else None
        end_s = _fmt_date(end) if end else date.today().isoformat()
        want = limit or (PAGE_CAP if start_s else 320)
        collected: list[list] = []
        cur_end = end_s
        complete = False

        for _ in range(100):
            count = PAGE_CAP if (start_s or want > PAGE_CAP) else max(want, 1)
            url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                   f"?param={sym.tencent},{period},{start_s or ''},{cur_end},{count},{adj}")
            js = self.http.get_json(url)
            node = (js.get("data") or {}).get(sym.tencent)
            if not node:
                if collected:
                    raise SourceError("腾讯长历史分页中断，结果不完整")
                raise NoDataError(f"腾讯无 {sym.tencent} 数据")
            expected_key = f"{adj}{period}"
            arr = node.get(expected_key)
            if not isinstance(arr, list):
                raise SourceError(f"腾讯响应缺少请求的复权口径 {expected_key}")
            if not arr:
                if not collected:
                    raise NoDataError(f"腾讯 {sym.tencent} {period} 无K线")
                complete = True
                break
            collected = list(arr) + collected
            first_day = str(arr[0][0])[:10]
            if start_s and first_day <= start_s:
                complete = True
                break
            if len(arr) < PAGE_CAP or (not start_s and len(collected) >= want):
                complete = True
                break
            try:
                nxt = datetime.strptime(first_day, "%Y-%m-%d") - timedelta(days=1)
            except ValueError as e:
                raise SourceError(f"腾讯K线日期格式异常: {first_day}") from e
            if nxt.strftime("%Y-%m-%d") >= cur_end:
                raise SourceError("腾讯长历史分页未向前推进")
            cur_end = nxt.strftime("%Y-%m-%d")

        if not complete:
            raise SourceError("腾讯长历史达到分页上限，拒绝返回不完整结果")
        if not collected:
            raise NoDataError(f"腾讯 {sym.tencent} {period} 无K线")
        # 去重 + 排序
        seen: dict[str, list] = {}
        for it in collected:
            if len(it) >= 6:
                seen[str(it[0])[:10]] = it
        rows = [{"date": k, "open": _to_num(v[1]), "close": _to_num(v[2]),
                 "high": _to_num(v[3]), "low": _to_num(v[4]),
                 "volume": _to_num(v[5]) * 100} for k, v in sorted(seen.items())]
        df = pd.DataFrame(rows)
        df["amount"] = float("nan")
        if limit:
            df = df.tail(limit)
        return df

    def minute(self, sym: Symbol, freq: int, limit: int) -> pd.DataFrame:
        url = (f"https://ifzq.gtimg.cn/appstock/app/kline/mkline"
               f"?param={sym.tencent},m{freq},,{limit}")
        js = self.http.get_json(url)
        node = (js.get("data") or {}).get(sym.tencent) or {}
        arr = node.get(f"m{freq}") or []
        rows = []
        for it in arr:
            if len(it) < 6:
                continue
            rows.append({"datetime": _parse_min_ts(it[0]), "open": _to_num(it[1]),
                         "close": _to_num(it[2]), "high": _to_num(it[3]),
                         "low": _to_num(it[4]), "volume": _to_num(it[5]) * 100})
        if not rows:
            raise NoDataError("腾讯分钟线为空")
        return pd.DataFrame(rows)


# ---------------------------------------------------------------- 新浪
class SinaSource(BaseSource):
    """新浪财经 hq.sinajs.cn / money.finance.sina.com.cn。行情快，K线仅日线且条数有上限。"""

    name = "sina"
    capabilities = ("quote", "kline")

    def quote(self, symbols: list[Symbol]) -> pd.DataFrame:
        rows: list[dict] = []
        for chunk in _chunks(symbols, 40):
            codes = ",".join(s.sina for s in chunk)
            txt = self.http.get_text(f"https://hq.sinajs.cn/list={codes}",
                                     encoding="gbk", headers=_SINA_HEADERS)
            for line in txt.split(";"):
                if "=" not in line:
                    continue
                head = line.split("=")[0].strip().split("_")[-1]   # sh600000
                arr = line.split('="', 1)[-1].strip().strip('"').split(",")
                if len(arr) < 33:
                    continue
                price, prev = _to_num(arr[3]), _to_num(arr[2])
                chg = price - prev if prev else float("nan")
                rows.append({
                    "code": head[2:],
                    "market": head[:2].upper(),
                    "name": arr[0],
                    "price": price, "prev_close": prev,
                    "open": _to_num(arr[1]),
                    "high": _to_num(arr[4]), "low": _to_num(arr[5]),
                    "change": chg,
                    "pct_chg": chg / prev * 100 if prev else float("nan"),
                    "volume": _to_num(arr[8]), "amount": _to_num(arr[9]),
                    "ts": f"{arr[30]} {arr[31]}",
                })
        if not rows:
            raise NoDataError("新浪行情返回空")
        return pd.DataFrame(rows)

    def kline(self, sym: Symbol, period: str, adjust: str,
              start: str | None, end: str | None, limit: int | None) -> pd.DataFrame:
        if period not in ("daily", "day", "d"):
            raise SourceError("新浪K线仅支持日线")
        if adjust != "none":
            raise SourceError("新浪K线仅支持不复权（adjust='none'）")
        n = 1000 if start else min(limit or 300, 1000)
        url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={sym.sina}&scale=240&ma=5&datalen={n}")
        js = self.http.get_json(url, headers=_SINA_HEADERS)
        if not js:
            raise NoDataError("新浪K线为空")
        df = pd.DataFrame(js)
        df = df.rename(columns={"day": "date"})
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["amount"] = float("nan")
        df = df[[c for c in ("date", "open", "high", "low", "close", "volume", "amount")
                 if c in df.columns]]
        if start:
            df = df[df["date"] >= _fmt_date(start)]
        if end:
            df = df[df["date"] <= _fmt_date(end)]
        return df.tail(limit) if limit else df


# ---------------------------------------------------------------- 东方财富
class EastmoneySource(BaseSource):
    """
    东方财富。
      行情（push2/push2his）：数据最全，但国内部分网络/代理会 RST，故作为备源。
      数据中心（datacenter/datacenter-web）：财报、宏观、分红，实测稳定，是主源。
    """

    name = "eastmoney"
    capabilities = ("quote", "kline", "minute", "list", "financial", "macro", "dividend")

    # ---------- 行情 ----------
    def quote(self, symbols: list[Symbol]) -> pd.DataFrame:
        fields = ("f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f107,f116,f117,"
                  "f162,f167,f168,f169,f170,f171")
        rows = []
        for s in symbols:
            js = self.http.get_json(
                "https://push2.eastmoney.com/api/qt/stock/get"
                f"?secid={s.em_secid}&fields={fields}", headers=_EM_HEADERS)
            d = js.get("data")
            if not d:
                continue
            scale = 100.0  # 东财价格字段为分
            rows.append({
                "code": d.get("f57"), "name": d.get("f58"),
                "market": s.market,
                "price": _to_num(d.get("f43")) / scale,
                "high": _to_num(d.get("f44")) / scale,
                "low": _to_num(d.get("f45")) / scale,
                "open": _to_num(d.get("f46")) / scale,
                "prev_close": _to_num(d.get("f60")) / scale,
                "volume": _to_num(d.get("f47")) * 100,
                "amount": _to_num(d.get("f48")),
                "turnover": _to_num(d.get("f168")) / 100,
                "pe_ttm": _to_num(d.get("f162")) / 100,
                "pb": _to_num(d.get("f167")) / 100,
                "change": _to_num(d.get("f169")) / scale,
                "pct_chg": _to_num(d.get("f170")) / 100,
                "amplitude": _to_num(d.get("f171")) / 100,
                "total_mv": _to_num(d.get("f116")),
                "float_mv": _to_num(d.get("f117")),
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        if not rows:
            raise NoDataError("东财行情返回空")
        return pd.DataFrame(rows)

    def kline(self, sym: Symbol, period: str, adjust: str,
              start: str | None, end: str | None, limit: int | None) -> pd.DataFrame:
        klt = _EM_KLT.get(period, period)
        fqt = _EM_FQT.get(adjust, "1")
        beg = _fmt_date(start, "%Y%m%d") if start else "0"
        ed = _fmt_date(end, "%Y%m%d") if end else "20500101"
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
               f"?secid={sym.em_secid}&fields1=f1,f2,f3,f4,f5,f6"
               "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
               f"&klt={klt}&fqt={fqt}&beg={beg}&end={ed}"
               + (f"&lmt={limit}" if limit else ""))
        js = self.http.get_json(url, headers=_EM_HEADERS)
        kl = (js.get("data") or {}).get("klines") or []
        if not kl:
            raise NoDataError("东财K线为空")
        rows = [k.split(",") for k in kl]
        df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low",
                                         "volume", "amount", "amplitude", "pct_chg",
                                         "change", "turnover"])
        for c in df.columns[1:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["volume"] = df["volume"] * 100
        return df

    def minute(self, sym: Symbol, freq: int, limit: int) -> pd.DataFrame:
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
               f"?secid={sym.em_secid}&fields1=f1,f2,f3,f4,f5,f6"
               "&fields2=f51,f52,f53,f54,f55,f56,f57"
               f"&klt={freq}&fqt=1&beg=0&end=20500101")
        js = self.http.get_json(url, headers=_EM_HEADERS)
        kl = (js.get("data") or {}).get("klines") or []
        if not kl:
            raise NoDataError("东财分钟线为空")
        rows = [k.split(",")[:7] for k in kl]
        df = pd.DataFrame(rows, columns=["datetime", "open", "close", "high", "low",
                                         "volume", "amount"])
        for c in df.columns[1:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["volume"] = df["volume"] * 100
        return df.tail(limit)

    # ---------- 列表 ----------
    def stock_list(self, market: str = "all") -> pd.DataFrame:
        fs = {"all": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
              "sh": "m:1+t:2,m:1+t:23", "sz": "m:0+t:6,m:0+t:80", "bj": "m:0+t:81+s:2048"}.get(market, market)
        rows: list[dict] = []
        page = 1
        while page <= 60:
            url = ("https://push2.eastmoney.com/api/qt/clist/get"
                   f"?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12&fs={fs}"
                   "&fields=f12,f14,f13,f2,f3,f20")
            js = self.http.get_json(url, headers=_EM_HEADERS)
            data = js.get("data") or {}
            diff = data.get("diff") or []
            if not diff:
                break
            for d in diff:
                rows.append({"code": d.get("f12"), "name": d.get("f14"),
                             "market": Symbol.parse(str(d.get("f12"))).market,
                             "price": d.get("f2"), "pct_chg": d.get("f3"),
                             "total_mv": d.get("f20")})
            if len(rows) >= (data.get("total") or 0):
                break
            page += 1
        if not rows:
            raise NoDataError("东财股票列表为空")
        return pd.DataFrame(rows)

    # ---------- 财务 ----------
    def _f10(self, report: str, sym: Symbol, page_size: int = 12,
             filter_field: str = "SECUCODE", sort: str = "REPORT_DATE",
             page: int = 1) -> pd.DataFrame:
        flt = f"({filter_field}%3D%22{sym.secucode}%22)"
        url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get"
               f"?reportName={report}&columns=ALL&pageNumber={page}&pageSize={page_size}"
               f"&sortTypes=-1&sortColumns={sort}&source=HSF10&client=PC&filter={flt}")
        js = self.http.get_json(url, headers=_EM_F10_HEADERS)
        if not js.get("success", True):
            msg = str(js.get("message"))
            raise NoDataError(msg[:120])
        data = (js.get("result") or {}).get("data") or []
        if not data:
            raise NoDataError(f"{report} 无数据")
        return pd.DataFrame(data)

    def financial_summary(self, sym: Symbol, periods: int = 12) -> pd.DataFrame:
        df = self._f10("RPT_F10_FINANCE_MAINFINADATA", sym, page_size=periods)
        out = pd.DataFrame({
            "report_date": pd.to_datetime(df["REPORT_DATE"]).dt.strftime("%Y-%m-%d"),
            "report_type": df.get("REPORT_TYPE"),
        })
        for src, dst in _FIN_FIELD_MAP.items():
            out[dst] = pd.to_numeric(df[src], errors="coerce") if src in df.columns else float("nan")
        return out

    def financial_statement(self, sym: Symbol, statement: str, periods: int = 8) -> pd.DataFrame:
        report = _STATEMENT_REPORTS.get(statement)
        if not report:
            raise SourceError(f"未知报表类型 {statement}")
        flt = f"(SECURITY_CODE%3D%22{sym.code}%22)"
        url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get"
               f"?reportName={report}&columns=ALL&pageNumber=1&pageSize={periods}"
               f"&sortTypes=-1&sortColumns=REPORT_DATE&filter={flt}")
        js = self.http.get_json(url, headers=_EM_DATA_HEADERS)
        data = (js.get("result") or {}).get("data") or []
        if not data:
            raise NoDataError(f"{statement} 无数据")
        df = pd.DataFrame(data)
        df.insert(0, "report_date", pd.to_datetime(df["REPORT_DATE"]).dt.strftime("%Y-%m-%d"))
        df.insert(1, "report_type", _report_type_from_date(df["REPORT_DATE"]))
        return df.drop(columns=[c for c in ("REPORT_DATE",) if c in df.columns])

    def dividend(self, sym: Symbol, periods: int = 20) -> pd.DataFrame:
        return self._f10("RPT_SHAREBONUS_DET", sym, page_size=periods)

    # ---------- 宏观 ----------
    def macro(self, indicator: str, limit: int = 120) -> pd.DataFrame:
        cfg = MACRO_CATALOG.get(indicator)
        if not cfg:
            raise SourceError(f"未收录的宏观指标: {indicator}")
        if not cfg.get("report"):
            raise SourceError(f"东财数据中心无 {indicator} 报表，交由备用源")
        host = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
                if cfg.get("host") == "web"
                else "https://datacenter.eastmoney.com/api/data/v1/get?")
        sort = cfg.get("sort", "REPORT_DATE")
        url = (host + f"reportName={cfg['report']}&columns=ALL&pageNumber=1"
               f"&pageSize={min(limit, 500)}&sortTypes=-1&sortColumns={sort}")
        js = self.http.get_json(url, headers=_EM_DATA_HEADERS)
        if not js.get("success", True):
            raise NoDataError(str(js.get("message"))[:120])
        data = (js.get("result") or {}).get("data") or []
        if not data:
            raise NoDataError(f"{indicator} 无数据")
        df = pd.DataFrame(data)
        date_col = "TRADE_DATE" if "TRADE_DATE" in df.columns and cfg.get("host") == "web" else "REPORT_DATE"
        df.insert(0, "date", pd.to_datetime(df[date_col], errors="coerce")
                  .dt.strftime("%Y-%m-%d"))
        return df


# ---------------------------------------------------------------- baostock
class BaostockSource(BaseSource):
    """baostock：免费、无注册，历史日线/周线/月线质量好，财报指标齐全；需保持 TCP 连接。"""

    name = "baostock"
    capabilities = ("kline", "list", "financial", "calendar")
    _lock = threading.Lock()

    def _with_session(self, fn: Callable[[Any], Any]) -> Any:
        try:
            import baostock as bs
        except ImportError as e:
            raise SourceError("未安装 baostock: pip install baostock") from e

        with self._lock:
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):   # baostock 会 print "login success!"
                lg = bs.login()
            if lg.error_code != "0":
                raise SourceError(f"baostock 登录失败 {lg.error_code}:{lg.error_msg}")
            try:
                return fn(bs)
            except CNFinError:
                raise
            except Exception as e:  # noqa: BLE001
                # baostock 边界：外部库异常统一转为 SourceError，才能参与路由降级。
                raise SourceError(f"baostock 查询失败: {type(e).__name__}: {e}") from e
            finally:
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        bs.logout()
                except Exception:  # noqa: BLE001
                    pass

    def kline(self, sym: Symbol, period: str, adjust: str,
              start: str | None, end: str | None, limit: int | None) -> pd.DataFrame:
        freq = {"daily": "d", "day": "d", "d": "d", "weekly": "w", "week": "w",
                "monthly": "m", "month": "m"}.get(period)
        if freq not in ("d", "w", "m"):
            raise SourceError(f"baostock 不支持 period={period}")
        if adjust not in _BS_ADJ:
            raise SourceError(f"baostock 不支持 adjust={adjust}")
        flag = _BS_ADJ[adjust]
        s = _fmt_date(start) if start else "2015-01-01"
        e = _fmt_date(end) if end else date.today().isoformat()

        def run(bs):
            rs = bs.query_history_k_data_plus(
                sym.baostock,
                "date,open,high,low,close,volume,amount,pctChg,turn",
                start_date=s, end_date=e, frequency=freq, adjustflag=flag)
            if rs.error_code != "0":
                raise SourceError(f"baostock: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            return pd.DataFrame(rows, columns=rs.fields)

        df = self._with_session(run)
        if df.empty:
            raise NoDataError("baostock 返回空")
        for c in ("open", "high", "low", "close", "volume", "amount", "pctChg", "turn"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.rename(columns={"pctChg": "pct_chg", "turn": "turnover"})
        return df.tail(limit).reset_index(drop=True) if limit else df

    def stock_list(self, market: str = "all") -> pd.DataFrame:
        day = date.today().isoformat()

        def run(bs):
            rows = []
            for d in (day, _prev_trade_guess(day)):
                rs = bs.query_all_stock(day=d)
                if rs.error_code != "0":
                    continue
                while rs.next():
                    rows.append(rs.get_row_data())
                if rows:
                    return pd.DataFrame(rows, columns=rs.fields)
            raise SourceError(f"baostock 股票列表失败: {rs.error_msg}")

        df = self._with_session(run)
        df = df[df["code"].str.match(r"(sh|sz|bj)\.\d{6}")]
        df["market"] = df["code"].str[:2].str.upper()
        df["code"] = df["code"].str[3:]
        return df.rename(columns={"code_name": "name"})[["code", "name", "market"]].reset_index(drop=True)

    def financial_summary(self, sym: Symbol, periods: int = 8) -> pd.DataFrame:
        """用 baostock 的盈利能力/成长能力/偿债能力拼出简化财务摘要"""
        years = sorted({date.today().year - i for i in range(periods // 4 + 1)}, reverse=True)

        def run(bs):
            frames = []
            for y in years:
                for q in (4, 3, 2, 1):
                    rs = bs.query_profit_data(code=sym.baostock, year=y, quarter=q)
                    if rs.error_code != "0":
                        continue
                    rows = []
                    while rs.next():
                        rows.append(rs.get_row_data())
                    if rows:
                        d = dict(zip(rs.fields, rows[0]))
                        d["_y"] = y
                        d["_q"] = q
                        frames.append(d)
            return pd.DataFrame(frames)

        df = self._with_session(run)
        if df.empty:
            raise NoDataError("baostock 财务为空")
        out = pd.DataFrame({
            "report_date": df.apply(lambda r: f"{int(r['_y'])}-{['03-31','06-30','09-30','12-31'][int(r['_q']) - 1]}", axis=1),
            "report_type": df["_q"].map({1: "一季报", 2: "中报", 3: "三季报", 4: "年报"}),
            "eps": pd.to_numeric(df.get("epsTTM"), errors="coerce"),
            "roe": pd.to_numeric(df.get("roeAvg"), errors="coerce") * 100,
            "net_margin": pd.to_numeric(df.get("netProfitMargin"), errors="coerce") * 100,
            "gross_margin": pd.to_numeric(df.get("gpMargin"), errors="coerce") * 100,
        })
        return out.sort_values("report_date", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- akshare
class AkshareSource(BaseSource):
    """
    akshare 作为「长尾兜底」：它内部其实也是爬各家网站，
    但接口面最广，适合补齐东财/腾讯没有的冷门数据（如社融、部分宏观口径）。
    """

    name = "akshare"
    capabilities = ("kline", "list", "financial", "macro")

    def _ak(self):
        # 关掉 akshare 内部的 tqdm 进度条，避免污染调用方的 stdout/stderr
        os.environ.setdefault("TQDM_DISABLE", "1")
        try:
            import akshare as ak
        except ImportError as e:
            raise SourceError("未安装 akshare: pip install akshare") from e
        return ak

    @staticmethod
    def _call(label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """外部库边界：非 cnfin 异常统一包装为 SourceError，参与降级而不是裸抛。"""
        try:
            return fn(*args, **kwargs)
        except CNFinError:
            raise
        except Exception as e:  # noqa: BLE001
            raise SourceError(f"akshare {label} 失败: {type(e).__name__}: {e}") from e

    def financial_summary(self, sym: Symbol, periods: int = 12) -> pd.DataFrame:
        """用 akshare 的财务摘要（新浪/东财口径）作为东财 F10 的备份"""
        ak = self._ak()
        raw = self._call("财务摘要", ak.stock_financial_abstract, symbol=sym.code)
        if raw is None or raw.empty:
            raise NoDataError("akshare 财务摘要为空")
        common = raw[raw["选项"] == "常用指标"].set_index("指标")
        growth = raw[raw["选项"] == "成长能力"].set_index("指标")
        date_cols = [c for c in raw.columns if re.fullmatch(r"\d{8}", str(c))][:periods]
        if not date_cols:
            raise NoDataError("akshare 财务摘要无报告期列")

        def pick(src: pd.DataFrame, name: str, cols):
            if name in src.index:
                return pd.to_numeric(src.loc[name, cols], errors="coerce").values
            return [float("nan")] * len(cols)

        out = pd.DataFrame({
            "report_date": [f"{c[:4]}-{c[4:6]}-{c[6:8]}" for c in date_cols],
            "report_type": _report_type_from_date(pd.Series(date_cols)).values,
            "eps": pick(common, "基本每股收益", date_cols),
            "bps": pick(common, "每股净资产", date_cols),
            "revenue": pick(common, "营业总收入", date_cols),
            "revenue_yoy": pick(growth, "营业总收入增长率", date_cols),
            "net_profit": pick(common, "归母净利润", date_cols),
            "net_profit_yoy": pick(growth, "归属母公司净利润增长率", date_cols),
            "roe": pick(common, "净资产收益率(ROE)", date_cols),
            "gross_margin": pick(common, "毛利率", date_cols),
            "net_margin": pick(common, "销售净利率", date_cols),
            "debt_ratio": pick(common, "资产负债率", date_cols),
        })
        return out

    def financial_statement(self, sym: Symbol, statement: str, periods: int = 8) -> pd.DataFrame:
        """新浪财报（宽表：报告日 + 科目），作为东财三大报表的备份"""
        ak = self._ak()
        name = {"balance": "资产负债表", "income": "利润表", "cashflow": "现金流量表"}.get(statement)
        if not name:
            raise SourceError(f"未知报表 {statement}")
        df = self._call("财务报表", ak.stock_financial_report_sina,
                        stock=f"{sym.market.lower()}{sym.code}", symbol=name)
        if df is None or df.empty:
            raise NoDataError("akshare 报表为空")
        df = df.rename(columns={"报告日": "report_date"})
        df["report_date"] = pd.to_datetime(
            df["report_date"].astype(str), format="%Y%m%d", errors="coerce"
        ).dt.strftime("%Y-%m-%d")
        df.insert(1, "report_type", _report_type_from_date(df["report_date"]))
        return df.head(periods)

    def kline(self, sym: Symbol, period: str, adjust: str,
              start: str | None, end: str | None, limit: int | None) -> pd.DataFrame:
        ak = self._ak()
        per = {"daily": "daily", "day": "daily", "d": "daily",
               "weekly": "weekly", "week": "weekly", "w": "weekly",
               "monthly": "monthly", "month": "monthly", "m": "monthly"}.get(period)
        if per is None:
            raise SourceError(f"akshare 不支持 period={period}")
        if adjust not in ("none", "qfq", "hfq"):
            raise SourceError(f"akshare 不支持 adjust={adjust}")
        s = _fmt_date(start, "%Y%m%d") if start else "20150101"
        e = _fmt_date(end, "%Y%m%d") if end else date.today().strftime("%Y%m%d")
        df = self._call("历史行情", ak.stock_zh_a_hist,
                        symbol=sym.code, period=per, start_date=s, end_date=e,
                        adjust="" if adjust == "none" else adjust)
        if df is None or df.empty:
            raise NoDataError("akshare K线为空")
        df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                "最高": "high", "最低": "low", "成交量": "volume",
                                "成交额": "amount", "涨跌幅": "pct_chg", "换手率": "turnover"})
        if "volume" in df.columns:
            # akshare 的 A 股成交量单位为手；统一为股，与腾讯/东财一致。
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100
        return df.tail(limit).reset_index(drop=True) if limit else df

    def stock_list(self, market: str = "all") -> pd.DataFrame:
        ak = self._ak()
        df = self._call("股票列表", ak.stock_info_a_code_name)
        if df is None or df.empty:
            raise NoDataError("akshare 列表为空")
        df = df.rename(columns={"code": "code", "name": "name"})
        df["market"] = df["code"].map(lambda c: Symbol.parse(c).market)
        return df[["code", "name", "market"]]

    def macro(self, indicator: str, limit: int = 120) -> pd.DataFrame:
        ak = self._ak()
        fn_map = {
            "cpi": "macro_china_cpi", "ppi": "macro_china_ppi", "gdp": "macro_china_gdp",
            "pmi": "macro_china_pmi", "m2": "macro_china_money_supply",
            "money_supply": "macro_china_money_supply", "lpr": "macro_china_lpr",
            "social_financing": "macro_china_shrzgm",
            "trade": "macro_china_hgjck", "industrial": "macro_china_industrial_production_yoy",
            "retail": "macro_china_consumer_goods_retail",
        }
        fn_name = fn_map.get(indicator) or MACRO_CATALOG.get(indicator, {}).get("fallback_ak")
        if not fn_name or not hasattr(ak, fn_name):
            raise SourceError(f"akshare 无对应接口: {indicator}")
        df = self._call(f"宏观 {indicator}", getattr(ak, fn_name))
        if df is None or df.empty:
            raise NoDataError("akshare 宏观为空")
        return _normalize_ak_macro(df).head(limit)


# ==============================================================================
# 6. 路由：优先级 + 熔断 + 缓存 + 降级
# ==============================================================================

def _chunks(seq: Sequence, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _fmt_date(s: str | date | datetime, fmt: str = "%Y-%m-%d") -> str:
    if isinstance(s, (date, datetime)):
        return s.strftime(fmt)
    raw = str(s).strip().replace("/", "-")
    for f in ("%Y-%m-%d", "%Y%m%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(raw, f).strftime(fmt)
        except ValueError:
            continue
    raise ValueError(f"无效日期: {s!r}，应为 YYYY-MM-DD")


def _positive_int(value: int | None, name: str, *, allow_none: bool = True) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return value


def _normalize_period(period: str) -> str:
    normalized = {"daily": "day", "d": "day", "weekly": "week", "w": "week",
                  "monthly": "month", "m": "month"}.get(period, period)
    if normalized not in ("day", "week", "month"):
        raise ValueError("period 只能是 day/daily、week/weekly 或 month/monthly")
    return normalized


def _parse_ts(s: str) -> str:
    s = (s or "").strip()
    if re.fullmatch(r"\d{14}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:]}"
    return s


def _parse_min_ts(s: str) -> str:
    s = (s or "").strip()
    if re.fullmatch(r"\d{12}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"
    return s


def _prev_trade_guess(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    for i in range(1, 8):
        cand = d - timedelta(days=i)
        if cand.weekday() < 5:
            return cand.strftime("%Y-%m-%d")
    return day


def _normalize_ak_macro(df: pd.DataFrame) -> pd.DataFrame:
    """
    akshare 的宏观接口字段是中文且不统一（月份 / 日期 / 统计时间…），
    这里统一补一列标准 date（YYYY-MM-DD），保证和其它源可拼接。
    """
    date_candidates = ["月份", "日期", "统计时间", "时间", "TRADE_DATE", "REPORT_DATE", "季度"]
    col = next((c for c in date_candidates if c in df.columns), None)
    if col is None:
        return df
    raw = df[col].astype(str).str.strip()

    def parse(v: str):
        m = re.match(r"^(\d{4})年(\d{1,2})月", v)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-01"
        m = re.match(r"^(\d{4})(\d{2})$", v)
        if m:
            return f"{m.group(1)}-{m.group(2)}-01"
        m = re.match(r"^(\d{4})年$", v)
        if m:
            return f"{m.group(1)}-12-31"
        try:
            return pd.to_datetime(v).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return None

    df = df.copy()
    dates = raw.map(parse)
    df.insert(0, "date", dates)
    df = df.sort_values("date", ascending=False, na_position="last").reset_index(drop=True)
    # 金十/东财风格的「今值」统一叫 value
    if "今值" in df.columns and "value" not in df.columns:
        df["value"] = pd.to_numeric(df["今值"], errors="coerce")
    return df


class _Router:
    """按优先级调用数据源；失败降级；连续失败熔断。"""

    def __init__(self, cache: _Cache, cooldown: int = 300, max_fail: int = 2):
        self.cache = cache
        self.cooldown = cooldown
        self.max_fail = max_fail
        self._fails: dict[tuple[str, str], int] = {}
        self._blocked_until: dict[tuple[str, str], float] = {}
        self.trace: list[dict] = []
        self._lock = threading.Lock()

    def run(self, capability: str, chain: Sequence[tuple[str, Callable[[], pd.DataFrame]]],
            *, cache_key: str | None = None, ttl: int | None = None,
            validator: Callable[[pd.DataFrame], bool] | None = None) -> pd.DataFrame:
        ttl = self.cache.TTL.get(capability, 0) if ttl is None else ttl
        if cache_key:
            hit = self.cache.get(cache_key, ttl)
            if hit is not None:
                hit.attrs.setdefault("source", "cache")
                return hit

        errors: dict[str, str] = {}
        for name, fn in chain:
            key = (name, capability)
            with self._lock:
                blocked = self._blocked_until.get(key, 0) > time.monotonic()
            if blocked:
                errors[name] = "熔断冷却中"
                continue
            t0 = time.perf_counter()
            try:
                df = fn()
                if not isinstance(df, pd.DataFrame) or df.empty:
                    raise NoDataError("空结果")
                if validator and not validator(df):
                    raise SourceError("字段或数据完整性校验未通过")
                elapsed = int((time.perf_counter() - t0) * 1000)
                df.attrs.update({"source": name, "capability": capability,
                                 "elapsed_ms": elapsed, "cached": False})
                with self._lock:
                    self._fails.pop(key, None)
                    self.trace.append({"capability": capability, "source": name,
                                       "ok": True, "ms": elapsed, "error": ""})
                    if len(self.trace) > 200:
                        self.trace = self.trace[-200:]
                if cache_key:
                    self.cache.set(cache_key, df)
                return df
            except (SourceError, NoDataError) as e:
                elapsed = int((time.perf_counter() - t0) * 1000)
                errors[name] = f"{type(e).__name__}: {str(e)[:160]}"
                with self._lock:
                    # 空数据可能是停牌/非交易日，不应损伤源健康度。
                    if isinstance(e, SourceError):
                        n = self._fails.get(key, 0) + 1
                        self._fails[key] = n
                        if n >= self.max_fail:
                            self._blocked_until[key] = time.monotonic() + self.cooldown
                    self.trace.append({"capability": capability, "source": name,
                                       "ok": False, "ms": elapsed, "error": errors[name]})
                    if len(self.trace) > 200:
                        self.trace = self.trace[-200:]
                log.debug("%s/%s 失败: %s", name, capability, errors[name])
        raise AllSourcesFailed(capability, errors)


# ==============================================================================
# 7. 对外门面
# ==============================================================================

class CNFin:
    """
    中国金融数据统一入口。

    >>> fin = CNFin()
    >>> fin.kline("600000", start="2024-01-01").tail(3)
    >>> fin.macro("cpi", limit=6)
    """

    def __init__(self, cache: bool = True, cache_dir: str | Path | None = None,
                 timeout: float = 12.0, cooldown: int = 300,
                 http: _HttpClient | None = None, proxy: str | None = None,
                 trust_env: bool | None = None, retries: int = 2):
        # 每个实例拥有独立的 Session、限速和 host 熔断状态，避免相互污染。
        self.http = http or _HttpClient(
            timeout=timeout, retries=retries,
            trust_env=_HTTP._trust_env if trust_env is None else trust_env,
            proxy=_HTTP._proxy if proxy is None else proxy,
        )
        self.cache = _Cache(cache_dir, enabled=cache)
        self.router = _Router(self.cache, cooldown=cooldown)
        self.sources: dict[str, BaseSource] = {
            "tencent": TencentSource(self.http),
            "sina": SinaSource(self.http),
            "eastmoney": EastmoneySource(self.http),
            "baostock": BaostockSource(self.http),
            "akshare": AkshareSource(self.http),
        }

    # ---------------- 内部：构建调用链 ----------------
    def _chain(self, capability: str, method: str, *args, prefer: str | None = None,
               order: Sequence[str] | None = None, **kwargs):
        seq = list(order) if order else ["tencent", "sina", "eastmoney", "baostock", "akshare"]
        if prefer:
            if prefer not in self.sources:
                raise ValueError(f"未知数据源 {prefer}，可用: {', '.join(self.sources)}")
            if capability not in self.sources[prefer].capabilities:
                raise ValueError(f"数据源 {prefer} 不支持 {capability}")
            seq = [prefer] + [s for s in seq if s != prefer]
        chain = []
        for name in seq:
            src = self.sources.get(name)
            if src is None or capability not in src.capabilities:
                continue
            fn = getattr(src, method, None)
            if fn is None:
                continue
            chain.append((name, (lambda s=src, f=fn: f(*args, **kwargs))))
        if not chain:
            raise CNFinError(f"没有可用数据源支持 {capability}")
        return chain

    # ---------------- 行情 ----------------
    def quote(self, codes: str | Sequence[str], *, prefer: str | None = None,
              use_cache: bool = True) -> pd.DataFrame:
        """
        实时行情快照。支持股票 / 指数 / ETF / 可转债。

        Returns DataFrame[code, name, price, pct_chg, open, high, low, prev_close,
                          volume, amount, turnover, pe_ttm, pb, float_mv, total_mv, ts]
        """
        syms = list(dict.fromkeys(_sym(c) for c in ([codes] if isinstance(codes, str) else codes)))
        if not syms:
            raise ValueError("codes 不能为空")
        chain = self._chain("quote", "quote", syms, prefer=prefer)
        identities = "_".join(f"{s.market}.{s.code}" for s in syms)
        key = (f"quote_{identities}_{prefer}_{time.strftime('%Y%m%d%H%M')}"
               if use_cache else None)
        expected = {(s.code, s.market) for s in syms}

        def complete(d: pd.DataFrame) -> bool:
            if not {"code", "market"}.issubset(d.columns):
                return False
            actual = list(zip(d["code"].astype(str), d["market"].astype(str)))
            return expected.issubset(set(actual)) and len(actual) == len(set(actual))

        df = self.router.run("quote", chain, cache_key=key,
                             ttl=10 if use_cache else 0, validator=complete)
        cols = ["code", "name", "market", "price", "pct_chg", "change", "open", "high", "low",
                "prev_close", "volume", "amount", "turnover", "pe_ttm", "pb",
                "float_mv", "total_mv", "ts"]
        df = df.copy()
        df = df[[(str(c), str(m)) in expected for c, m in zip(df["code"], df["market"])]]
        order = {identity: i for i, identity in enumerate((s.code, s.market) for s in syms)}
        df["_order"] = [order[(str(c), str(m))] for c, m in zip(df["code"], df["market"])]
        df = df.sort_values("_order").drop(columns="_order")
        for c in cols:
            if c not in df.columns:
                df[c] = float("nan")
        return df[cols].reset_index(drop=True)

    def kline(self, code: str, *, period: str = "daily", adjust: str = "qfq",
              start: str | None = None, end: str | None = None,
              limit: int | None = None, is_index: bool = False,
              prefer: str | None = None, use_cache: bool = True) -> pd.DataFrame:
        """
        历史 K 线。

        period: day/week/month（daily|weekly|monthly 亦可）
        adjust: qfq（前复权，默认）/ hfq（后复权）/ none（不复权）
        返回列：date, open, high, low, close, volume, amount, pct_chg
        """
        period = _normalize_period(period)
        if adjust not in ("none", "qfq", "hfq"):
            raise ValueError("adjust 只能是 none / qfq / hfq")
        limit = _positive_int(limit, "limit")
        start = _fmt_date(start) if start else None
        end = _fmt_date(end) if end else None
        if start and end and start > end:
            raise ValueError("start 不能晚于 end")
        sym = _sym(code, is_index=is_index)
        chain = self._chain("kline", "kline", sym, period, adjust, start, end, limit,
                            prefer=prefer,
                            order=["tencent", "eastmoney", "baostock", "sina", "akshare"])
        key = (f"kline_{sym.code}_{sym.market}_{int(sym.is_index)}_{period}_{adjust}_"
               f"{start}_{end}_{limit}_{prefer}") if use_cache else None
        required = {"date", "open", "high", "low", "close", "volume"}
        df = self.router.run("kline", chain, cache_key=key,
                             ttl=3600 if use_cache else 0,
                             validator=lambda d: required.issubset(d.columns))
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        for c in ("open", "high", "low", "close", "volume", "amount"):
            if c not in df.columns:
                df[c] = float("nan")
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["date", "close"]).drop_duplicates("date", keep="last")
        df = df.sort_values("date")
        if start:
            df = df[df["date"] >= start]
        if end:
            df = df[df["date"] <= end]
        if limit:
            df = df.tail(limit)
        if "pct_chg" not in df.columns or df["pct_chg"].isna().all():
            df["pct_chg"] = df["close"].pct_change() * 100
        cols = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
        return df[cols].reset_index(drop=True)

    def minute(self, code: str, *, freq: int = 5, limit: int = 240,
               prefer: str | None = None) -> pd.DataFrame:
        """分钟 K 线。freq ∈ {1,5,15,30,60}"""
        if freq not in (1, 5, 15, 30, 60):
            raise ValueError("freq 只能是 1 / 5 / 15 / 30 / 60")
        limit = _positive_int(limit, "limit", allow_none=False)
        sym = _sym(code)
        chain = self._chain("minute", "minute", sym, freq, limit, prefer=prefer,
                            order=["tencent", "eastmoney"])
        df = self.router.run("minute", chain, ttl=0).copy()
        for c in ("open", "high", "low", "close", "volume"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.reset_index(drop=True)

    def stock_list(self, market: str = "all", *, prefer: str | None = None) -> pd.DataFrame:
        """全市场股票列表 DataFrame[code, name, market]"""
        market = market.lower()
        if market not in ("all", "sh", "sz", "bj"):
            raise ValueError("market 只能是 all / sh / sz / bj")
        chain = self._chain("list", "stock_list", market, prefer=prefer,
                            order=["akshare", "baostock", "eastmoney"])
        df = self.router.run("list", chain, cache_key=f"list_{market}_{prefer}", ttl=86400)
        if market != "all":
            df = df[df["market"].astype(str).str.upper() == market.upper()]
        return df[["code", "name", "market"]].reset_index(drop=True)

    # ---------------- 财务 ----------------
    def financial_summary(self, code: str, *, periods: int = 12,
                          prefer: str | None = None) -> pd.DataFrame:
        """
        关键财务指标（按报告期倒序）。
        列：report_date, report_type, eps, bps, revenue, revenue_yoy,
            net_profit, net_profit_yoy, roe, gross_margin, net_margin, debt_ratio
        """
        periods = _positive_int(periods, "periods", allow_none=False)
        sym = _sym(code)
        chain = self._chain("financial", "financial_summary", sym, periods, prefer=prefer,
                            order=["eastmoney", "akshare", "baostock"])
        df = self.router.run("financial", chain,
                             cache_key=f"finsum_{sym.market}_{sym.code}_{periods}_{prefer}",
                             ttl=86400,
                             validator=lambda d: "report_date" in d.columns).copy()
        df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        df["report_type"] = _report_type_from_date(df["report_date"])
        cols = ["report_date", "report_type", "eps", "bps", "revenue", "revenue_yoy",
                "net_profit", "net_profit_yoy", "roe", "gross_margin", "net_margin",
                "debt_ratio", "ocf_per_share", "total_share"]
        for c in cols:
            if c not in df.columns:
                df[c] = float("nan")
        return (df[cols].dropna(subset=["report_date"]).drop_duplicates("report_date", keep="first")
                .sort_values("report_date", ascending=False).head(periods).reset_index(drop=True))

    def financial_statement(self, code: str, statement: str = "income", *,
                            periods: int = 8, prefer: str | None = None) -> pd.DataFrame:
        """
        三大报表原始数据（东财口径，字段为原始英文 key，含中文对照见 docstring）。
        statement: income（利润表）/ balance（资产负债表）/ cashflow（现金流量表）
        """
        if statement not in _STATEMENT_REPORTS:
            raise ValueError("statement 只能是 income / balance / cashflow")
        periods = _positive_int(periods, "periods", allow_none=False)
        sym = _sym(code)

        chain = self._chain("financial", "financial_statement", sym, statement, periods,
                            prefer=prefer, order=["eastmoney", "akshare"])
        df = self.router.run("financial", chain,
                             cache_key=(f"finstmt_{sym.market}_{sym.code}_{statement}_"
                                        f"{periods}_{prefer}"), ttl=86400,
                             validator=lambda d: "report_date" in d.columns)
        return df.reset_index(drop=True)

    def dividend(self, code: str, *, periods: int = 20) -> pd.DataFrame:
        """分红送转记录"""
        periods = _positive_int(periods, "periods", allow_none=False)
        sym = _sym(code)
        chain = [("eastmoney",
                  lambda: self.sources["eastmoney"].dividend(sym, periods))]
        return self.router.run("financial", chain,
                               cache_key=f"div_{sym.market}_{sym.code}_{periods}",
                               ttl=86400).reset_index(drop=True)

    @staticmethod
    def statement_fields(statement: str = "income") -> pd.DataFrame:
        """
        三大报表常用字段对照表：DataFrame[field, cn_name]
        用法：fin.financial_statement("600000","income")[["report_date", *fin.statement_fields("income")["field"]]]
        """
        m = STATEMENT_FIELDS.get(statement)
        if not m:
            raise ValueError("statement 只能是 income / balance / cashflow")
        return pd.DataFrame({"field": list(m), "cn_name": list(m.values())})

    # ---------------- 宏观 ----------------
    def macro(self, indicator: str = "cpi", *, limit: int = 120, tidy: bool = False,
              prefer: str | None = None) -> pd.DataFrame:
        """
        中国宏观数据（国家统计局口径，东财数据中心镜像）。

        indicator 见 MACRO_CATALOG，常用：
          cpi / ppi / gdp / pmi / m2 / lpr / social_financing /
          trade / industrial / fixed_asset / retail / house_price / forex
        tidy=True 时返回 [date, period, value, unit] 的整洁长表。
        """
        limit = _positive_int(limit, "limit", allow_none=False)
        key = indicator.lower()
        if key not in MACRO_CATALOG:
            raise ValueError(f"未知指标 {indicator}，可用: {', '.join(MACRO_CATALOG)}")
        chain = self._chain("macro", "macro", key, limit, prefer=prefer,
                            order=["eastmoney", "akshare"])
        df = self.router.run("macro", chain, cache_key=f"macro_{key}_{limit}_{prefer}",
                             ttl=21600, validator=lambda d: "date" in d.columns).copy()
        if tidy:
            cfg = MACRO_CATALOG[key]
            vcol = cfg.get("value")
            if vcol not in df.columns:
                vcol = "value" if "value" in df.columns else None
            if vcol is None:
                raise SourceError(
                    f"{df.attrs.get('source')} 的 {key} 缺少已审定数值列 {cfg.get('value')}，"
                    "拒绝猜测第一个数值列")
            out = pd.DataFrame({
                "date": df["date"],
                "period": df.get("TIME", df["date"]),
                "indicator": key,
                "value": pd.to_numeric(df[vcol], errors="coerce"),
                "unit": cfg.get("unit", ""),
            })
            return out.reset_index(drop=True)
        return df.reset_index(drop=True)

    def macro_catalog(self) -> pd.DataFrame:
        """支持的宏观指标清单"""
        return pd.DataFrame([
            {"indicator": k, "name": v["name"], "unit": v.get("unit"),
             "freq": v.get("freq"), "report": v["report"], "note": v.get("note", "")}
            for k, v in MACRO_CATALOG.items()
        ])

    # ---------------- 国家统计局官方接口（直连） ----------------
    def nbs_query(self, dbcode: str = "hgyd", zb_code: str = "A01",
                  rowcode: str = "zb", colcode: str = "sj",
                  extra_dfwds: list | None = None, retries: int = 2) -> pd.DataFrame:
        """
        直连国家统计局「国家数据」老接口（easyquery.htm）。

        ⚠️ 实测说明：统计局已改版为 SPA，老接口加了 WAF 的 URL ACL，
           大量家宽/机房 IP 直接返回 403（含本机）。若你调用时报 403，
           请改用 fin.macro("cpi")（东财镜像的就是统计局原始口径，数值一致），
           或给请求带上浏览器 Cookie 后重试。

        参数
        ----
        dbcode : hgyd=月度, hgjd=季度, hgnd=年度, hgjc=季度累计, fsyd/fsjd/fsnd=分省
        zb_code: 指标大类代码，如 A01=居民消费价格指数, A02=工业, A03=固定资产投资,
                 A04=国内贸易, A05=对外经济, A09=采购经理指数
        """
        import json as _json
        dfwds = [{"wdcode": "zb", "valuecode": zb_code}]
        if extra_dfwds:
            dfwds += extra_dfwds
        params = {
            "m": "QueryData", "dbcode": dbcode, "rowcode": rowcode,
            "colcode": colcode, "wds": "[]", "dfwds": _json.dumps(dfwds),
            "k1": str(int(time.time() * 1000)),
        }
        last = None
        for i in range(retries + 1):
            try:
                js = self.http.get_json(
                    "https://data.stats.gov.cn/easyquery.htm", params=params,
                    headers={"Referer": "https://data.stats.gov.cn/easyquery.htm?cn=A01",
                             "X-Requested-With": "XMLHttpRequest"},
                    allow_direct_retry=False)
                nodes = (js.get("returndata") or {}).get("datanodes") or []
                if not nodes:
                    raise NoDataError("统计局返回空 datanodes")
                rows = []
                for n in nodes:
                    rows.append({"code": n.get("code"),
                                 "value": (n.get("data") or {}).get("data"),
                                 "strdata": (n.get("data") or {}).get("strdata"),
                                 "dotcount": (n.get("data") or {}).get("dotcount")})
                return pd.DataFrame(rows)
            except SourceBlocked as e:
                last = e
                log.warning("统计局接口被 WAF 拦截（%s），这是已知的 IP/路径级限制", e)
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.5 * (i + 1))
        raise SourceBlocked(
            f"国家统计局接口不可达：{last}。建议改用 fin.macro('cpi') 等东财镜像口径。")

    # ---------------- 交易日历 ----------------
    def trade_days(self, start: str | None = None, end: str | None = None) -> list[str]:
        """
        交易日列表。实现方式：取上证指数日线日期（本地缓存 1 天），
        不依赖任何额外接口，且与真实交易日完全一致。
        """
        end = _fmt_date(end or date.today().isoformat())
        start = _fmt_date(start or f"{int(end[:4]) - 1}-01-01")
        if start > end:
            raise ValueError("start 不能晚于 end")
        key = f"cal_{start}_{end}"
        hit = self.cache.get(key, 86400)
        if hit is not None and "date" in hit.columns:
            return hit["date"].tolist()
        idx = self.kline("sh000001", start=start, end=end, is_index=True, adjust="none")
        days = idx["date"].tolist()
        self.cache.set(key, pd.DataFrame({"date": days}))
        return days

    def is_trade_day(self, day: str | None = None) -> bool:
        d = _fmt_date(day or date.today().isoformat())
        if datetime.strptime(d, "%Y-%m-%d").weekday() >= 5:
            return False
        return d in self.trade_days(start=d, end=d)

    # ---------------- 工具 ----------------
    def health(self, verbose: bool = True) -> pd.DataFrame:
        """
        一键体检：对每个 (能力, 数据源) 做真实小样本调用，输出可用性与耗时。
        这是排查「今天哪个接口挂了」最快的方式。
        """
        tests: list[tuple[str, str, Callable[[], Any]]] = []

        def add(cap, src_name, fn):
            tests.append((cap, src_name, fn))

        add("quote", "tencent", lambda: self.sources["tencent"].quote([_sym("600000")]))
        add("quote", "sina", lambda: self.sources["sina"].quote([_sym("600000")]))
        add("quote", "eastmoney", lambda: self.sources["eastmoney"].quote([_sym("600000")]))
        add("kline", "tencent", lambda: self.sources["tencent"].kline(
            _sym("600000"), "day", "qfq", None, None, 5))
        add("kline", "eastmoney", lambda: self.sources["eastmoney"].kline(
            _sym("600000"), "day", "qfq", None, None, 5))
        add("kline", "baostock", lambda: self.sources["baostock"].kline(
            _sym("600000"), "day", "qfq", None, None, 5))
        add("kline", "sina", lambda: self.sources["sina"].kline(
            _sym("600000"), "day", "qfq", None, None, 5))
        add("list", "akshare", lambda: self.sources["akshare"].stock_list())
        add("list", "eastmoney", lambda: self.sources["eastmoney"].stock_list())
        add("financial", "eastmoney", lambda: self.sources["eastmoney"].financial_summary(
            _sym("600000"), 2))
        add("financial", "baostock", lambda: self.sources["baostock"].financial_summary(
            _sym("600000"), 4))
        add("macro", "eastmoney", lambda: self.sources["eastmoney"].macro("cpi", 3))
        add("macro", "akshare", lambda: self.sources["akshare"].macro("cpi", 3))
        add("minute", "tencent", lambda: self.sources["tencent"].minute(_sym("600000"), 5, 10))

        rows = []
        for cap, src_name, fn in tests:
            t0 = time.perf_counter()
            ok, err, n = True, "", 0
            try:
                out = fn()
                n = len(out) if hasattr(out, "__len__") else 1
                ok = n > 0
            except Exception as e:  # noqa: BLE001
                ok, err = False, f"{type(e).__name__}: {str(e)[:90]}"
            ms = int((time.perf_counter() - t0) * 1000)
            rows.append({"capability": cap, "source": src_name, "ok": ok,
                         "rows": n, "ms": ms, "error": err})
            if verbose:
                print(f"[{'OK  ' if ok else 'FAIL'}] {cap:10s} {src_name:10s} "
                      f"{n:5d}行 {ms:6d}ms {err}", flush=True)
        df = pd.DataFrame(rows)
        if verbose:
            ok_n = int(df["ok"].sum())
            print(f"\n通过 {ok_n}/{len(df)} 项")
        return df

    def clear_cache(self) -> int:
        return self.cache.clear()

    def reset_circuit(self) -> None:
        """清空熔断状态（换了网络/代理后调用，让所有源重新参与路由）"""
        self.router._fails.clear()
        self.router._blocked_until.clear()
        self.http.clear_dead_hosts()

    def last_trace(self) -> pd.DataFrame:
        """最近路由尝试明细（最多 40 条，含源、成败、耗时与错误）"""
        return pd.DataFrame(self.router.trace[-40:])


def get_client(**kwargs) -> CNFin:
    """便捷工厂：get_client(timeout=15)"""
    return CNFin(**kwargs)


# ==============================================================================
# 8. CLI
# ==============================================================================

def _cli(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="cnfin", description="中国金融数据统一接入层")
    p.add_argument("--no-cache", action="store_true", help="禁用本地缓存")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("health", help="全源体检")
    sub.add_parser("demo", help="全功能冒烟测试")
    sub.add_parser("catalog", help="宏观指标清单")

    q = sub.add_parser("quote", help="实时行情")
    q.add_argument("codes", nargs="+")

    k = sub.add_parser("kline", help="历史K线")
    k.add_argument("code")
    k.add_argument("--period", default="daily")
    k.add_argument("--adjust", default="qfq", choices=["qfq", "hfq", "none"])
    k.add_argument("--start")
    k.add_argument("--end")
    k.add_argument("--limit", type=int)
    k.add_argument("--index", action="store_true")

    f = sub.add_parser("fin", help="财务摘要")
    f.add_argument("code")
    f.add_argument("--periods", type=int, default=8)
    f.add_argument("--statement", choices=["income", "balance", "cashflow"])

    m = sub.add_parser("macro", help="宏观数据")
    m.add_argument("indicator", nargs="?", default="cpi")
    m.add_argument("--limit", type=int, default=12)
    m.add_argument("--tidy", action="store_true")

    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 0

    fin = CNFin(cache=not a.no_cache)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.unicode.east_asian_width", True)

    if a.cmd == "health":
        fin.health()
    elif a.cmd == "catalog":
        print(fin.macro_catalog().to_string(index=False))
    elif a.cmd == "quote":
        print(fin.quote(a.codes).to_string(index=False))
    elif a.cmd == "kline":
        df = fin.kline(a.code, period=a.period, adjust=a.adjust, start=a.start,
                       end=a.end, limit=a.limit, is_index=a.index)
        print(df.tail(20).to_string(index=False))
        print(f"\n共 {len(df)} 行 | 源: {df.attrs.get('source')} | "
              f"耗时 {df.attrs.get('elapsed_ms')}ms")
    elif a.cmd == "fin":
        if a.statement:
            print(fin.financial_statement(a.code, a.statement, periods=a.periods).to_string(index=False))
        else:
            print(fin.financial_summary(a.code, periods=a.periods).to_string(index=False))
    elif a.cmd == "macro":
        print(fin.macro(a.indicator, limit=a.limit, tidy=a.tidy).to_string(index=False))
    elif a.cmd == "demo":
        _demo(fin)
    return 0


def _demo(fin: CNFin) -> None:
    """冒烟测试：把每个能力都跑一遍，打印结果摘要"""
    print("=" * 78)
    print(f"cnfin {__version__} 冒烟测试")
    print("=" * 78)

    def step(title, fn):
        print(f"\n── {title} " + "─" * max(0, 60 - len(title)))
        try:
            out = fn()
            if isinstance(out, pd.DataFrame):
                print(out.head(5).to_string(index=False))
                print(f"-> {len(out)} 行, 源={out.attrs.get('source')}, "
                      f"{out.attrs.get('elapsed_ms')}ms")
            else:
                print(out)
        except Exception as e:  # noqa: BLE001
            print(f"!! 失败: {type(e).__name__}: {e}")

    step("实时行情 600000/000001/上证指数",
         lambda: fin.quote(["600000", "000001", "sh000001"]))
    step("日K线 600000 前复权", lambda: fin.kline("600000", limit=5))
    step("周K线 600000 后复权", lambda: fin.kline("600000", period="week", adjust="hfq", limit=3))
    step("ETF 510300", lambda: fin.kline("510300", limit=3))
    step("指数 000300", lambda: fin.kline("000300", is_index=True, limit=3))
    step("5分钟线 600000", lambda: fin.minute("600000", freq=5, limit=5))
    step("股票列表", lambda: fin.stock_list().head(5))
    step("财务摘要 600000", lambda: fin.financial_summary("600000", periods=4))
    step("利润表 600000", lambda: fin.financial_statement("600000", "income", periods=2))
    step("资产负债表 600000", lambda: fin.financial_statement("600000", "balance", periods=2))
    step("现金流量表 600000", lambda: fin.financial_statement("600000", "cashflow", periods=2))
    step("分红 600000", lambda: fin.dividend("600000", periods=3))
    step("宏观 CPI", lambda: fin.macro("cpi", limit=3))
    step("宏观 GDP", lambda: fin.macro("gdp", limit=3))
    step("宏观 PMI", lambda: fin.macro("pmi", limit=3))
    step("宏观 LPR", lambda: fin.macro("lpr", limit=3))
    step("交易日历", lambda: f"最近交易日: {fin.trade_days(end='2026-10-24')[-3:]}")


if __name__ == "__main__":
    raise SystemExit(_cli())
