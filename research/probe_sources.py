"""
probe_sources.py —— 中国金融数据源可用性实测脚本

对 A股行情 / 财务 / 宏观 三大类能力，逐个真实调用候选数据源，
记录：是否成功、行数、列数、样例、耗时、异常信息。
输出 results/source_report.json 与 results/source_report.md
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from dataclasses import dataclass, asdict, field
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
OUT = ROOT / "results"
OUT.mkdir(exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


@dataclass
class Result:
    capability: str        # 能力：quote / daily_kline / financial / macro ...
    source: str            # 数据源名：akshare / baostock / tencent ...
    entry: str             # 具体接口名
    ok: bool = False
    rows: int = 0
    cols: int = 0
    latency_ms: int = 0
    columns: list = field(default_factory=list)
    sample: str = ""
    error: str = ""


RESULTS: list[Result] = []


def run(capability: str, source: str, entry: str):
    """装饰器：返回一个可调用对象，调用时执行并记录结果"""
    def deco(fn):
        def wrapper(*args, **kwargs):
            r = Result(capability=capability, source=source, entry=entry)
            t0 = time.perf_counter()
            try:
                obj = fn(*args, **kwargs)
                if isinstance(obj, pd.DataFrame):
                    r.rows, r.cols = len(obj), obj.shape[1]
                    r.columns = [str(c) for c in obj.columns][:25]
                    if len(obj):
                        r.sample = obj.head(2).to_json(
                            orient="records", force_ascii=False)[:600]
                    r.ok = r.rows > 0
                    if not r.ok:
                        r.error = "返回空表"
                elif isinstance(obj, (list, tuple)):
                    r.rows = len(obj)
                    r.ok = r.rows > 0
                    r.sample = str(obj[:2])[:600]
                else:
                    r.ok = obj is not None
                    r.sample = str(obj)[:300]
            except Exception as e:
                r.ok = False
                r.error = f"{type(e).__name__}: {e}"[:400]
            r.latency_ms = int((time.perf_counter() - t0) * 1000)
            RESULTS.append(r)
            flag = "OK  " if r.ok else "FAIL"
            print(f"[{flag}] {capability:16s} {source:12s} {entry:46s} "
                  f"{r.rows:6d}行 {r.latency_ms:6d}ms {r.error[:70]}", flush=True)
            return r
        return wrapper
    return deco


# ---------------------------------------------------------------- 直连 API
def http_get(url, headers=None, timeout=15, encoding=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    resp = requests.get(url, headers=h, timeout=timeout)
    resp.raise_for_status()
    if encoding:
        resp.encoding = encoding
    return resp


def probe_tencent_quote():
    txt = http_get("https://qt.gtimg.cn/q=sh600000,sz000001", encoding="gbk").text
    rows = []
    for line in txt.strip().split(";"):
        if "=" not in line:
            continue
        payload = line.split('="', 1)[-1].rstrip('"')
        f = payload.split("~")
        if len(f) > 45:
            rows.append({"code": f[2], "name": f[1], "price": float(f[3] or 0),
                         "prev_close": float(f[4] or 0), "open": float(f[5] or 0),
                         "volume": float(f[6] or 0), "high": float(f[33] or 0),
                         "low": float(f[34] or 0), "amount": float(f[37] or 0),
                         "pct_chg": float(f[32] or 0)})
    return pd.DataFrame(rows)


def probe_tencent_kline(code="sh600000", n=120):
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={code},day,,,{n},qfq")
    js = http_get(url).json()
    block = js["data"][code]
    arr = block.get("qfqday") or block.get("day")
    df = pd.DataFrame([a[:6] for a in arr],
                      columns=["date", "open", "close", "high", "low", "volume"])
    for c in ["open", "close", "high", "low", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def probe_sina_quote():
    txt = http_get("https://hq.sinajs.cn/list=sh600000,sz000001",
                   headers={"Referer": "https://finance.sina.com.cn"}, encoding="gbk").text
    rows = []
    for line in txt.strip().split(";"):
        if "=" not in line:
            continue
        arr = line.split('="', 1)[-1].rstrip('"').split(",")
        if len(arr) > 32:
            rows.append({"name": arr[0], "open": float(arr[1]), "prev_close": float(arr[2]),
                         "price": float(arr[3]), "high": float(arr[4]), "low": float(arr[5]),
                         "volume": float(arr[8]), "amount": float(arr[9]),
                         "date": arr[30], "time": arr[31]})
    return pd.DataFrame(rows)


def probe_sina_kline(code="sh600000", n=60):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=5&datalen={n}")
    js = http_get(url, headers={"Referer": "https://finance.sina.com.cn"}).json()
    return pd.DataFrame(js)


def probe_em_quote():
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=1.600000"
           "&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f169,f170,f116,f117")
    js = http_get(url, headers={"Referer": "https://quote.eastmoney.com/"}).json()
    if not js.get("data"):
        raise RuntimeError(f"东财返回空 data: {js}")
    d = js["data"]
    return pd.DataFrame([{"code": d["f57"], "name": d["f58"], "price": d["f43"] / 100,
                          "high": d["f44"] / 100, "low": d["f45"] / 100,
                          "open": d["f46"] / 100, "prev_close": d["f60"] / 100,
                          "pct_chg": d["f170"] / 100, "mktcap": d["f116"]}])


def probe_em_kline():
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600000"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           "&klt=101&fqt=1&beg=20240101&end=20500101")
    js = http_get(url, headers={"Referer": "https://quote.eastmoney.com/"}).json()
    kl = (js.get("data") or {}).get("klines")
    if not kl:
        raise RuntimeError(f"东财K线空: {str(js)[:150]}")
    rows = [k.split(",") for k in kl]
    return pd.DataFrame(rows, columns=["date", "open", "close", "high", "low",
                                       "volume", "amount", "amplitude"])


def probe_em_stocklist():
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=50&po=1&np=1"
           "&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
           "&fields=f12,f14,f2,f3,f20")
    js = http_get(url, headers={"Referer": "https://quote.eastmoney.com/"}).json()
    diff = (js.get("data") or {}).get("diff")
    if not diff:
        raise RuntimeError(f"东财列表空: {str(js)[:150]}")
    return pd.DataFrame(diff)


def probe_em_macro_cpi():
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_ECONOMY_CPI&columns=ALL&pageNumber=1&pageSize=12"
           "&sortColumns=REPORT_DATE&sortTypes=-1")
    js = http_get(url, headers={"Referer": "https://data.eastmoney.com/"}).json()
    data = (js.get("result") or {}).get("data")
    if not data:
        raise RuntimeError(f"空: {str(js)[:150]}")
    return pd.DataFrame(data)


def probe_em_macro_gdp():
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_ECONOMY_GDP&columns=ALL&pageNumber=1&pageSize=8"
           "&sortColumns=REPORT_DATE&sortTypes=-1")
    js = http_get(url, headers={"Referer": "https://data.eastmoney.com/"}).json()
    return pd.DataFrame((js.get("result") or {}).get("data") or [])


def probe_em_macro_pmi():
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_ECONOMY_PMI&columns=ALL&pageNumber=1&pageSize=8"
           "&sortColumns=REPORT_DATE&sortTypes=-1")
    js = http_get(url, headers={"Referer": "https://data.eastmoney.com/"}).json()
    return pd.DataFrame((js.get("result") or {}).get("data") or [])


def probe_em_macro_money():
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_ECONOMY_CURRENCY_SUPPLY&columns=ALL&pageNumber=1&pageSize=8"
           "&sortColumns=REPORT_DATE&sortTypes=-1")
    js = http_get(url, headers={"Referer": "https://data.eastmoney.com/"}).json()
    return pd.DataFrame((js.get("result") or {}).get("data") or [])


def probe_nbs_new():
    """国家统计局新版数据平台"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://data.stats.gov.cn/"})
    # 新版站点：/dg/website/... 前端；老接口已被 WAF 拦截，这里探测新旧两套
    for url in [
        "https://data.stats.gov.cn/dg/website/page.html",
        "https://data.stats.gov.cn/easyquery.htm?cn=A01",
    ]:
        r = s.get(url, timeout=15)
        if r.status_code == 200 and "403" not in r.text[:200]:
            return pd.DataFrame([{"url": url, "status": r.status_code,
                                  "bytes": len(r.content)}])
    raise RuntimeError("统计局 WAF 拦截（UrlACL），新旧接口均 403")


