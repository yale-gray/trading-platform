"""Smoke tests for the Backtests page (SPEC-006): AppTest with synthetic fixtures only.

The page starts with no result and renders without exceptions. The "Run backtest" button may be
clicked; it runs the real backtester on the committed fixtures (offline, a few seconds).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 60
SYNTHETIC_BANNER = "not real prices"

_SCRIPT = (
    "import sys\n"
    f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
    "from devconsole.views import backtests\n"
    "backtests.render()\n"
)


def _page() -> AppTest:
    return AppTest.from_string(_SCRIPT, default_timeout=TIMEOUT).run(timeout=TIMEOUT)


def test_backtests_page_renders_with_default_selections_and_no_exception() -> None:
    at = _page()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.title) == 1 and at.title[0].value == "Backtests"
    assert any(SYNTHETIC_BANNER in info.value for info in at.info)
    assert any(button.label == "Run backtest" for button in at.button)
    assert len(at.selectbox(key="backtest_choice").options) == 4  # full fund + 3 sleeves


@pytest.mark.timeout(120)
def test_run_backtest_button_renders_results_without_exception() -> None:
    at = _page()
    at.button(key="run_backtest").click().run(timeout=TIMEOUT)
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.error) == 0, [e.value for e in at.error]
    assert len(at.dataframe) == 2  # metrics table and finished-trades table
    assert len(at.get("plotly_chart")) == 2  # equity curve and drawdown
