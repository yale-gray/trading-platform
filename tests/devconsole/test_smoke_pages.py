"""Page smoke tests for the dev console (SPEC-006 T14, AC 12).

Each view is rendered through ``streamlit.testing.v1.AppTest`` with synthetic (fixture) data, so
nothing touches the network, the real cache or the repo. The Overview page's "Run tests" runner
and its git-history call are monkeypatched; the tests never click "Run tests".
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from devconsole.lib.sources import DataSource
from tradebot.strategy.schema import STRATEGIES

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_PATH = REPO_ROOT / "devconsole" / "app.py"
VIEW_MODULES = ("overview", "market_data", "strategies", "cost_sandbox", "quality")
TIMEOUT = 30
SYNTHETIC_BANNER = "not real prices"


def _page_script(module: str) -> str:
    """Script that puts the repo root on sys.path, imports one view and renders it."""
    return (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        f"from devconsole.views import {module}\n"
        f"{module}.render()\n"
    )


def _run_view(module: str, state: dict[str, object] | None = None) -> AppTest:
    at = AppTest.from_string(_page_script(module), default_timeout=TIMEOUT)
    for key, value in (state or {}).items():
        at.session_state[key] = value
    return at.run(timeout=TIMEOUT)


def _info_texts(at: AppTest) -> list[str]:
    return [element.value for element in at.info]


@pytest.fixture(autouse=True)
def _no_subprocess_or_test_run(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the test runner and git history with stubs. Records any test-run attempt."""
    from devconsole.lib import runners
    from devconsole.views import overview

    calls: list[str] = []

    def fake_run_tests(*args: object, **kwargs: object) -> None:
        calls.append("run_tests")
        return None

    def fake_recent_commits(*args: object, **kwargs: object) -> tuple[list[object], str | None]:
        return [], "git history is stubbed in tests"

    monkeypatch.setattr(runners, "run_tests", fake_run_tests)
    monkeypatch.setattr(overview, "run_tests", fake_run_tests)
    monkeypatch.setattr(overview, "recent_commits", fake_recent_commits)
    return calls


@pytest.mark.parametrize("module", VIEW_MODULES)
def test_view_renders_without_exception(module: str) -> None:
    at = _run_view(module)
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.title) == 1


@pytest.mark.parametrize("module", ("market_data", "strategies"))
def test_data_views_show_synthetic_banner_by_default(module: str) -> None:
    """PM decision: only the market data and strategies views must show the synthetic banner."""
    at = _run_view(module)
    assert not at.exception
    assert any(SYNTHETIC_BANNER in text for text in _info_texts(at))


@pytest.mark.timeout(90)
def test_strategies_view_renders_every_strategy() -> None:
    at = _run_view("strategies")
    assert not at.exception
    assert len(STRATEGIES) == 3
    for strategy_id, spec in STRATEGIES.items():
        at = at.selectbox(key="strategy_id").select(spec.name).run(timeout=TIMEOUT)
        assert not at.exception, (strategy_id, [e.value for e in at.exception])
        assert at.selectbox(key="strategy_id").value == strategy_id


def test_overview_has_run_tests_button_and_does_not_run_tests(_no_subprocess_or_test_run) -> None:
    at = _run_view("overview")
    assert not at.exception
    assert any(button.label == "Run tests" for button in at.button)
    assert _no_subprocess_or_test_run == []


def test_cost_sandbox_bad_quantity_shows_error_not_exception() -> None:
    at = _run_view("cost_sandbox")
    assert not at.exception
    assert len(at.error) == 0

    at.text_input(key="sandbox_qty").input("abc").run(timeout=TIMEOUT)
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.error) == 1
    assert "Check your inputs" in at.error[0].value


def test_market_data_local_cache_missing_dir_shows_info_and_creates_nothing(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "no-such-cache"
    at = _run_view(
        "market_data",
        state={"data_source": DataSource.LOCAL_CACHE, "cache_dir_text": str(missing)},
    )
    assert not at.exception, [e.value for e in at.exception]
    assert any("No local cache yet" in text for text in _info_texts(at))
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


def test_app_navigation_script_runs_without_exception() -> None:
    at = AppTest.from_file(str(APP_PATH), default_timeout=TIMEOUT).run(timeout=TIMEOUT)
    assert not at.exception, [e.value for e in at.exception]
    assert any(title.value == "Overview" for title in at.title)


@pytest.mark.parametrize("module", VIEW_MODULES)
def test_view_module_imports_and_exposes_render(module: str) -> None:
    """Import-level fallback check: every view module loads and exposes a callable render()."""
    mod = importlib.import_module(f"devconsole.views.{module}")
    assert callable(mod.render)
