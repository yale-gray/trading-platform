# Audit 001: Wave 2 data layer (data timing, bias, cache integrity)

- **Auditor:** quality-lead
- **Date:** 2026-10-20
- **Code:** `src/tradebot/data/{symbols 48, calendar 114, quality 173, cache 366, restatement 75, universe 55, service 422, __init__ 53, types, config, errors, provider}.py` and `src/tradebot/data/providers/{yfinance_provider 193, ccxt_provider 134, fixture_provider 64, __init__}.py` (1,804 lines in `data/` in total)
- **Tests:** `tests/data/*` (incl. `test_service.py` 600 lines, 34 cases with `test_fixtures.py`; both landed during the audit), `tests/unit/test_data_quality.py`, `tests/unit/test_universe_membership.py`, `tests/lookahead/*`. Scoped run: 289 passed, 1 skipped (STRATEGIES registry, known). `uv run ruff check .`: clean. mypy not run.
- **Against:** SPEC-001 sections 1, 3, 4, 5, 6; ADR-0001 D1 (labels / completed bars), D2 (fully adjusted OHLC), D9 (fixtures); SPEC-005 section 8 severity scale
- **Verdict: PASS-WITH-NOTES**

No lookahead path and no wall-clock read was found, and the adjustment math is correct (one factor on O/H/L/C, volume untouched). Nothing here produces wrong numbers on the normal path, so there is no blocker. Five majors remain (cache crash window, no minimum overlap in restatement detection, a 1 ppm tolerance with a permanent wedge on any quarantine, and two untested safety nets). They should be fixed before the Wave 3 paper runner depends on this layer unattended.

## Scope

All of `src/tradebot/data/` and its providers, the tests listed above, and the data-layer slice of the lookahead tests. Not in scope: indicators and strategies (only the lookahead harness was read), execution, platform CLI. The PM's known-items list was not re-reported (see the end).

## Evidence

- `uv run pytest tests/data tests/unit/test_data_quality.py tests/unit/test_universe_membership.py tests/lookahead -q`: 289 passed, 1 skipped (after `test_service.py` and `test_fixtures.py` appeared; first run before they existed was 255 passed, 1 skipped).
- `uv run ruff check .`: all checks passed.
- Wall-clock grep of `src/` for `datetime.now`, `date.today`, `time.time`, `utcnow`, `Timestamp.now/today`, `monotonic`: only `clock.py:32` (`SystemClock`). `SystemClock` is the default in `MarketData.__init__` (`service.py:71`) and both providers (`yfinance_provider.py:45`, `ccxt_provider.py:44`), which is the production wiring, not a leak.
- Calendar probes (`last_closed_bar_date`, throwaway script in the scratchpad): 2024-11-29 early close (18:00 UTC): 18:59 returns 11-27, 19:00 returns 11-29. 2024-07-03 early close (17:00 UTC): 17:00 and 16:59 return 07-02, 18:00 returns 07-03. Crypto: 2024-05-05 00:00:00 returns 05-04, 05-04 23:59:59 returns 05-03. Correct, including the DST/early-close handling.
- Facade probes with a fake provider on temp dirs (no fixtures touched):
  - equity multi-day gap (cache 3/1, runner back 5/15): fetch starts at the tail window start (2/16) and is appended, no quarantine. Crypto gap likewise. Downtime plus a 0.42% dividend restatement: ratio 1.00422 recorded, no quarantine.
  - head prepend with a 0.99 restatement: prepended rows rescaled onto the cached basis (ratio 1.0 to 1e-16 at the join).
  - offline: zero provider calls for `get_bars`, `verify`, head sync, explicit `generation=`; `rebase` raises `DataError`; `require_fresh` raises `StaleDataError`.
  - `end` is honoured; the in-progress bar is dropped for equity and crypto.
- Restatement probes (200 random trials per row, `reconcile_overlap`): jitter of +-1e-7 per element: 100% `no_change` (and 100% `restatement` with a true 0.42% factor). +-1e-6: still 100% fine. +-2e-6: 64% revision (quarantine) when there is no restatement, 69% when there is. +-1e-5: 100% revision.
- Mutation run on a scratch copy of `src/` and `tests/` (26 mutants, see table). `git status tests/fixtures`: no changes.

## Checklist (Y / N / n.a. with file:line)

