from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from cnfin import (
    AkshareSource,
    BaostockSource,
    CNFin,
    EastmoneySource,
    NoDataError,
    SinaSource,
    SourceBlocked,
    SourceError,
    Symbol,
    TencentSource,
    _normalize_ak_macro,
)


SYM = Symbol.parse("600000")


def test_tencent_quote_parses_market_units_and_timestamp() -> None:
    fields = [""] * 55
    values = {1: "浦发银行", 2: "600000", 3: "10.5", 4: "10", 5: "10.1", 6: "12",
              30: "20240102093000", 31: "0.5", 32: "5", 33: "11", 34: "9.5",
              37: "2", 38: "1.2", 39: "5.5", 43: "7", 44: "3", 45: "4",
              46: "0.8", 49: "1.1"}
    for index, value in values.items():
        fields[index] = value
    http = Mock()
    http.get_text.return_value = f'v_sh600000="{"~".join(fields)}";'

    result = TencentSource(http).quote([SYM])

    row = result.iloc[0]
    assert row["market"] == "SH"
    assert row["volume"] == 1200
    assert row["amount"] == 20000
    assert row["float_mv"] == 3e8
    assert row["ts"] == "2024-01-02 09:30:00"


def test_tencent_minute_parses_datetime_and_volume() -> None:
    http = Mock()
    http.get_json.return_value = {"data": {"sh600000": {"m5": [
        ["202401020935", 1, 2, 3, 0.5, 10]
    ]}}}
    result = TencentSource(http).minute(SYM, 5, 10)
    assert result.loc[0, "datetime"] == "2024-01-02 09:35"
    assert result.loc[0, "volume"] == 1000


def test_sina_quote_parses_change_and_units() -> None:
    fields = [""] * 33
    values = {0: "浦发银行", 1: "10.1", 2: "10", 3: "10.5", 4: "11", 5: "9.5",
              8: "1200", 9: "13000", 30: "2024-01-02", 31: "09:30:00"}
    for index, value in values.items():
        fields[index] = value
    http = Mock()
    http.get_text.return_value = f'var hq_str_sh600000="{",".join(fields)}";'

    result = SinaSource(http).quote([SYM])

    assert result.loc[0, "change"] == 0.5
    assert result.loc[0, "pct_chg"] == 5
    assert result.loc[0, "volume"] == 1200


def test_eastmoney_quote_scales_price_fields() -> None:
    http = Mock()
    http.get_json.return_value = {"data": {
        "f57": "600000", "f58": "浦发银行", "f43": 1050, "f44": 1100,
        "f45": 950, "f46": 1010, "f60": 1000, "f47": 12, "f48": 13000,
        "f168": 120, "f162": 550, "f167": 80, "f169": 50, "f170": 500,
        "f171": 700, "f116": 100, "f117": 80,
    }}
    result = EastmoneySource(http).quote([SYM])
    assert result.loc[0, "price"] == 10.5
    assert result.loc[0, "pct_chg"] == 5
    assert result.loc[0, "volume"] == 1200


def test_eastmoney_kline_and_minute_schema() -> None:
    http = Mock()
    http.get_json.return_value = {"data": {"klines": [
        "2024-01-01,1,2,3,0.5,10,1000,1,2,0.1,0.2",
        "2024-01-02,2,3,4,1.5,20,2000,1,2,0.1,0.2",
    ]}}
    source = EastmoneySource(http)
    daily = source.kline(SYM, "101", "1", None, None, 2)
    assert daily.columns.tolist() == ["date", "open", "close", "high", "low", "volume",
                                     "amount", "amplitude", "pct_chg", "change", "turnover"]
    assert daily["volume"].tolist() == [1000, 2000]

    minute = source.minute(SYM, 5, 1)
    assert len(minute) == 1
    assert minute.iloc[0]["datetime"] == "2024-01-02"


def test_eastmoney_stock_list_paginates_and_infers_bj() -> None:
    http = Mock()
    http.get_json.side_effect = [
        {"data": {"total": 2, "diff": [{"f12": "600000", "f14": "浦发银行", "f13": 1}]}},
        {"data": {"total": 2, "diff": [{"f12": "920001", "f14": "北交所样本", "f13": 0}]}},
    ]
    result = EastmoneySource(http).stock_list("all")
    assert http.get_json.call_count == 2
    assert result["market"].tolist() == ["SH", "BJ"]


def test_eastmoney_financial_summary_and_statement() -> None:
    http = Mock()
    http.get_json.return_value = {"success": True, "result": {"data": [{
        "REPORT_DATE": "2024-03-31 00:00:00", "REPORT_TYPE": "错误值",
        "EPSJB": "0.2", "BPS": "8", "TOTALOPERATEREVE": "100",
        "PARENTNETPROFIT": "10", "ROEJQ": "2.5", "TOTAL_OPERATE_INCOME": "100",
    }]}}
    source = EastmoneySource(http)
    summary = source.financial_summary(SYM, 1)
    assert summary.loc[0, "report_date"] == "2024-03-31"
    assert summary.loc[0, "net_profit"] == 10

    statement = source.financial_statement(SYM, "income", 1)
    assert statement.loc[0, "report_type"] == "一季报"
    assert statement.loc[0, "TOTAL_OPERATE_INCOME"] == "100"


def test_eastmoney_dividend_and_macro() -> None:
    http = Mock()
    http.get_json.return_value = {"success": True, "result": {"data": [{
        "REPORT_DATE": "2024-01-01", "NATIONAL_SAME": 0.3,
    }]}}
    source = EastmoneySource(http)
    assert len(source.dividend(SYM, 1)) == 1
    macro = source.macro("cpi", 1)
    assert macro.loc[0, "date"] == "2024-01-01"
    assert macro.loc[0, "NATIONAL_SAME"] == 0.3