# ---------------------------------------------------------------- akshare
def probe_akshare():
    import akshare as ak

    items = [
        ("stock_list", "stock_info_a_code_name", lambda: ak.stock_info_a_code_name()),
        ("quote", "stock_zh_a_spot_em", lambda: ak.stock_zh_a_spot_em()),
        ("daily_kline", "stock_zh_a_hist", lambda: ak.stock_zh_a_hist(
            symbol="600000", period="daily", start_date="20240101",
            end_date="20241231", adjust="qfq")),
        ("minute_kline", "stock_zh_a_hist_min_em", lambda: ak.stock_zh_a_hist_min_em(
            symbol="600000", period="5", adjust="qfq")),
        ("index_quote", "stock_zh_index_spot_em", lambda: ak.stock_zh_index_spot_em(
            symbol="沪深重要指数")),
        ("index_kline", "index_zh_a_hist", lambda: ak.index_zh_a_hist(
            symbol="000001", period="daily", start_date="20240101", end_date="20241231")),
        ("financial_report", "stock_financial_report_sina", lambda: ak.stock_financial_report_sina(
            stock="sh600000", symbol="资产负债表")),
        ("financial_abstract", "stock_financial_abstract", lambda: ak.stock_financial_abstract(
            symbol="600000")),
        ("financial_indicator", "stock_financial_analysis_indicator", lambda: ak.stock_financial_analysis_indicator(
            symbol="600000", start_year="2023")),
        ("valuation", "stock_a_indicator_lg", lambda: ak.stock_a_indicator_lg(symbol="600000")),
        ("trade_calendar", "tool_trade_date_hist_sina", lambda: ak.tool_trade_date_hist_sina()),
        ("macro_cpi", "macro_china_cpi", lambda: ak.macro_china_cpi()),
        ("macro_ppi", "macro_china_ppi", lambda: ak.macro_china_ppi()),
        ("macro_gdp", "macro_china_gdp", lambda: ak.macro_china_gdp()),
        ("macro_pmi", "macro_china_pmi", lambda: ak.macro_china_pmi()),
        ("macro_money", "macro_china_money_supply", lambda: ak.macro_china_money_supply()),
        ("macro_lpr", "macro_china_lpr", lambda: ak.macro_china_lpr()),
        ("macro_nbs_cpi", "macro_china_cpi_monthly", lambda: ak.macro_china_cpi_monthly()),
    ]
    for cap, entry, fn in items:
        run(cap, "akshare", entry)(fn)()


