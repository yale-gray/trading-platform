"""Runner status page: is the paper-trading runner alive, and how far has each portfolio got?"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import client
from dashboard.views import common

_EXPLAINER = """
The **runner** is the background process that advances the paper-trading portfolios one bar
(one trading day) at a time. It writes a **heartbeat** every five minutes. No recent heartbeat
means the runner is stopped. A **bar** is one day of prices. The last bar processed shows how
up to date each portfolio is.
"""


def render() -> None:
    st.title("Runner status")
    common.banner()
    common.explain(_EXPLAINER)
    status = common.fetch(client.runner_status)
    if status is None:
        return
    common.runner_badge()
    st.metric("Last heartbeat", common.fmt_ts(status["last_heartbeat"]))
    sleeves = status.get("sleeves", [])
    if not sleeves:
        st.info("No portfolios are registered with the runner yet.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Portfolio": s["key"],
                    "Last bar processed": common.fmt_ts(s["last_processed_bar"]),
                    "Status": s["last_status"],
                    "Last error": s["last_error"] or "—",
                }
                for s in sleeves
            ]
        ),
        hide_index=True,
    )
