from unittest.mock import Mock

import pandas as pd
import pytest

from cnfin import BaostockSource, CNFin, SinaSource, SourceError, Symbol, TencentSource


def test_tencent_hfq_missing_does_not_return_raw() -> None:
    http = Mock()
    http.get_json.return_value = {
        "data": {
            "sh600000": {
                "day": [["2024-01-02", "1", "2", "3", "0.5", "10"]],
            }
        }
    }

    with pytest.raises(SourceError, match="hfqday"):
        TencentSource(http).kline(Symbol.parse("600000"), "day", "hfq", None, None, 1)


def test_sina_rejects_adjusted_kline_instead_of_returning_raw() -> None:
    http = Mock()

    with pytest.raises(SourceError, match="不复权"):
        SinaSource(http).kline(Symbol.parse("600000"), "day", "qfq", None, None, 10)

    http.get_json.assert_not_called()


def test_baostock_limit_returns_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    source = BaostockSource(Mock())
    raw = pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-01-02", "2024-01-03"],
            "open": [1, 2, 3],
            "high": [1, 2, 3],
            "low": [1, 2, 3],
            "close": [1, 2, 3],
            "volume": [10, 20, 30],
            "amount": [100, 200, 300],
            "pctChg": [0, 1, 2],
            "turn": [0.1, 0.2, 0.3],
        }
    )
    monkeypatch.setattr(source, "_with_session", lambda fn: raw)

    result = source.kline(Symbol.parse("600000"), "day", "none", None, None, 2)

    assert result["date"].tolist() == ["2024-01-02", "2024-01-03"]


def test_is_trade_day_weekend_returns_false_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fin = CNFin(cache=False)
    monkeypatch.setattr(
        fin,
        "trade_days",
        lambda *args, **kwargs: pytest.fail("周末不应请求交易日历上游"),
    )

    assert fin.is_trade_day("2024-01-06") is False
