"""Local dev console entry point. Run: uv run streamlit run devconsole/app.py"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st  # noqa: E402

from devconsole.views import (  # noqa: E402
    backtests,
    cost_sandbox,
    market_data,
    overview,
    quality,
    strategies,
)

st.set_page_config(page_title="Trading Bot dev console", layout="wide")

st.navigation(
    [
        st.Page(overview.render, title="Overview", url_path="overview", default=True),
        st.Page(market_data.render, title="Market data", url_path="market-data"),
        st.Page(strategies.render, title="Strategies", url_path="strategies"),
        st.Page(backtests.render, title="Backtests", url_path="backtests"),
        st.Page(cost_sandbox.render, title="Money & costs sandbox", url_path="cost-sandbox"),
        st.Page(quality.render, title="Quality & reviews", url_path="quality"),
    ]
).run()