| Item | Result | Evidence |
|---|---|---|
| Lookahead: completed bars only, nothing after `end` | Y | Four independent filters: `yfinance_provider.py:173-178`, `ccxt_provider.py:117-121`, `quality.py:92-103`, `service.py:266-268`. Early closes and DST via `exchange_calendars` `session_close` (`calendar.py:56-63`). See F6, G1. |
| Truncation test present | Y for indicators (`test_indicator_truncation.py:46-55`); data layer covered by `test_service.py:431` (in-progress bar), `test_quality.py:246-285`, provider tests | `_read` cutoff is not bound (G1) |
| Wall-clock reads | Y (none) | grep above; `tests/test_no_wallclock.py` (pattern list is narrow, F15) |
| Adjustment math (D2) | Y | `yfinance_provider.py:160-171`: one factor on O/H/L/C, volume raw; restatement scales OHLC only (`restatement.py:69-70`); mutants M1, M2, M3, M15 killed |
| Cache append-only / immutable | Y with caveat | Rewrites the current generation file on append (`cache.py:286-294`) but verifies old rows byte-equal (`cache.py:270-278`); interior inserts refused (`:282-284`); M10, M11 killed |
| Atomic writes | Partly | Per file yes (`cache.py:63-76`); the pair is not atomic (F1) |
| sha256 verified on read | Y | `cache.py:163-164`; M9 killed. `fingerprint` and `_tail_plan` trust `gen.json` (F12) |
| Generation semantics | Y | New generation per rebase (`cache.py:228-249`), old ones stay readable; `append` grows the current generation in place, so `generation=N` is "N as of now", not a snapshot (F12) |
| Restatement vs revision | Partly | Correct math and chain-linking; weak guards (F2, F3) |
| Facade: gap refresh, head prepend, staleness, routing, offline, verify | Y | probes above; routing `service.py:87-99`; untested head prepend (G1) |
| Survivorship | n.a. in v1 (by PM decision) | `universe.py:54-55` always true; accepted caveat in SPEC-001 section 6 (F14) |
| Offline tests | Y | network guard in `tests/conftest.py`; providers injected |
| Costs / accounting / Decimal / benchmark | n.a. | not part of this layer |

## Findings (ranked)

