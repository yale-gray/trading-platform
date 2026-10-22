"""Index funds section page."""

from __future__ import annotations

from dashboard.views.section_page import render_section

EXPLAINER = """
This section is the **simple core** of the portfolio. Its money sits in index funds, which are
funds that each own many companies at once, so one fund moves with a whole market.

- **SPY** holds 500 large US companies (the S&P 500 index).
- **QQQ** holds about 100 of the largest non-financial Nasdaq companies, mostly technology.

Only the sleeves that belong to index funds are shown here.
"""


def render() -> None:
    render_section("Index funds", EXPLAINER)