def test_eastmoney_rejects_empty_or_unknown_reports() -> None:
    http = Mock()
    http.get_json.return_value = {"success": False, "message": "bad report"}
    source = EastmoneySource(http)
    with pytest.raises(NoDataError):
        source.macro("cpi")
    with pytest.raises(SourceError):
        source.macro("unknown")
    with pytest.raises(SourceError):
        source.financial_statement(SYM, "unknown")


def test_baostock_financial_summary_and_stock_list() -> None:
    source = BaostockSource(Mock())
    source._with_session = Mock(return_value=pd.DataFrame({
        "_y": [2023], "_q": [4], "epsTTM": [1.2], "roeAvg": [0.1],
        "netProfitMargin": [0.2], "gpMargin": [0.3],
    }))
    summary = source.financial_summary(SYM, 4)
    assert summary.loc[0, "report_type"] == "年报"
    assert summary.loc[0, "roe"] == 10

    source._with_session = Mock(return_value=pd.DataFrame({
        "code": ["sh.600000", "sz.000001", "xx.bad"],
        "code_name": ["浦发银行", "平安银行", "无效"],
    }))
    stocks = source.stock_list()
    assert stocks["market"].tolist() == ["SH", "SZ"]


def test_baostock_session_closes_and_suppresses_output(monkeypatch, capsys) -> None:
    calls = []
    fake = SimpleNamespace(
        login=lambda: (calls.append("login"), print("login success!"),
                       SimpleNamespace(error_code="0", error_msg=""))[-1],
        logout=lambda: (calls.append("logout"), print("logout"), None)[-1],
    )
    monkeypatch.setitem(__import__("sys").modules, "baostock", fake)
    source = BaostockSource(Mock())
    assert source._with_session(lambda bs: "ok") == "ok"
    assert calls == ["login", "logout"]
    assert capsys.readouterr().out == ""


def test_akshare_financial_summary_and_statement() -> None:
    source = AkshareSource(Mock())
    raw = pd.DataFrame({
        "选项": ["常用指标"] * 3 + ["成长能力"],
        "指标": ["基本每股收益", "每股净资产", "归母净利润", "营业总收入增长率"],
        "20240331": [0.2, 8, 10, 5],
    })
    ak = SimpleNamespace(stock_financial_abstract=lambda symbol: raw)
    source._ak = lambda: ak
    summary = source.financial_summary(SYM, 1)
    assert summary.loc[0, "report_date"] == "2024-03-31"
    assert summary.loc[0, "eps"] == 0.2

    statement_raw = pd.DataFrame({"报告日": ["20240331"], "营业收入": [100]})
    ak2 = SimpleNamespace(stock_financial_report_sina=lambda **kwargs: statement_raw)
    source._ak = lambda: ak2
    statement = source.financial_statement(SYM, "income", 1)
    assert statement.loc[0, "report_type"] == "一季报"


def test_akshare_macro_normalization_and_value() -> None:
    source = AkshareSource(Mock())
    raw = pd.DataFrame({"月份": ["2024年2月份", "2024年1月份"], "今值": ["2", "1"]})
    ak = SimpleNamespace(macro_china_cpi=lambda: raw)
    source._ak = lambda: ak

    result = source.macro("cpi", 2)

    assert result["date"].tolist() == ["2024-02-01", "2024-01-01"]
    assert result["value"].tolist() == [2, 1]


def test_normalize_ak_macro_handles_invalid_date() -> None:
    result = _normalize_ak_macro(pd.DataFrame({"季度": ["bad"]}))
    assert result["date"].isna().all()


def test_nbs_query_success_and_blocked_message() -> None:
    fin = CNFin(cache=False)
    fin.http.get_json = Mock(return_value={"returndata": {"datanodes": [{
        "code": "zb.A01", "data": {"data": 0.3, "strdata": "0.3", "dotcount": 1},
    }]}})
    result = fin.nbs_query(retries=0)
    assert result.loc[0, "value"] == 0.3

    fin.http.get_json = Mock(side_effect=SourceBlocked("HTTP 403"))
    with pytest.raises(SourceBlocked, match="东财镜像"):
        fin.nbs_query(retries=0)


def test_health_uses_real_source_boundaries_with_mocked_io() -> None:
    fin = CNFin(cache=False)
    for source in fin.sources.values():
        for method in ("quote", "kline", "minute", "stock_list", "financial_summary", "macro"):
            if hasattr(source, method):
                setattr(source, method, Mock(return_value=pd.DataFrame({"value": [1]})))
    result = fin.health(verbose=False)
    assert len(result) == 14
    assert result["ok"].all()


def test_akshare_external_runtime_error_is_wrapped_as_source_error() -> None:
    source = AkshareSource(Mock())
    ak = SimpleNamespace(stock_zh_a_hist=Mock(side_effect=RuntimeError("upstream bug")))
    source._ak = lambda: ak

    with pytest.raises(SourceError, match="akshare 历史行情.*失败"):
        source.kline(Symbol.parse("600000"), "day", "none", None, None, None)


def test_baostock_external_runtime_error_is_wrapped_as_source_error(monkeypatch) -> None:
    fake = SimpleNamespace(
        login=lambda: SimpleNamespace(error_code="0", error_msg=""),
        logout=lambda: None,
    )
    monkeypatch.setitem(__import__("sys").modules, "baostock", fake)
    source = BaostockSource(Mock())

    with pytest.raises(SourceError, match="baostock 查询失败"):
        source._with_session(lambda bs: (_ for _ in ()).throw(RuntimeError("socket closed")))
