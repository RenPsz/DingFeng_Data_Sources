import importlib.util

import pandas as pd
import pytest

from cnfin import CNFin


pytestmark = [pytest.mark.integration, pytest.mark.network]


@pytest.fixture(scope="module")
def live_fin() -> CNFin:
    return CNFin(cache=False, timeout=8, retries=0, cooldown=30)


def assert_ohlcv(frame: pd.DataFrame) -> None:
    assert not frame.empty
    assert {"date", "open", "high", "low", "close", "volume"}.issubset(frame.columns)
    assert frame["date"].is_unique
    assert frame["date"].is_monotonic_increasing
    assert (frame["high"] >= frame["low"]).all()
    assert (frame["volume"] >= 0).all()


def test_live_tencent_quote(live_fin: CNFin) -> None:
    result = live_fin.quote("600000", prefer="tencent", use_cache=False)
    assert len(result) == 1
    assert result.loc[0, "code"] == "600000"
    assert result.loc[0, "market"] == "SH"
    assert pd.notna(result.loc[0, "price"])


def test_live_tencent_kline_contract(live_fin: CNFin) -> None:
    result = live_fin.kline("600000", limit=10, prefer="tencent", use_cache=False)
    assert_ohlcv(result)
    assert len(result) <= 10
    assert result.attrs["source"] == "tencent"


def test_live_eastmoney_financial_contract(live_fin: CNFin) -> None:
    result = live_fin.financial_summary("600000", periods=2, prefer="eastmoney")
    assert len(result) > 0
    assert result["report_date"].notna().all()
    assert result["report_type"].isin(["一季报", "中报", "三季报", "年报"]).all()


def test_live_eastmoney_macro_contract(live_fin: CNFin) -> None:
    result = live_fin.macro("cpi", limit=3, tidy=True, prefer="eastmoney")
    assert len(result) > 0
    assert result["value"].notna().any()
    assert result["indicator"].eq("cpi").all()


@pytest.mark.skipif(importlib.util.find_spec("baostock") is None, reason="未安装 baostock")
def test_live_baostock_latest_limit(live_fin: CNFin) -> None:
    result = live_fin.kline("600000", adjust="none", limit=5, prefer="baostock", use_cache=False)
    assert_ohlcv(result)
    assert len(result) <= 5
