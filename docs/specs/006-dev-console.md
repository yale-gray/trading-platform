# SPEC-006: Local dev console (read-only Streamlit viewer)

- **Lead:** platform-lead
- **Status:** draft
- **Roadmap item:** Mini-wave 2.5 (between Wave 2 and Wave 3). Not in the original roadmap; PM to add a line.
- **Related ADRs/specs:** SPEC-004 (Wave 5 `dashboard/`, reads through the API), SPEC-001 (data), SPEC-002 (strategy schema), SPEC-003 (costs/money), SPEC-005 (tests, audits). ADR-0001 D9 (synthetic fixtures). CLAUDE.md hard rules 1, 3, 5.

## Goal
Give the owner, who is new to investing, a local web page to look around the project between build waves: progress, test results, prices, the three strategies in plain English, how costs work, and the audit trail. It is a viewer over existing library code. It is not the Wave 5 dashboard and it contains no trading logic.

## Non-goals
- Signals, trades, backtests, equity curves, portfolios. Nothing exists to show yet (engine is Wave 3, backtester Wave 4). Pages say so.
- Writing anything: no cache writes, no fixture writes, no repo writes, no network, no orders. No API, no DB, no auth.
- Replacing `dashboard/` (Wave 5). Only designing so pages can move there.
- Editing strategies or docs from the UI.

## Design

### Run it
```bash
uv run streamlit run devconsole/app.py          # opens http://localhost:8501
```
`streamlit` and `plotly` are already in the default `dashboard` dependency group (`default-groups` in `pyproject.toml`, installed at 1.66.0 / 7.1.0), so `uv sync` suffices. **No new dependency, no `pyproject.toml` edit.** Preview entry for the PM (`.claude/launch.json`, new file; the file does not exist today):
```json
{ "version": "0.0.1", "configurations": [ { "name": "devconsole", "runtimeExecutable": "uv",
  "runtimeArgs": ["run", "streamlit", "run", "devconsole/app.py", "--server.port", "8501", "--server.headless", "true"],
  "port": 8501 } ] }
```

