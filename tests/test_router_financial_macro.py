from unittest.mock import Mock

import pandas as pd
import pytest

from cnfin import CNFin, NoDataError, SourceError, _Cache, _Router


def test_router_falls_back_on_source_error() -> None:
    router = _Router(_Cache(enabled=False))
    good = pd.DataFrame({"close": [1]})

    result = router.run(
        "kline",
        [("bad", Mock(side_effect=SourceError("schema changed"))),
         ("good", Mock(return_value=good))],
    )

    assert result.attrs["source"] == "good"
    assert len(router.trace) == 2


def test_router_does_not_circuit_break_on_no_data() -> None:
    router = _Router(_Cache(enabled=False), max_fail=1)
    failing = Mock(side_effect=NoDataError("suspended"))
    chain = [("empty", failing), ("good", Mock(return_value=pd.DataFrame({"close": [1]})))]

    router.run("kline", chain)
    router.run("kline", chain)

    assert failing.call_count == 2
    assert router._blocked_until == {}


def test_router_trace_memory_is_bounded() -> None:
    router = _Router(_Cache(enabled=False))
    chain = [("good", Mock(return_value=pd.DataFrame({"close": [1]})))]

    for _ in range(250):
        router.run("kline", chain)

    assert len(router.trace) == 200
    assert len(router.trace[-40:]) == 40


def test_router_does_not_hide_programming_errors() -> None:
    router = _Router(_Cache(enabled=False))
    fallback = Mock(return_value=pd.DataFrame({"close": [1]}))

    with pytest.raises(RuntimeError, match="bug"):
        router.run("kline", [("bad", Mock(side_effect=RuntimeError("bug"))), ("good", fallback)])

    fallback.assert_not_called()


def test_router_schema_validation_failure_uses_next_source() -> None:
    router = _Router(_Cache(enabled=False))
    result = router.run(
        "kline",
        [("bad", Mock(return_value=pd.DataFrame({"wrong": [1]}))),
         ("good", Mock(return_value=pd.DataFrame({"close": [1]})))],
        validator=lambda frame: "close" in frame,
    )

    assert result.attrs["source"] == "good"


def test_financial_summary_has_stable_schema_and_report_types() -> None:
    fin = CNFin(cache=False)
    fin.sources["eastmoney"].financial_summary = Mock(return_value=pd.DataFrame(
        {"report_date": ["2024-03-31", "2023-12-31"], "eps": [0.2, 1.0]}
    ))

    result = fin.financial_summary("600000", periods=2)

    assert result["report_type"].tolist() == ["一季报", "年报"]
    for column in ("revenue", "net_profit", "roe", "debt_ratio"):
        assert column in result.columns
    assert result["report_date"].is_monotonic_decreasing


def test_financial_periods_validation_happens_before_network() -> None:
    fin = CNFin(cache=False)
    fin.router.run = Mock()
    with pytest.raises(ValueError):
        fin.financial_summary("600000", periods=0)
    with pytest.raises(ValueError):
        fin.financial_statement("600000", periods=-1)
    fin.router.run.assert_not_called()


def test_macro_tidy_uses_only_audited_value_column() -> None:
    fin = CNFin(cache=False)
    fin.sources["eastmoney"].macro = Mock(return_value=pd.DataFrame(
        {"date": ["2024-01-01"], "NATIONAL_SAME": [0.3], "UNRELATED": [999]}
    ))

    result = fin.macro("cpi", tidy=True)

    assert result.columns.tolist() == ["date", "period", "indicator", "value", "unit"]
    assert result["value"].tolist() == [0.3]


def test_macro_tidy_refuses_to_guess_arbitrary_numeric_column() -> None:
    fin = CNFin(cache=False)
    fin.sources["eastmoney"].macro = Mock(return_value=pd.DataFrame(
        {"date": ["2024-01-01"], "UNRELATED": [999]}
    ))

    with pytest.raises(SourceError, match="拒绝猜测"):
        fin.macro("cpi", tidy=True)


def test_macro_rejects_unknown_indicator_and_bad_limit() -> None:
    fin = CNFin(cache=False)
    with pytest.raises(ValueError):
        fin.macro("unknown")
    with pytest.raises(ValueError):
        fin.macro("cpi", limit=0)


def test_trade_days_validates_range() -> None:
    fin = CNFin(cache=False)
    with pytest.raises(ValueError):
        fin.trade_days("2024-02-01", "2024-01-01")
    with pytest.raises(ValueError):
        fin.is_trade_day("bad-date")
