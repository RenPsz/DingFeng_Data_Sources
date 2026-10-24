import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(ROOT / "cnfin.py"), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


@pytest.mark.e2e
def test_cli_catalog_runs_without_network() -> None:
    result = run_cli("catalog")

    assert result.returncode == 0, result.stderr
    assert "cpi" in result.stdout
    assert "social_financing" in result.stdout


@pytest.mark.e2e
def test_cli_import_and_public_offline_workflow() -> None:
    code = """
import cnfin
from cnfin import CNFin
fin = CNFin(cache=False)
assert cnfin.__version__
assert not fin.macro_catalog().empty
assert fin.is_trade_day('2024-01-06') is False
print('offline-e2e-ok', cnfin.__version__)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "offline-e2e-ok" in result.stdout


@pytest.mark.e2e
def test_cli_rejects_invalid_adjust() -> None:
    result = run_cli("kline", "600000", "--adjust", "bad")

    assert result.returncode == 2
    assert "invalid choice" in result.stderr