| # | Severity | file:line | Issue | Required fix |
|---|---|---|---|---|
| F1 | major | `cache.py:198-210`, `:286-294` | `append` replaces `bars.parquet` in place, then replaces `gen.json` (sha256, first, last). A crash or Ctrl-C between the two `os.replace` calls leaves new bars with the old sha. Probed: every later read, refresh and verify of that symbol raises `CacheCorruptError`, and the previous bars are already overwritten, so there is no rollback. Fail-closed (loud, never silently wrong), narrow window, but it bricks a symbol until a manual repair or a rebase onto a new basis, which loses the immutability promise for that history. There is no lock either, so two writers (runner plus CLI refresh) can interleave the two files the same way. | Make the commit atomic: write `bars-<sha8>.parquet` (content-addressed name), then publish by one `os.replace` of `gen.json` that names the file and its sha. The old file stays until the new `gen.json` lands. Add a `flock` on the symbol dir for writers. At minimum, add a `verify --repair` path that recomputes the sha when the parquet is valid and a superset of the last known rows. |
| F2 | major | `restatement.py:56-58`, `:66-71` | No minimum overlap. With a single shared bar, `std == 0`, so any ratio, including a genuine 3% one-bar revision, is classified "restatement" and the new rows are scaled by it. Probed through `MarketData`: event `n_overlap: 1, ratio 1.0309` recorded, new rows silently re-based on a bad bar. The facade normally asks for 10 bars, but `validate_bars` ignores leading gaps (`quality.py:137-139`), so a provider that drops the early overlap days gets through. Immutable cache means the damage is permanent. `tests/data/test_restatement.py:118` (`overlap=st.integers(1, 10)`) cements the 1-bar behaviour as correct. | Require `n_overlap >= min(3, len(cached_tail))` (or overlap covering at least half the tail); otherwise raise `DataRevisionError`. Change the property test's lower bound and add a 1-bar-revision test. |
| F3 | major | `restatement.py:20`, `:66-68`; `cache.py:320-326`; `service.py:130-141` | (a) Tolerance is 1e-6 relative on both checks, applied after multiplying by a per-bar `Adj/Close` factor. Probed: 1e-7 jitter is safe (zero false positives), 1e-6 is the edge, 2e-6 flips about two thirds of trials to a revision. That margin is only ~16x over float32 noise and about 1x over 4-decimal rounding of a $30-$50 ETF price (5e-7 to 2e-6 relative). It was never calibrated against real Yahoo output. (b) Any quarantine wedges the symbol permanently: the overlap window is "last 10 cached bars", the cache never advances while the fetch is rejected, so the same rejected comparison repeats forever. Probed: a Yahoo dividend adjustment that lands one day late (ex-date bar inside the window) produced `DataRevisionError` on 11 consecutive days and 11 quarantine files. The only exit is a human `rebase`. Fail-closed, but the paper runner would go stale on a harmless, recurring event (17 ETFs, roughly monthly dividends). | Calibrate the tolerance on a real captured pair of Yahoo fetches (a manual one-off, outside tests), or use `abs(ratio_i - mean) <= max(1e-6, 2 * rounding_floor / price)`. Add a documented recovery path: on a revision, retry with the window anchored at the last bar whose ratio matches (the post-ex-date bar), or add `tradebot data accept-quarantine`. At minimum, surface the wedge in `refresh()` reports and the runner health check. |
| F4 | major (test) | `service.py:266-268`; `service.py:158-195` | Two safety nets have no binding test. Mutant M16 (delete the `_read` cutoff) survives all 289 tests, and M19/M20 (head prepend unscaled / disabled) survive. The `_read` cutoff is the last lookahead defence for a cache holding bars newer than the clock (offline backtest with a fixture cache and an earlier `FixedClock`, or a clock reset). Head prepend (rescale, `_head_tried`, no-overlap refusal, quarantine) has no test at all. | Add to `tests/data/test_service.py`: (1) offline `MarketData`, cache with bars to 2024-12-31, `FixedClock` at 2024-12-20 21:30 UTC, assert the last returned bar is 12-20 and `latest_bar` the same, for equity and crypto; (2) head prepend with a constant-ratio restated fetch (check join continuity and event), with a varying-ratio fetch (expects `DataRevisionError`, quarantine, cache unchanged), and a second call making no provider request. |
| F5 | minor | `quality.py:107-109` | No finiteness check: `+inf` prices pass (`isna()` and `<= 0` are both False). Probed: an `inf` close row is accepted into the clean frame with only an "outlier +inf%" warning. A Yahoo `Close == 0` with `Adj Close > 0` gives `inf` in `_to_frame` (`yfinance_provider.py:160-166`) and would reach the immutable cache. | `~np.isfinite(prices).all(axis=1)` joins the bad-price mask. Add a test. |
| F6 | minor | `quality.py:137-146`, `service.py:148-156` | Bars on non-session days (weekend or holiday rows, a known Yahoo artefact) are never flagged: the gap check only looks for missing sessions. Probed: a Saturday bar is validated, cached and then immutable. Future-dated labels are caught by the cutoff; this is only about phantom rows on dates in the past. | Treat `present.difference(expected)` for equities as a `DataQualityError` (spec lists it nowhere, so add it to SPEC-001 section 5), or drop and warn. |
| F7 | minor | `service.py:234-246`, `:359-366` | `verify()` (and the offline `refresh()`) catch only `DataQualityError`; a `CacheCorruptError` from the sha check propagates and aborts the loop. The docstring promises that "one bad symbol does not hide the rest". Probed with one corrupted symbol of two. `verify` never called a provider (confirmed: provider call list empty in every probe, incl. corrupt case). | Catch `CacheCorruptError` and `DataError` per symbol and report as `warnings=["corrupt: ..."]`. |
| F8 | minor | `service.py:213-231`, `:217-219` | `_sync_tails` has no per-symbol isolation: one `DataQualityError`/`DataRevisionError` aborts the batch, so later symbols are not refreshed. `fetch_many` also fetches `min(start)..max(end)` for the group, so a freshly added symbol (start 2005) makes every other symbol's current cache validate a 20-year frame; one historical Yahoo gap then blocks all symbols' freshness. | Catch per symbol, record the error in the report, continue; slice each frame to its own plan range before `_ingest_tail`. |
| F9 | minor | `yfinance_provider.py:80-82`, `service.py:148-149` | If a batch returns data for some tickers and nothing for one, that ticker silently becomes an empty frame; `_ingest_tail` returns a report with `n_bars=0` and no warning (probed). Only `require_fresh` catches it later. | Add a warning ("provider returned no rows for X") in the report when a non-empty plan returns an empty frame. |
| F10 | minor | `service.py:107-108`, `quality.py:94`, `yfinance_provider.py:174` | `equity_close_buffer_min` is honoured only by `_target`; `validate_bars` and the yfinance provider use the hard-coded 60. A config of 30 leaves the newest bar un-cacheable (stale) for 30 minutes; 120 hides it (safe). | Pass the config buffer into both, or remove the config knob. |
| F11 | minor | `calendar.py:69`, `ccxt_provider.py:120` | Crypto buffer is 0: the first fetch at 00:00:00 UTC can see a not-yet-final daily candle, and it becomes immutable. | Use a small crypto buffer (5 to 10 min), same as equities. |
| F12 | minor | `cache.py:350-366` | `fingerprint` hashes the whole-file sha, so any append or head prepend changes the fingerprint of an unchanged backtest window (false "data changed"), and it trusts `gen.json` without re-hashing `bars.parquet`. | Hash the window rows plus the gen id (and verify the file once). Document that a generation is append-only, not a frozen snapshot. |
| F13 | minor (test) | `tests/unit/test_universe_membership.py:50-120` | The survivorship tests exercise a test-local helper (`_available`) over fixture files, not production code. They pass whatever `universe.py` does. The always-true `StaticUniverse` is the PM's v1 decision. | Move the first-available-date helper into `universe.py` (SPEC-001 section 6) and test that, or retitle the tests as fixture-shape checks. |
| F14 | nit | `restatement.py:66-71` | Only `close` is compared over the overlap; a revised open/high/low with an unchanged close is invisible (cached rows are never rewritten, so no corruption, but a revision goes unreported). | Optionally also compare H/L ratios; info only. |
| F15 | nit | `tests/test_no_wallclock.py:9` | The grep guard misses `from datetime import datetime as dt`, `pd.Timestamp.now(`, `time.monotonic(`. My grep for those found nothing, so no live issue. | Add the extra patterns, or an AST check. |
| F16 | nit | SPEC-001 section 5 | The text says the chain-linked series is "price-return-like"; the math in `reconcile_overlap` keeps the new rows on the cached basis, which keeps the series continuous across the ex-date (total-return-like). Behaviour is fine; the doc is wrong. | Fix the sentence. |

