"""Trading Bot dashboard (SPEC-004). Run: uv run streamlit run dashboard/app.py

Bot pages read only through the API (``dashboard/client.py``). The Developer section reuses
the dev console's ``render()`` functions from ``devconsole/views``.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st  # noqa: E402

from dashboard.views import backtests as bot_backtests  # noqa: E402
from dashboard.views import (  # noqa: E402
    portfolio_detail,
    portfolios,
    runner,
    section_crypto,
    section_daytrading,
    section_index,
    section_stocks,
)
from dashboard.views import strategies as bot_strategies  # noqa: E402
from devconsole.views import backtests as dev_backtests  # noqa: E402
from devconsole.views import cost_sandbox, market_data, overview, quality  # noqa: E402
from devconsole.views import strategies as dev_strategies  # noqa: E402

st.set_page_config(page_title="Trading Bot", layout="wide")

st.navigation(
    {
        "Bot": [
            st.Page(portfolios.render, title="Portfolios", url_path="portfolios", default=True),
            st.Page(portfolio_detail.render, title="Portfolio detail", url_path="portfolio-detail"),
            st.Page(bot_strategies.render, title="Strategies", url_path="strategies"),
            st.Page(bot_backtests.render, title="Backtests", url_path="backtests"),
            st.Page(runner.render, title="Runner status", url_path="runner"),
        ],
        "Sections": [
            st.Page(section_index.render, title="Index funds", url_path="index-funds"),
            st.Page(section_stocks.render, title="Stocks", url_path="stocks"),
            st.Page(section_crypto.render, title="Crypto", url_path="crypto"),
            st.Page(section_daytrading.render, title="Day trading", url_path="day-trading"),
        ],
        "Developer": [
            st.Page(overview.render, title="Overview", url_path="dev-overview"),
            st.Page(market_data.render, title="Market data", url_path="dev-market-data"),
            st.Page(dev_strategies.render, title="Strategy explainer", url_path="dev-strategies"),
            st.Page(dev_backtests.render, title="Dev backtests", url_path="dev-backtests"),
            st.Page(
                cost_sandbox.render, title="Money & costs sandbox", url_path="dev-cost-sandbox"
            ),
            st.Page(quality.render, title="Quality & reviews", url_path="dev-quality"),
        ],
    }
).run()