### Layout
```
devconsole/
  app.py            # entry: sys.path bootstrap, st.set_page_config, st.navigation([...]), sidebar source picker
  ui.py             # shared widgets: explainer(), source_sidebar(), source_banner(), term() tooltips
  lib/              # pure functions, NO streamlit import (unit-testable)
    sources.py roadmap.py reviews.py rules_english.py sandbox.py charts.py runners.py
  views/            # one module per page, each exposes render() -> None
    overview.py market_data.py strategies.py cost_sandbox.py quality.py
tests/devconsole/   # conftest.py, test_*.py
```
- `views/` (not Streamlit's magic `pages/` dir) so `st.navigation` fully controls order and titles, and each page is a plain `render()` function. Moving a page to `dashboard/` later means copying the file and swapping its `lib` data calls for API client calls; widgets stay.
- `app.py` is run with `devconsole/` as the script dir, not the repo root, and `devconsole` is not an installed package. `app.py` therefore starts with `sys.path.insert(0, <repo root>)` (with `# noqa: E402` on following imports), and `tests/devconsole/conftest.py` does the same. Do not touch `pyproject.toml` for this.
- `lib/` imports only stdlib, pandas, plotly and `tradebot.*`. `views/` imports `streamlit` and `lib`. Pages never compute indicators, fills or P&L themselves: they call `tradebot.strategy.indicators.INDICATORS`, `tradebot.sim.costs` and `tradebot.portfolio.money` and plot the result.
- No wall-clock reads in `devconsole/` (SPEC-005 guard spirit). Default date ranges derive from the loaded data's own last date. Where a `now` is unavoidable (`validate_bars`), use a constant for synthetic data and `tradebot.clock.SystemClock().now()` for the real cache.

### Data source (visible and safe)
Sidebar radio, default **Synthetic practice data**, optional **My local cache (read-only)**. A banner on every page, rendered by `ui.source_banner()`:
- Synthetic: blue `st.info`: "Showing **synthetic practice data, not real prices**. Generated for tests; offline. Good for learning how things look."
- Local cache: green `st.success`: "Showing your **real downloaded prices** from `<cache_dir>`. Read-only; this app never downloads or changes them."

Read paths (both read-only):
- Synthetic: `FixtureProvider(tests/fixtures/market).fetch_bars(...)` directly, then `validate_bars` in memory. No `MarketData` facade, because its refresh path writes a cache.
- Local cache: `MarketData(DataConfig(cache_dir=<dir>, offline=True)).get_bars(symbol, start, end, refresh=False)`. `<dir>` = `DataConfig().cache_dir` (honours `TRADEBOT_CACHE_DIR`, default `~/.tradebot/cache`). If the directory does not exist, show "no local cache yet; run a data refresh later" and do not create it. A symbol with no cached bars shows an empty-state message, never an error trace.
- Data errors (`DataError` subclasses) are caught in `lib/sources.py` and returned as a message string for `st.warning`.

### Interfaces
```python
# devconsole/lib/sources.py
class DataSource(StrEnum): SYNTHETIC = "synthetic"; LOCAL_CACHE = "local_cache"
SYNTHETIC_LABEL = "synthetic practice data, not real prices"
FIXTURES_DIR: Path            # <repo>/tests/fixtures/market, resolved from __file__
FULL_START, FULL_END = date(2000, 1, 3), date(2035, 12, 31)   # within the XNYS calendar bounds
@dataclass(frozen=True)
class BarsResult:
    source: DataSource; root: Path; symbol: str
    bars: pd.DataFrame            # BAR_COLUMNS, UTC DatetimeIndex; empty if none
    report: QualityReport | None  # None only when there is nothing to validate
    message: str | None           # human-readable problem/empty-state, else None
def resolve_root(source: DataSource, *, fixtures_dir: Path = FIXTURES_DIR,
                 cache_dir: Path | None = None) -> Path: ...        # pure; never creates dirs
def load_bars(source: DataSource, symbol: str, start: date, end: date, *,
              fixtures_dir: Path = FIXTURES_DIR, cache_dir: Path | None = None) -> BarsResult: ...
def data_extent(source: DataSource, symbol: str, **kw) -> tuple[date, date] | None: ...  # first/last bar
def universe() -> list[str]: ...                                    # list(DEFAULT_UNIVERSE), 20 symbols

# devconsole/lib/roadmap.py
@dataclass(frozen=True)
class RoadmapSection:
    title: str; status_hint: str   # heading text; "" or the parenthetical e.g. "done 2026-10-20", "next"
    done: int; in_progress: int; todo: int
    items: tuple[tuple[str, str], ...]   # (state "x"|"~"|" ", text)
    @property
    def total(self) -> int: ...
    @property
    def fraction(self) -> float: ...     # done/total, 0.0 when total == 0
def parse_roadmap(text: str) -> list[RoadmapSection]: ...   # every "## " heading with >= 1 checkbox, in order

# devconsole/lib/reviews.py
@dataclass(frozen=True)
class ReviewSummary:
    file: str; title: str; scope: str; date: str   # ISO string or ""
    initial_verdict: str; latest_verdict: str      # "PASS" | "PASS-WITH-NOTES" | "FAIL" | "UNKNOWN"
    reaudited: bool
def parse_review(file: str, text: str) -> ReviewSummary: ...
def parse_process_log(text: str) -> pd.DataFrame: ...       # the markdown table -> columns from its header row
def list_reviews(reviews_dir: Path) -> list[ReviewSummary]: ...  # skips _TEMPLATE.md, newest date first

# devconsole/lib/rules_english.py
@dataclass(frozen=True)
class RuleText:
    summary: str                    # one sentence, e.g. "Buys SPY or QQQ when ..."
    entry: list[str]                # bullet lines
    exit: list[str]
    stops: list[str]
    parameters: dict[str, str]      # name -> "value (what it does)" using GLOSSARY where known
    warmup: str                     # "needs 201 days of history before it can act"
    signals_note: str = "Signals appear after Wave 3."
GLOSSARY: dict[str, str]            # indicator/term -> one-line plain explanation (rsi, sma, atr, ret, vol, macd, bbands, ema, "trailing stop", ...)
def describe_strategy(spec: StrategySpec) -> RuleText: ...  # resolves $params, walks Condition/Operand models (not YAML text); handles rules + rotation kinds
def describe_condition(cond: Condition) -> str: ...
def describe_operand(op: Operand) -> str: ...             # {ind: rsi, period: 2} -> "2-day RSI"; {ind: sma, period: 200} -> "200-day average price"
def indicators_used(spec: StrategySpec) -> list[tuple[str, dict[str, float]]]: ...  # (indicator name, resolved params), deduped, for charting
# `StrategySpec` = the union type alias exported by tradebot.strategy.schema (RulesSpec | RotationSpec); worker confirms the real name.

# devconsole/lib/sandbox.py
@dataclass(frozen=True)
class TradeBreakdown:
    asset_class: AssetClass; qty: Decimal; fee_bps: Decimal; slip_bps: Decimal
    buy_open: Decimal; buy_fill: Decimal; buy_notional: Decimal; buy_fee: Decimal; buy_cash_out: Decimal
    sell_open: Decimal; sell_fill: Decimal; sell_notional: Decimal; sell_fee: Decimal; sell_cash_in: Decimal
    round_trip_pnl: Decimal         # sell_cash_in - buy_cash_out
    round_trip_pnl_pct: Decimal     # pnl / buy_cash_out, quantized with money.q_ratio
    total_costs: Decimal            # both fees + both slippage_cost values
def parse_decimal(text: str) -> Decimal: ...   # strips "$" and ","; rejects NaN/inf/<=0 with ValueError; never goes through float
def compute_trade(asset_class: AssetClass, qty: Decimal, buy_open: Decimal, sell_open: Decimal,
                  config: EngineConfig | None = None) -> TradeBreakdown: ...
# uses default_config().costs[asset_class]; money.buy_qty(qty, asset_class) for lot rules (whole shares for equity,
# 8 dp for crypto); costs.buy_fill_price/sell_fill_price/notional/fee/buy_cash_out/sell_cash_in/slippage_cost.
# MoneyError and ValueError propagate; the view shows them with st.error. No ledger, no portfolio object.

# devconsole/lib/charts.py   (plotly figure builders; take DataFrames, return go.Figure; no streamlit)
def candlestick_with_volume(bars: pd.DataFrame, symbol: str, overlays: Mapping[str, pd.Series]) -> go.Figure: ...
def indicator_subplot(series: Mapping[str, pd.Series], title: str, hlines: Sequence[float] = ()) -> go.Figure: ...
def compute_overlays(bars: pd.DataFrame, picks: Sequence[tuple[str, dict[str, float]]]) -> dict[str, pd.Series]: ...
# compute_overlays calls INDICATORS[name] (sma/ema on close, rsi on close, atr on high/low/close, ...) and labels
# each series e.g. "SMA 200". DataFrame-valued indicators (macd, bbands) are split into one series per column.
# Overlay kinds: "price" (sma, ema, bbands) draw on the candles; "oscillator" (rsi, atr, macd, vol, ret) get their own subplot.

# devconsole/lib/runners.py   (the only subprocess users; read-only commands)
@dataclass(frozen=True)
class TestRun:
    passed: int; failed: int; skipped: int; errors: int; duration_s: float
    failures: tuple[tuple[str, str], ...]     # (test id, short message)
    finished_note: str                        # e.g. "ok" | "pytest crashed: <stderr tail>" | "timed out"
def parse_junit(xml_text: str) -> TestRun: ...                       # pure
def run_tests(repo_root: Path, timeout_s: int = 600) -> TestRun | None: ...
#   Runs ["uv","run","pytest","-q",f"--junitxml={tmp}"] with cwd=repo_root, tmp = tempfile.mkdtemp() OUTSIDE the repo,
#   removed afterwards. Guarded by a module-level threading.Lock acquired non-blocking: returns None if a run is
#   already in progress (no double start, across browser sessions).
@dataclass(frozen=True)
class Commit: sha: str; author: str; date: str; subject: str
def parse_git_log(raw: str) -> list[Commit]: ...                     # pure; fields separated by \x1f
def recent_commits(repo_root: Path, n: int = 10) -> tuple[list[Commit], str | None]: ...
#   subprocess ["git","log","-n",str(n),"--date=short","--pretty=format:%h%x1f%an%x1f%ad%x1f%s"], list args, no shell;
#   returns ([], "git history unavailable: <reason>") when git is missing or this is not a repo.
```

### Pages
Every page starts with `ui.explainer(...)` (1–3 plain sentences) and the source banner. Finance terms use `help=` tooltips on widgets or an `st.caption` glossary line via `ui.term(name)` (backed by `rules_english.GLOSSARY`).

1. **Overview**
   - Explainer: "This is the project's home screen: how far along the build is, whether the automatic checks pass, and what changed recently."
   - Progress: `parse_roadmap(docs/ROADMAP.md)`; one `st.progress(fraction)` per section with "done/total" and its `status_hint`. Waves first, then phases.
   - Tests: **Run tests** button calls `runners.run_tests`. Result stored in `st.session_state["test_run"]` and shown as passed/failed/skipped metrics plus an expander of failures. Button is disabled while `st.session_state["tests_running"]` is true; a `None` return shows "A test run is already in progress". Before any run: "No results yet; the suite takes about a minute" (no auto-run on load). Writes only to the temp dir outside the repo. Note on page: "Tests use fake data and never touch the internet."
   - Recent commits: table from `recent_commits(repo_root, 10)`; unavailable message if `git` fails.
   - Links to key docs (relative paths shown as code, plus `st.link_button` is not used because files are local): STRATEGY, ROADMAP, ADR-0001, specs 001–006, process log. Each has a "view here" expander rendering the markdown (read-only `Path.read_text`).
2. **Market data**
   - Explainer: "A candlestick shows one day of trading: where the price opened, how high and low it went, and where it closed. The bars underneath are how many shares or coins traded."
   - Controls: symbol selectbox (`universe()`), date range (defaults to the last ~2 years of the loaded data; limits = `data_extent`), overlay multiselect built from `INDICATORS` with presets (SMA 50, SMA 200, EMA 20, Bollinger; RSI 2 / RSI 14, ATR 14 in a subplot), numeric period inputs.
   - Output: `candlestick_with_volume`, optional `indicator_subplot`. Data-quality panel from `BarsResult.report`: rows, first/last date, gaps, duplicates dropped, outliers, warnings list (empty = "no problems found"). Calendar note: equities and ETFs follow the NYSE schedule (weekdays, no market holidays); BTC/ETH/SOL trade every day including weekends. Caption on adjusted prices: "prices are adjusted for splits and dividends so long-run charts are comparable."
3. **Strategies**
   - Explainer: "A strategy is a written set of rules for when to buy and sell. These three are the starting sleeves from the strategy document. Nothing here is buying or selling: signals appear after Wave 3."
   - For each of `STRATEGIES` (tabs or expanders): name/description, `st.code(dump_strategy(spec), language="yaml")`, `describe_strategy(spec)` summary and bullets, parameters table, warmup, stops, universe, `required_symbols()`. Chart: one symbol selectbox (restricted to the strategy's universe), plots `indicators_used(spec)` via `compute_overlays` + chart builders.
   - No evaluation, no entry/exit markers. A visible `st.info("signals appear after Wave 3")`.
4. **Money & costs sandbox**
   - Explainer: "Every simulated trade pays a small fee and gets a slightly worse price than the quote (slippage). Try a buy and a sell to see exactly where the money goes. This is a calculator: nothing is saved or traded."
   - Inputs: asset type (Stock/ETF or Crypto), quantity, buy open price, sell open price (all `text_input` parsed by `parse_decimal` so no float rounding). Shows the current costs from `default_config()` (fee bps, slippage bps) with tooltips ("1 bp = 0.01%").
   - Output: a two-column buy/sell table of fill price, notional, fee, cash out/in, then round-trip P&L, P&L %, total costs. Caption: "Starting prices are the day's open because the simulator fills at the next bar's open (no peeking ahead)." Errors via `st.error`.
5. **Quality & reviews**
   - Explainer: "Independent audits check the code for mistakes before we build on it. PASS-WITH-NOTES means safe to continue, with a follow-up list."
   - Table from `list_reviews(docs/reviews)`: file, scope, date, initial verdict, latest verdict (re-audit), with a coloured verdict badge. `st.selectbox` of reports then `st.markdown(Path.read_text)` viewer. Process log: `st.dataframe(parse_process_log(docs/process-log.md))`, with the line "Rework = how many times a task was sent back."

### Invariants / rules
- The app opens no file for writing in the repo or in the cache; the only write is the pytest junit temp file under `tempfile.mkdtemp()` outside the repo, deleted after use.
- No `requests`/network use; `MarketData` is only ever constructed with `offline=True`.
- No Decimal-to-float conversion on the sandbox page.
- Every page shows the source banner; synthetic data is never shown without the "not real prices" label (including on the Strategies chart).
- Views contain no arithmetic on prices or money beyond formatting.
- Subprocess calls use list arguments, fixed commands, `cwd=repo_root`, no `shell=True`, and a timeout.

### How it grows
- After Wave 3: a Trades/Signals page showing `evaluate` output for a chosen strategy and symbol (library call, plotted as markers on the Strategies chart).
- After Wave 4: a Backtests page (equity curve vs SPY/BTC buy-and-hold, metrics cards) calling the backtester.
- Wave 5: copy `views/*.py` into `dashboard/pages`, replace `lib.sources`/direct library calls with API client calls (SPEC-004: dashboard is an HTTP client of the API only), keep `lib/charts.py`, `rules_english.py`, `roadmap.py` as shared presentation helpers. `devconsole/` is then either deleted or kept as the offline developer view. PM decides.

## Acceptance criteria
1. `uv run streamlit run devconsole/app.py` starts and all five pages render with the synthetic source without exceptions.
2. A source banner appears on every page; synthetic text contains "synthetic practice data, not real prices".
3. Market data: all 20 symbols load from fixtures; a candlestick+volume chart renders; SMA 50/200, RSI 2 and ATR overlays use `INDICATORS` outputs exactly; the quality panel shows rows, first/last date and `validate_bars` warnings.
4. `load_bars(LOCAL_CACHE, ...)` with a missing cache dir returns an empty result with a message, creates no directory, and makes no network call. With a populated temp cache it returns the bars.
5. Fixture tree and cache dir are byte-identical (file list, sizes, mtimes) before and after loading bars and running quality checks.
6. `parse_roadmap` on the current `docs/ROADMAP.md` yields Wave 1 and Wave 2 at 100%, Wave 3 at 0% with its `next` hint; counts match a hand count of the sample text in tests.
7. `parse_review`: an audit with an original `PASS-WITH-NOTES` header and a final `**Verdict: PASS.**` re-audit section yields initial `PASS-WITH-NOTES`, latest `PASS`, `reaudited=True`; a file with no re-audit has `latest == initial`; no verdict gives `UNKNOWN`. `_TEMPLATE.md` is skipped.
8. `describe_strategy` on `mean_reversion` includes "2-day RSI", "below 10", "200-day" and "SPY" and "QQQ", never prints `$rsi_len`-style raw references or YAML punctuation, and produces non-empty text for all three built-ins (rules and rotation kinds) plus every comparison and boolean node type.
9. `compute_trade` matches a hand calculation computed from `default_config()` values for one equity and one crypto case: buy fill = `ceil8(open*(1+slip/10000))`, fee = `q2_half_up(notional*fee_bps/10000)`, `round_trip_pnl == sell_cash_in - buy_cash_out`. Bad input (zero, negative, NaN, fractional equity shares) raises and does not return a result.
10. `run_tests` returns `None` when another run holds the lock (tested with a pre-acquired lock and a monkeypatched subprocess); `parse_junit` counts pass/fail/skip/error from a sample XML and extracts failure ids and messages. No test executes the real pytest or git.
11. `parse_git_log` handles empty output, unicode subjects and subjects containing `|`; `recent_commits` returns the unavailable message (no raise) when git fails.
12. Smoke test imports every `devconsole.views.*` module and `devconsole.app`'s page list without starting a server, and renders each page via `AppTest` (see T14). `uv run ruff check .` is clean; all new tests run offline and are marked `unit`.

## Tasks
Mini-wave 2.5. Fan-out: T1 first; then T2–T6 in parallel; tests and pages after their libs. Max parallelism 6 at a time. Tests listed separately per CLAUDE.md routing. Every worker may only touch the listed files. Run `uv run ruff check .` and `uv run pytest tests/devconsole -q` before reporting.

### T1 [haiku] Scaffold, navigation, launch entry
- **Files:** `devconsole/__init__.py`, `devconsole/lib/__init__.py`, `devconsole/views/__init__.py`, `devconsole/app.py`, `devconsole/views/overview.py`, `devconsole/views/market_data.py`, `devconsole/views/strategies.py`, `devconsole/views/cost_sandbox.py`, `devconsole/views/quality.py`, `.claude/launch.json`, `tests/devconsole/conftest.py`
- **Depends on:** none
- **Do:** Create the layout above. `app.py`: `sys.path` bootstrap to the repo root, `st.set_page_config(page_title="Trading Bot dev console", layout="wide")`, `st.navigation([st.Page(overview.render, title="Overview", url_path="overview", default=True), ...])`, call `.run()`. Each view stub defines `render()` that calls `st.title("<Page name>")` and `st.info("Coming in this mini-wave")` only. `tests/devconsole/conftest.py` inserts the repo root on `sys.path` (no other fixtures). Write the `launch.json` exactly as in Design.
- **Done when:** `uv run python -c "import devconsole.app"` works from the repo root (import has no side effects beyond defining pages) and ruff is clean.

### T2 [sonnet] Data source library and shared UI
- **Files:** `devconsole/lib/sources.py`, `devconsole/ui.py`
- **Depends on:** T1
- **Do:** Implement `sources.py` per the interfaces (fixtures through `FixtureProvider`, cache through `MarketData(offline=True)`, `validate_bars` for the report, catch `DataError`, never create dirs, canonical symbols via `normalize_symbol`). If `FixtureProvider`/`BarCache.read` turns out to write anything (lock files etc.), stop and report instead of working around it. `ui.py` (imports streamlit): `explainer(text)`, `source_sidebar() -> tuple[DataSource, Path | None]` (radio stored in `st.session_state["data_source"]`, default synthetic, optional cache-path text defaulting to `DataConfig().cache_dir`), `source_banner(source, cache_dir)` with the exact texts in Design, `term(name)` caption/tooltip helper reading `rules_english.GLOSSARY` (import lazily so T2 does not block on T3), `load_bars_cached` as `st.cache_data` wrapper keyed on (source, root, symbol, start, end).
- **Done when:** AC 2, 4, 5 (library part).

### T3 [sonnet] Rule-to-English renderer
- **Files:** `devconsole/lib/rules_english.py`
- **Depends on:** T1
- **Do:** Implement `describe_*`, `indicators_used`, `GLOSSARY` per the interfaces. Walk the pydantic models in `tradebot.strategy.schema` (use `spec.resolve()` for parameter values; show a parameter's name in the parameters table, not in the sentence). Cover all `Condition` node types (gt, ge, lt, le, eq, crosses_above, crosses_below, all, any, not, always), all operand types (price, indicator with `of`/`skip`/`fast`/`slow`/`signal`/`k`/`field`, pos.bars_held, literals), stops (ATR trailing: "sells if price falls N x its 14-day average daily range below the highest close since buying"), and rotation (rank, top k, eligibility, fallback). Plain wording: "2-day RSI is below 10", "price is above its 200-day average".
- **Done when:** AC 8.

### T4 [sonnet] Cost sandbox calculator
- **Files:** `devconsole/lib/sandbox.py`
- **Depends on:** T1
- **Do:** Implement per the interface, calling only `tradebot.sim.costs`, `tradebot.portfolio.money` and `default_config()`. Decimal only; no float conversion anywhere. `slippage_cost` is informational (included in `total_costs`, not subtracted twice from P&L, since slippage is already inside the fill prices; document this in the docstring).
- **Done when:** AC 9.

### T5 [haiku] Roadmap and review/process-log parsers
- **Files:** `devconsole/lib/roadmap.py`, `devconsole/lib/reviews.py`
- **Depends on:** T1
- **Do:** Implement per the interfaces. Roadmap: `## ` headings, checkbox lines `- [x]`/`- [~]`/`- [ ]` (also tolerate `* `), text may contain `[ ]` mid-line which must not count; one checkbox per line only. Parenthetical in heading becomes `status_hint`. Review: header lines `- **Date:**`, `- **Code:**` (scope; fall back to the H1 title), `- **Verdict:**`; the latest verdict is the last `**Verdict: X**`/`**Verdict: X.**` occurrence in the file; `reaudited` is true when it differs in position from the header one. Process log: parse the first markdown table with a header row and `---|` separator, trim cells, ignore escaped pipes.
- **Done when:** AC 6, 7 (library part).

### T6 [haiku] Chart builders
- **Files:** `devconsole/lib/charts.py`
- **Depends on:** T1
- **Do:** Implement per the interfaces with `plotly.graph_objects` and `plotly.subplots.make_subplots` (candles row 1 at 70% height, volume row 2; optional separate oscillator figure). Use `INDICATORS` for every indicator value; no hand-rolled maths. Hide weekend gaps for equities via `rangebreaks` only when the symbol has no `/`. Figure colours: up green, down red, theme-neutral.
- **Done when:** AC 3 (library part): returns figures whose traces match `INDICATORS` outputs.

### T7 [haiku] Runners library (git, tests)
- **Files:** `devconsole/lib/runners.py`
- **Depends on:** T1
- **Do:** Implement per the interfaces. `parse_junit` uses `xml.etree.ElementTree` on `<testsuite>`/`<testcase>` (handles `<failure>`, `<error>`, `<skipped>`, root `<testsuites>`). `run_tests`: lock non-blocking, `tempfile.mkdtemp()`, always remove in `finally`, handle `FileNotFoundError` (uv missing), `TimeoutExpired`, and a missing/invalid XML file. Use `subprocess.run(list, cwd=..., capture_output=True, text=True, timeout=...)`. Note this is escalated to sonnet if it fails review once (subprocess + shared lock).
- **Done when:** AC 10, 11 (library part).

### T8 [haiku] Unit tests for the libraries
- **Files:** `tests/devconsole/test_sources.py`, `tests/devconsole/test_roadmap.py`, `tests/devconsole/test_reviews.py`, `tests/devconsole/test_rules_english.py`, `tests/devconsole/test_sandbox.py`, `tests/devconsole/test_runners.py`, `tests/devconsole/test_charts.py`
- **Depends on:** T2, T3, T4, T5, T6, T7
- **Do:** `@pytest.mark.unit`, offline, inline text samples (do not depend on the live `ROADMAP.md` wording except one loose test: parse the real file, assert at least two sections and no negative counts). Sources tests use `tests/fixtures/market` read-only plus `tmp_path` caches; assert AC 4/5 (snapshot file list, sizes, mtimes before and after). Sandbox expected values hand-computed from `default_config()` in the test (derive bps from the config, then compute with Decimal in the test). Mock `subprocess.run`; never run real pytest/git. Rules tests use the three real strategies plus small hand-built specs for each node type. Charts: assert trace counts and that overlay y-values equal `INDICATORS` output on a fixture symbol.
- **Done when:** AC 4–11 pass offline in under the 20 s per-test timeout.

### T9 [sonnet] Overview page
- **Files:** `devconsole/views/overview.py`
- **Depends on:** T2, T5, T7
- **Do:** Implement page 1 as specified, including the no-double-start session-state handling, failure expander and doc viewers. Repo root resolved from `__file__`.
- **Done when:** AC 1 (page), 10.

### T10 [haiku] Market data page
- **Files:** `devconsole/views/market_data.py`
- **Depends on:** T2, T6
- **Do:** Implement page 2 as specified; calls only `ui`, `sources`, `charts`. Empty states for no data / quality failure.
- **Done when:** AC 1, 3.

### T11 [haiku] Strategies page
- **Files:** `devconsole/views/strategies.py`
- **Depends on:** T2, T3, T6
- **Do:** Implement page 3 as specified. Load strategies from `tradebot.strategy.schema.STRATEGIES` and format with `dump_strategy`. No signal evaluation.
- **Done when:** AC 1, 8 (page shows output).

### T12 [haiku] Cost sandbox page
- **Files:** `devconsole/views/cost_sandbox.py`
- **Depends on:** T4
- **Do:** Implement page 4 as specified; `ui.source_banner` still shown (this page uses no market data; it says "Prices here are numbers you type").
- **Done when:** AC 1, 9 (page).

### T13 [haiku] Quality & reviews page
- **Files:** `devconsole/views/quality.py`
- **Depends on:** T2, T5
- **Do:** Implement page 5 as specified; handle a missing `docs/reviews` or process log gracefully.
- **Done when:** AC 1, 7 (page).

### T14 [haiku] Page smoke test
- **Files:** `tests/devconsole/test_smoke_pages.py`
- **Depends on:** T9–T13
- **Do:** Installed Streamlit is 1.66.0 and `from streamlit.testing.v1 import AppTest` imports. For each view module: `AppTest.from_function(lambda: render())` style wrapper (import `render` inside the function body, as `AppTest.from_function` re-executes the source), `.run(timeout=30)`, assert `not at.exception`, and assert a banner text containing "not real prices" is present in the default (synthetic) state. Also `AppTest.from_file("devconsole/app.py").run()` as a navigation check; if `st.navigation` is not supported under AppTest at runtime, drop that one check and keep the per-view runs. Fallback if AppTest is unusable at all: `importlib.import_module` every `devconsole.views.*` and assert it exposes a callable `render`. The tests must never click "Run tests". Marked `unit`; uses a monkeypatched `runners.run_tests`/`recent_commits` so nothing spawns a subprocess.
- **Done when:** AC 12.

## Open questions
- For PM: add "Mini-wave 2.5" to `docs/ROADMAP.md`, and a `.claude/launch.json` is a new file (the PM owns `.claude/`; task T1 creates it only if the PM approves).
- For PM: `uv run ruff check .` will now lint `devconsole/` too (it is outside `docs`). Acceptable, and `pyproject.toml` needs no change. Coverage `source = ["tradebot"]` excludes `devconsole/` (intended; it is presentation code).
- `StrategySpec` union alias name in `tradebot.strategy.schema` must be confirmed by the T3 worker (module exposes `RulesSpec`/`RotationSpec`).
- Whether the local-cache toggle should also offer a cache-path field (default yes: `DataConfig().cache_dir`).

## Risks
- `BarCache.read` might create lock/temp files or a directory; AC 5 catches this. If so the worker stops and reports, and the PM may allow a pure `pd.read_parquet` reader in `lib/sources.py` instead.
- Running the test suite from the UI takes about a minute and the run can block that Streamlit session thread; the lock prevents a second run but a long run feels frozen (spinner shown). Acceptable locally.
- `Run tests` shells out to `uv`; if `uv` is not on the PATH of the Streamlit process the page shows a clear error rather than failing.
- ROADMAP/review markdown formats may drift; parsers degrade to `UNKNOWN`/empty rather than raising.
- Multiple pages importing `STRATEGIES` triggers lazy YAML loading from the repo-root `strategies/` dir; a launch from another cwd still works because paths resolve from package `__file__`.
- Hosting or multi-user use would need removal of the subprocess features and path inputs; this app is local-only by design.