## Mutation and test-strength review

Mutants applied to a scratch copy (`scratchpad/mut`), tests: data, unit data, lookahead. By real run, not by reading.

| Mutation | Caught by | Verdict |
|---|---|---|
| M1 volume multiplied by the factor | `test_yfinance_provider.py::test_split_like_factor_applies_to_ohlc_and_leaves_volume_unchanged` | Caught |
| M2 / M3 open (high) not adjusted | same test | Caught |
| M4 yfinance no cap at last closed bar | `test_in_progress_session_dropped_before_close_plus_60_and_kept_after` | Caught |
| M5 ccxt keeps today's bar | `test_unclosed_bar_for_today_utc_is_dropped_with_fixed_clock` | Caught |
| M6 / M7 equity buffer 0 / ignore buffer | `test_calendar.py::test_last_closed_bar_inside_buffer_is_previous_session` | Caught |
| M8 `validate_bars` keeps in-progress bar | `test_quality.py::test_equity_bar_inside_close_buffer_is_dropped` | Caught |
| M9 `read` skips sha check | `test_cache.py::test_corrupted_bytes_raise_cache_corrupt` | Caught |
| M10 / M11 append allows altered / interior rows | `test_cache.py::test_append_differing_overlap_raises_and_leaves_cache`, `::test_append_interior_rows_refused` | Caught |
| M12 tolerance 1e-3 | `test_restatement.py::test_non_constant_perturbation_above_tol_is_revision` | Caught |
| M13 / M14 / M15 inverse scale / `>=` last / scale volume | restatement and cache restatement tests | Caught |
| M16 `_read` cutoff removed | none (289 pass) | **Survives** (F4) |
| M17 staleness tolerance 3 days | `test_service.py::test_require_fresh_raises_when_clock_moves_past_the_data` | Caught |
| M18 overlap window 1 bar | `test_revised_tail_raises_and_appends_nothing` | Caught |
| M19 head prepend not rescaled | none | **Survives** (F4) |
| M20 head prepend disabled | none | **Survives** (F4) |
| M21 `verify` also syncs tails | `test_verify_reports_missing_cache_as_a_warning` | Caught |
| M22 offline still refreshes | `test_offline_get_bars_matches_manifest_for_all_twenty_fixture_symbols` | Caught |
| M23 gap check disabled | `test_equity_interior_gap_is_error_listing_the_missing_session` | Caught |
| M24 `os.replace(path, path)` after replace | no effect | Equivalent (a no-op mutant, only shows the atomic test does not detect the two-file crash; see G3) |
| M25 fingerprint ignores sha | `test_fingerprint_changes_across_generations_and_appends` | Caught |
| M26 restatement event not recorded without new rows | `test_restatement_without_new_rows_still_records_event` | Caught |