# ---------------------------------------------------------------- baostock
def probe_baostock():
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        RESULTS.append(Result("login", "baostock", "bs.login", False,
                              error=f"{lg.error_code}:{lg.error_msg}"))
        print("[FAIL] baostock login", lg.error_code, lg.error_msg)
        return
    RESULTS.append(Result("login", "baostock", "bs.login", True, rows=1, sample="login success"))
    print("[OK  ] baostock login", flush=True)

    def q_daily():
        rs = bs.query_history_k_data_plus(
            "sh.600000", "date,code,open,high,low,close,volume,amount,turn,pctChg",
            start_date="2024-01-01", end_date="2024-12-31", frequency="d", adjustflag="2")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)

    def q_all():
        rs = bs.query_all_stock(day="2026-10-23")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)

    def q_profit():
        rs = bs.query_profit_data(code="sh.600000", year=2024, quarter=4)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)

    def q_growth():
        rs = bs.query_growth_data(code="sh.600000", year=2024, quarter=4)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)

    run("daily_kline", "baostock", "query_history_k_data_plus")(q_daily)()
    run("stock_list", "baostock", "query_all_stock")(q_all)()
    run("financial_indicator", "baostock", "query_profit_data")(q_profit)()
    run("financial_indicator", "baostock", "query_growth_data")(q_growth)()
    bs.logout()


