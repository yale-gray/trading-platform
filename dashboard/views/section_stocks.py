"""Stocks section page."""

from __future__ import annotations

from dashboard.views.common import SP500_NOTE
from dashboard.views.section_page import render_section

EXPLAINER = """
Once a month the stock strategy looks at S&P 500 companies and picks the ones that have been
strong recently (this habit is called **momentum**). It then holds those picks until the next
monthly review.

**Caveat:** the stock list is the S&P 500 as it is today. Companies that were dropped from
the index in the past are missing, so past results look better than they would have been.
"""


def render() -> None:
    render_section("Stocks", EXPLAINER, note=SP500_NOTE)