Leak canaries (`tests/lookahead/test_lookahead_detector_catches_leaks.py`): they do bind. Four leak types (`shift(-1)`, centered rolling, global max normalisation, `bfill`) must fail both checkers, and a causal control must pass both, so the checker is neither vacuous nor always-failing. They test the harness and indicators on the SPY fixture, not the data layer (data-layer binding is by `test_service.py:431` and the provider/quality tests above). Residual gaps are G5 and F15.

## Test gaps

- **G1 (major):** `_read` cutoff and head prepend, as in F4.
- **G2 (minor):** no service-level test for the exact failure the PM named: a runner restarted after multi-day downtime (equity and crypto) must fetch from the tail-window start, append, and not quarantine, and it must also survive downtime across a dividend restatement. My probes pass; add them as regression tests to `test_service.py` using `FixedClock.advance`.
- **G3 (major, pairs with F1):** no crash-between-files test. Existing atomic tests (`test_cache.py:285`, `:306`) fail one `os.replace`, not the bars/gen.json pair. Add: patch `_atomic_json` to raise on `gen.json` during `append`, then assert the cache is either at the old state or readable.
- **G4 (minor):** no tests for F5 (inf price), F6 (phantom session bar), F7 (verify with a corrupted symbol), F9 (empty ticker in batch).
- **G5 (nit):** `test_indicator_truncation.py` compares exact floats (rtol 0) between prefix and full-series rolling results; it passes today but an indicator using an online/incremental rolling algorithm could differ in the last ulp for reasons unrelated to lookahead. Keep an eye out when new indicators land. The canaries also don't cover a volume-only leak.

## Re-audit needed? yes

Trigger: fixes for F1 + G3, F2, F3 (calibration note or recovery path), and G1/F4. A short re-audit then (rerun the mutation set M16, M19, M20, add M-overlap and a crash-window mutant) is enough. Minor findings F5 to F13 can ride along without a re-audit if tests are added.

## Known items (not re-reported, confirmed present)

Crypto `spot_` basis prefix rejected by the cache (`cache.py:51-52`, `service.py:101-104`); bare `BTC` accepted as an equity (`symbols.py:15`); a yfinance range with no sessions retries instead of returning empty (`yfinance_provider.py:80-82`); `StaticUniverse.is_member` raises on 6-letter names (`universe.py:55`); lookahead STRATEGIES meta-test looks in the wrong module (`test_indicator_truncation.py:68`); schema asset-class heuristic should use `data.symbols.asset_class` (`calendar.py:26-27` and `quality.py:82` also duplicate the `"/" in symbol` test).

## Re-audit (fix pass)

**Verdict: PASS.** No blocker and no major is open. Full suite (1386 passed, 1 skipped) re-run by me. `git status tests/fixtures` is clean (fixtures read unchanged). Scope was read-only: mutants and a crash probe were run on a scratch copy of `src` and `tests` only.

### Majors