# ---------------------------------------------------------------- efinance
def probe_efinance():
    import efinance as ef

    run("quote", "efinance", "stock.get_realtime_quotes")(lambda: ef.stock.get_realtime_quotes(
        ["600000", "000001"]))()
    run("daily_kline", "efinance", "stock.get_quote_history")(lambda: ef.stock.get_quote_history(
        "600000"))()
    run("stock_list", "efinance", "stock.get_realtime_quotes()")(lambda: ef.stock.get_realtime_quotes())()
    run("fund_quote", "efinance", "fund.get_fund_codes")(lambda: ef.fund.get_fund_codes("股票型"))()


# ---------------------------------------------------------------- adata
def probe_adata():
    import adata

    run("daily_kline", "adata", "stock.market.get_market")(lambda: adata.stock.market.get_market(
        stock_code="600000", start_date="2024-01-01", k_type=1, adjust_type=1))()
    run("stock_list", "adata", "stock.info.all_code")(lambda: adata.stock.info.all_code())()
    run("financial", "adata", "stock.finance.get_core_index")(lambda: adata.stock.finance.get_core_index(
        stock_code="600000"))()
    if hasattr(adata, "macro"):
        run("macro", "adata", "macro.get_macro_gdp")(lambda: adata.macro.get_macro_gdp())()


def main():
    print("=" * 110)
    print("直连 HTTP 接口")
    print("=" * 110)
    run("quote", "tencent", "qt.gtimg.cn/q=")(probe_tencent_quote)()
    run("daily_kline", "tencent", "web.ifzq.gtimg.cn fqkline")(probe_tencent_kline)()
    run("quote", "sina", "hq.sinajs.cn/list=")(probe_sina_quote)()
    run("daily_kline", "sina", "CN_MarketData.getKLineData")(probe_sina_kline)()
    run("quote", "eastmoney", "push2 stock/get")(probe_em_quote)()
    run("daily_kline", "eastmoney", "push2his kline/get")(probe_em_kline)()
    run("stock_list", "eastmoney", "push2 clist/get")(probe_em_stocklist)()
    run("macro", "eastmoney", "RPT_ECONOMY_CPI")(probe_em_macro_cpi)()
    run("macro", "eastmoney", "RPT_ECONOMY_GDP")(probe_em_macro_gdp)()
    run("macro", "eastmoney", "RPT_ECONOMY_PMI")(probe_em_macro_pmi)()
    run("macro", "eastmoney", "RPT_ECONOMY_CURRENCY_SUPPLY")(probe_em_macro_money)()
    run("macro", "nbs", "data.stats.gov.cn")(probe_nbs_new)()

    for name, fn in [("akshare", probe_akshare), ("baostock", probe_baostock),
                     ("efinance", probe_efinance), ("adata", probe_adata)]:
        print("=" * 110)
        print(f"SDK: {name}")
        print("=" * 110)
        try:
            fn()
        except Exception:
            traceback.print_exc()

    (OUT / "source_report.json").write_text(
        json.dumps([asdict(r) for r in RESULTS], ensure_ascii=False, indent=2),
        encoding="utf-8")

    df = pd.DataFrame([asdict(r) for r in RESULTS])
    summary = (df.groupby(["capability", "source"])["ok"]
                 .agg(["sum", "count"]).reset_index()
                 .rename(columns={"sum": "ok", "count": "total"}))
    print("\n" + "=" * 110)
    print("汇总")
    print("=" * 110)
    print(summary.to_string(index=False))
    print(f"\n总计 {len(df)} 项，成功 {int(df['ok'].sum())} 项，失败 {int((~df['ok']).sum())} 项")


if __name__ == "__main__":
    main()
