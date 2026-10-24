from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from cnfin import (
    AkshareSource,
    AllSourcesFailed,
    CNFin,
    NoDataError,
    SourceError,
    Symbol,
    TencentSource,
)


def bars(dates: list[str], start_price: int = 1) -> pd.DataFrame:
    values = list(range(start_price, start_price + len(dates)))
    return pd.DataFrame(
        {
            "date": dates,
            "open": values,
            "high": [v + 1 for v in values],
            "low": [v - 1 for v in values],
            "close": values,
            "volume": [100] * len(values),
            "amount": [1000] * len(values),
        }
    )


def test_tencent_paginates_large_limit_without_start() -> None:
    http = Mock()
    pages = [
        pd.date_range("2023-06-17", periods=640).strftime("%Y-%m-%d").tolist(),
        pd.date_range("2021-09-24", periods=640).strftime("%Y-%m-%d").tolist(),
        pd.date_range("2020-01-01", periods=100).strftime("%Y-%m-%d").tolist(),
    ]

    def response(*args, **kwargs):
        page = pages[http.get_json.call_count - 1]
        return {"data": {"sh600000": {"day": [[d, 1, 2, 3, 0.5, 10] for d in page]}}}

    http.get_json.side_effect = response
    result = TencentSource(http).kline(Symbol.parse("600000"), "day", "none", None, None, 1300)

    assert http.get_json.call_count == 3
    assert len(result) == 1300
    assert result["date"].is_unique
    assert result["date"].is_monotonic_increasing
    assert result["date"].iloc[-1] == pages[0][-1]


def test_tencent_long_history_stops_at_requested_start() -> None:
    http = Mock()
    responses = [
        {"data": {"sh600000": {"qfqday": [
            [d, 1, 2, 3, 0.5, 10]
            for d in pd.date_range("2024-01-01", periods=640).strftime("%Y-%m-%d")
        ]}}},
        {"data": {"sh600000": {"qfqday": [["2023-12-29", 1, 2, 3, 0.5, 10]]}}},
    ]
    http.get_json.side_effect = responses

    result = TencentSource(http).kline(
        Symbol.parse("600000"), "day", "qfq", "2023-12-30", None, None
    )

    assert http.get_json.call_count == 2
    assert result["date"].iloc[0] == "2023-12-29"


def test_sina_filters_dates_and_takes_latest_limit() -> None:
    http = Mock()
    http.get_json.return_value = [
        {"day": f"2024-01-0{i}", "open": i, "high": i, "low": i, "close": i, "volume": 10}
        for i in range(1, 6)
    ]

    from cnfin import SinaSource

    result = SinaSource(http).kline(
        Symbol.parse("600000"), "day", "none", "2024-01-02", "2024-01-05", 2
    )

    assert result["date"].tolist() == ["2024-01-04", "2024-01-05"]


def test_akshare_preserves_frequency_and_normalizes_volume() -> None:
    source = AkshareSource(Mock())
    ak = Mock()
    ak.stock_zh_a_hist.return_value = pd.DataFrame(
        {
            "日期": ["2024-01-05", "2024-01-12", "2024-01-19"],
            "开盘": [1, 2, 3], "收盘": [1, 2, 3], "最高": [1, 2, 3],
            "最低": [1, 2, 3], "成交量": [10, 20, 30], "成交额": [100, 200, 300],
        }
    )
    source._ak = Mock(return_value=ak)

    result = source.kline(Symbol.parse("600000"), "week", "none", None, None, 2)

    assert ak.stock_zh_a_hist.call_args.kwargs["period"] == "weekly"
    assert result["date"].tolist() == ["2024-01-12", "2024-01-19"]
    assert result["volume"].tolist() == [2000, 3000]


def test_facade_sorts_filters_and_applies_limit_last() -> None:
    fin = CNFin(cache=False)
    dates = ["2024-01-03", "2024-01-01", "2024-01-05", "2024-01-02", "2024-01-04"]
    fin.sources["tencent"].kline = Mock(return_value=bars(dates))

    result = fin.kline("600000", start="2024-01-02", end="2024-01-05", limit=2)

    assert result["date"].tolist() == ["2024-01-04", "2024-01-05"]
    assert result.attrs["source"] == "tencent"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"period": "hour"},
        {"adjust": "forward"},
        {"limit": 0},
        {"limit": -1},
        {"limit": True},
        {"start": "not-a-date"},
        {"start": "2024-02-01", "end": "2024-01-01"},
        {"prefer": "unknown"},
    ],
)
def test_kline_rejects_invalid_arguments_before_routing(kwargs: dict) -> None:
    fin = CNFin(cache=False)
    fin.router.run = Mock()
    with pytest.raises(ValueError):
        fin.kline("600000", **kwargs)
    fin.router.run.assert_not_called()


def test_cache_key_separates_preferred_source(tmp_path: Path) -> None:
    fin = CNFin(cache_dir=tmp_path)
    fin.sources["tencent"].kline = Mock(return_value=bars(["2024-01-01"], 1))
    fin.sources["sina"].kline = Mock(return_value=bars(["2024-01-01"], 9))

    tencent = fin.kline("600000", adjust="none", prefer="tencent")
    sina = fin.kline("600000", adjust="none", prefer="sina")

    assert tencent["close"].iloc[0] == 1
    assert sina["close"].iloc[0] == 9
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_partial_batch_quote_falls_back_and_preserves_order() -> None:
    fin = CNFin(cache=False)
    partial = pd.DataFrame([{"code": "600000", "market": "SH", "price": 10}])
    complete = pd.DataFrame(
        [{"code": "000001", "market": "SZ", "price": 11},
         {"code": "600000", "market": "SH", "price": 10}]
    )
    fin.sources["tencent"].quote = Mock(return_value=partial)
    fin.sources["sina"].quote = Mock(return_value=complete)

    result = fin.quote(["600000", "000001"], use_cache=False)

    assert result["code"].tolist() == ["600000", "000001"]
    assert result.attrs["source"] == "sina"


def test_partial_batch_from_every_source_fails_explicitly() -> None:
    fin = CNFin(cache=False)
    for name in ("tencent", "sina", "eastmoney"):
        fin.sources[name].quote = Mock(
            return_value=pd.DataFrame([{"code": "600000", "market": "SH"}])
        )

    with pytest.raises(AllSourcesFailed):
        fin.quote(["600000", "000001"], use_cache=False)


def test_stock_list_market_filter_applies_to_fallback() -> None:
    fin = CNFin(cache=False)
    fin.sources["akshare"].stock_list = Mock(return_value=pd.DataFrame(
        [{"code": "600000", "name": "浦发银行", "market": "SH"},
         {"code": "000001", "name": "平安银行", "market": "SZ"}]
    ))

    result = fin.stock_list("sz")

    assert result["code"].tolist() == ["000001"]
    assert fin.sources["akshare"].stock_list.call_args.args == ("sz",)


def test_minute_and_stock_list_validate_arguments() -> None:
    fin = CNFin(cache=False)
    with pytest.raises(ValueError):
        fin.minute("600000", freq=2)
    with pytest.raises(ValueError):
        fin.minute("600000", limit=0)
    with pytest.raises(ValueError):
        fin.stock_list("hk")
