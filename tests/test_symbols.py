import pytest

from cnfin import CNFin, Symbol


@pytest.mark.parametrize(
    ("raw", "code", "market"),
    [
        ("600000", "600000", "SH"),
        ("600000.SH", "600000", "SH"),
        ("600000.SS", "600000", "SH"),
        ("SH600000", "600000", "SH"),
        ("sh.600000", "600000", "SH"),
        ("sz000001", "000001", "SZ"),
        ("920001", "920001", "BJ"),
        ("830799.BJ", "830799", "BJ"),
        ("00700", "00700", "HK"),
    ],
)
def test_symbol_parse_formats(raw: str, code: str, market: str) -> None:
    symbol = Symbol.parse(raw)
    assert (symbol.code, symbol.market) == (code, market)
    assert symbol.raw == raw


def test_symbol_source_formats() -> None:
    symbol = Symbol.parse("600000.SH")
    assert symbol.tencent == "sh600000"
    assert symbol.sina == "sh600000"
    assert symbol.em_secid == "1.600000"
    assert symbol.baostock == "sh.600000"
    assert symbol.secucode == "600000.SH"
    assert str(symbol) == "600000.SH"


def test_bare_000001_is_stock_unless_index_is_explicit() -> None:
    assert Symbol.parse("000001").market == "SZ"
    assert Symbol.parse("000001", is_index=True).market == "SH"
    assert Symbol.parse("000001", is_index=True).is_index is True


@pytest.mark.parametrize("raw", ["", "abc", "60000A", "600000.XX", "1234"])
def test_symbol_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(ValueError):
        Symbol.parse(raw)


@pytest.mark.parametrize("statement", ["income", "balance", "cashflow"])
def test_statement_field_catalog(statement: str) -> None:
    fields = CNFin.statement_fields(statement)
    assert not fields.empty
    assert fields.columns.tolist() == ["field", "cn_name"]


def test_statement_field_catalog_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        CNFin.statement_fields("unknown")
