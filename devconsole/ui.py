"""Shared Streamlit widgets for the dev console pages."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import streamlit as st

from devconsole.lib.sources import (
    SYNTHETIC_LABEL,
    BarsResult,
    DataSource,
    load_bars,
    resolve_root,
)

_SOURCE_LABELS = {
    DataSource.SYNTHETIC: "Synthetic practice data",
    DataSource.LOCAL_CACHE: "My local cache (read-only)",
}


def explainer(text: str) -> None:
    """Plain-language intro shown at the top of a page."""
    st.markdown(text)


def source_sidebar() -> tuple[DataSource, Path | None]:
    """Sidebar data-source picker. Returns (source, cache_dir or None for synthetic)."""
    st.session_state.setdefault("data_source", DataSource.SYNTHETIC)
    source = st.sidebar.radio(
        "Data source",
        list(DataSource),
        format_func=_SOURCE_LABELS.__getitem__,
        key="data_source",
        help="Synthetic data is generated for tests. The local cache holds real downloaded prices.",
    )
    if source is not DataSource.LOCAL_CACHE:
        return source, None
    default = str(resolve_root(DataSource.LOCAL_CACHE))
    text = st.sidebar.text_input("Cache folder", value=default, key="cache_dir_text")
    return source, Path(text).expanduser()


def _cache_has_files(folder: Path) -> bool:
    """True when the cache folder exists and holds at least one entry. Never creates it."""
    try:
        return folder.is_dir() and any(folder.iterdir())
    except OSError:
        return False


def source_banner(source: DataSource, cache_dir: Path | None = None) -> None:
    """Banner stating which data is on screen."""
    if source is DataSource.SYNTHETIC:
        st.info(
            f"Showing **{SYNTHETIC_LABEL}**. Generated for tests; offline. "
            "Good for learning how things look."
        )
    else:
        shown = resolve_root(source, cache_dir=cache_dir)
        if not _cache_has_files(shown):
            st.warning(
                f"No local prices saved yet in `{shown}`. This page only reads saved prices; "
                "nothing is downloaded here."
            )
            return
        st.success(
            f"Showing your **real downloaded prices** from `{shown}`. "
            "Read-only; this app never downloads or changes them."
        )


def term(name: str) -> None:
    """Caption with the plain-English meaning of a finance term."""
    from devconsole.lib.rules_english import GLOSSARY  # lazy: built by a separate task

    text = GLOSSARY.get(name.lower())
    if text is not None:
        st.caption(f"**{name}**: {text}")


@st.cache_data(show_spinner=False, ttl=60)
def load_bars_cached(
    source: DataSource, root: Path, symbol: str, start: date, end: date
) -> BarsResult:
    """Cached ``load_bars``. ``root`` keys the cache and is also the folder read."""
    if source is DataSource.SYNTHETIC:
        return load_bars(source, symbol, start, end, fixtures_dir=root)
    return load_bars(source, symbol, start, end, cache_dir=root)