| Finding | Status | Evidence |
|---|---|---|
| F1 non-atomic bars/gen.json pair | Fixed | `cache.py:215-258`. `append` writes a content-addressed `bars-<sha8>.parquet` (`os.replace` into a name that is not yet referenced), then publishes with ONE `os.replace` of `gen.json`, which carries `file` and the full `sha256`. Stale files are swept only after the publish (`:249-252`). Crash probe: I forced `os.replace` to raise at each of its calls during `append` (the bars file, then `gen.json`). In both cases the old generation reads intact, `gen.json` names a file that exists, and a retried append succeeds. `gen.json` can only point at a file written and fsynced before it. Mutants: sweeping before publish, a fixed file name (in-place overwrite) and a disabled sweep are all caught (`test_cache.py:323` G3 test and the atomic tests). |
| F2 min overlap | Fixed | `restatement.py:23`, `:73-77`. `DEFAULT_MIN_OVERLAP=3`; an exact match is still "no_change" at any overlap, and a non-identical overlap below 3 bars raises. Mutant `min_overlap=1` is caught. |
| F3 tolerance | Fixed | `restatement.py:22`, `:78`. Tolerance is relative 1e-5, with `(max-min)/mean`. Mutants "absolute spread" and "default 1e-3" are caught. The recovery path is `RevisionQuarantinedError` with a `status` and `symbol`, memoised per `(source, symbol, generation)` (`service.py:60-72`, `:243-262`, `:294-306`). It is cleared by `rebase`, or when another process changes the generation (`_active_wedge`). The memo-off mutant is caught. Recovery stays a human `rebase`, as agreed. |
| F4/G1 untested safety nets | Fixed | Mutants M16 (`_read` cutoff removed), M19 (head not rescaled) and M20 (head prepend disabled) are all now caught by `tests/data` (1 failure each). Head prepend now uses `reconcile_overlap` (`service.py:197-206`), so it shares the tail's tolerance and minimum-overlap rule. |

### Other mutants re-run (all caught)

- `flock` removed (`test_second_writer_is_refused_while_lock_held`).
- Phantom-session drop disabled.
- Infinite price check removed. My first inf mutant was equivalent, because the `bad_price` term is redundant with the separate error; the real removal was caught.
- sha256 verification on read removed.

### Lock check

`_symbol_lock` is an exclusive `flock` on the symbol directory fd, non-blocking with a 30 s retry that raises `DataError`. It is held only in `_new_generation`, `append` and `_record_event`. None of these calls another (`apply_fetch` calls `append` or `_record_event` after its own read, outside any lock), so there is no re-entrant `flock` on a second fd, which would self-deadlock. Reads take no lock. The kernel releases the lock on process death, so there are no stale locks. The suite and my probes show no hang in single-process use.

### F6 decision (drop non-session equity bars with a warning, not reject): sound

- It is conservative. It only removes rows labelled on days the exchange calendar says are closed. The result is a missing bar, never a fabricated one.
- The warning is recorded in `QualityReport.warnings` and logged (`quality.py:111-123`).
- Genuine missing real sessions still fail the interior-gap check. That check runs after the drop, over the sessions calendar.
- Residual note (nit): if the calendar library wrongly lists a real trading day as closed, that bar is dropped, with a warning only. Keep the calendar pinned and tested. This is not a blocker.

### Minors

F5 (inf), F6, F7 (verify and offline refresh report corruption per symbol with `status="corrupt"`), F8 (per-symbol isolation in `_sync_tails`), F9 (missing-ticker warning and no-session range skips the download), F10 (buffer plumbed), crypto `spot_` basis, bare `BTC` rejected, and `is_member` returning False for unparseable symbols are fixed and covered, with tests in `test_quality.py`, `test_service.py`, `test_yfinance_provider.py` and `test_symbols.py`. G2 (downtime regression) was already passing.

### Remaining findings (none blocking)

| ID | Sev | Where | Finding |
|---|---|---|---|
| R1 | minor | `service.py:179-206` | A head-prepend revision is quarantined and raised once, but `_head_tried` is set before the fetch (`:190`), so the next call with the same `start` returns silently. The head is simply never prepended. This is safe (nothing is written), but the wedge is not memoised like the tail's. Consider recording it in `_wedged`, or surfacing it in `verify`. |
| R2 | minor | `cache.py:149-155` | Multi-process only. `read` checks `bars.is_file()` and later hashes the file. A writer that publishes and sweeps in between makes `_sha256_file` raise `FileNotFoundError` rather than retrying. The retry loop covers only the `is_file` window. Wrap the hash and read in the retry. |
| R3 | nit | `cache.py:98-107`, `:75-86` | No directory fsync after `os.replace`. A process crash is fully covered. After a power loss on a filesystem that reorders renames, `gen.json` could name a bars file that is not durable. That fails loudly with `CacheCorruptError` and is recoverable by `rebase`. |
| R4 | nit | `cache.py:25` | `fcntl` makes the cache POSIX-only. Fine for the stated platforms. |
| R5 | minor | `restatement.py` | Unchanged and not requested: F11 (crypto buffer 0), F12 (fingerprint changes on any append), F14 (only `close` compared in the overlap) and F15 (the wall-clock grep guard), plus F13 and F16, remain as written above. Take them with Wave 3. |
