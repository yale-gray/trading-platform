# Audit: SPEC-003 T1 money module (Wave 1)

- **Auditor:** quality-lead
- **Date:** 2026-10-20
- **Code:** `src/tradebot/portfolio/money.py` (194 lines)
- **Tests:** `tests/portfolio/test_money.py` (95 cases collected, all pass; `ruff check` clean; mypy not installed in `.venv`, so not run)
- **Against:** SPEC-003 section 2 (rounding table) and the T1 block (API table and criteria 1-10), ADR-0001 D7 and addendum A6
- **Verdict: PASS-WITH-NOTES**

Every quantum and rounding mode matches the spec table. There are no correctness defects on the paths the engine will use today. The notes cover one real hazard at the float boundary (numpy scalars), incomplete ambient-context independence, and test-strength gaps. None blocks Wave 2.

## Checklist

| Item | Result | Evidence |
|---|---|---|
| Price input quantum 1e-8, HALF_EVEN | Y | `money.py:27`, `:70` |
| Buy fill CEILING, sell fill FLOOR | Y | `ceil8` `:108-110`, `floor8` `:113-115` |
| Notional / market value cents HALF_EVEN | Y | `q2` `:118-121` |
| Fee cents HALF_UP | Y | `q2_half_up` `:124-127` |
| Whole-share buy sizing DOWN, quantum 1 | Y | `:130-135` |
| Crypto buy sizing DOWN, 1e-8 | Y | `:138-143` |
| Sells never rounded, only checked | Y | `check_sell_qty` `:153-162` |
| Weights 1e-6, ratios 1e-10, HALF_EVEN | Y | `:97-99`, `:165-174` |
| Explicit `rounding=` on every quantize | Y | `:70`, `:105`, `:121`, `:127`, `:135`, `:143`, `:159`, `:168`, `:174` |
| No `Decimal(float)` anywhere | Y | The only `Decimal(` calls are the constants (`:27-32`), `Decimal(repr(x))` (`:70`) and `Decimal(s)` on a `str` (`:189`) |
| Float boundary is `Decimal(repr(x))` only | Y, with the numpy caveat in F1 | `:66-72` |
| Errors: NaN, inf, <=0, bool, str, Decimal | Y | Probed. `bool` and `str` give `TypeError`. NaN, inf and `<=0` give `BadPriceError` / `BadRatioError`. 1e-9 and 5e-9 (tie to 0) give `BadPriceError`. |
| `to_text` / `from_text` round-trip preserves exponent | Y | Tests `:352-368` |
| `AssetClass` re-export | Y | See below |
| Ambient context independence | Partial | prec and rounding are independent. Traps and Emax are not (F2). |

**`AssetClass` re-export (confirmed).** `from tradebot.portfolio.money import AssetClass` works, and `money.AssetClass is tradebot.data.types.AssetClass` is `True`. Importing money loads neither pandas nor numpy: `tradebot/data/__init__.py` is empty and `types.py` only imports `StrEnum` and `NewType`. This deviates from T1's "no imports outside the stdlib", which is the accepted PM decision. The effective property (money stays dependency-light) still holds. `ruff` does not flag the import because it is used in `buy_qty` and `check_sell_qty`. The test imports it via money (`test_money.py:22`), so the re-export is covered implicitly. Money has no `__all__`, so the re-export is implicit; ruff will not warn if a later edit stops using it.

## Findings (ranked)

### F1 (Medium): `numpy.float64` is rejected as an invalid price or ratio, with a misleading error
`_check_number` (`money.py:61-63`) accepts `np.float64`, because it subclasses `float`. Under numpy 2.x (2.5.4 is installed), `repr(np.float64(1.5))` is `'np.float64(1.5)'`. `Decimal('np.float64(1.5)')` raises `InvalidOperation`, which `_from_number` (`:68-72`) swallows and returns `None`. The caller then gets `BadPriceError: price not representable or quantizes to zero: np.float64(1.5)`.

Probed: `to_price(np.float64(1.5))` raises `BadPriceError`, and `to_ratio(np.float64(0.5))` raises `BadRatioError`. `np.int64` raises `TypeError`, which is acceptable.

Why it matters: the data layer reads parquet through pandas, so bar values are `np.float64` unless the caller does `.item()` or `float(x)` first. `BarDriver` is the sole float-to-Decimal boundary (ADR D3), and a missed `float()` there becomes a whole-run `BadPriceError` that looks like a data problem. The conversion is also the "only float entry" by contract, so it should be robust to numpy scalars.

Fix options:
- (a) Convert first with `x = float(x)` for `float` instances. `float(np.float64)` is an exact conversion, so the `repr` of the result is the plain Python repr.
- (b) Use `repr(float(x))` in `_from_number`.
- (c) Document that callers must pass builtin floats, and add a test.

Option (a) with a test (`to_price(np.float64(1.5))`, if numpy is a dev dependency) is cheapest. Also separate "not parseable" from "quantizes to zero" in the error text.

### F2 (Low-Medium): Ambient-context independence covers prec and rounding only; traps and Emax leak through
`money_context()` (`:53-58`) uses `localcontext()`, which copies the caller's context and then sets only `prec`. That matches the spec wording ("localcontext with prec=28"), but the stated property (T1 criterion 10, "independent of the caller's ambient decimal context") is not fully true. Probed:
- With `InvalidOperation` trap disabled in the caller: `to_price(1e30)` and `q2(Decimal("1e30"))` return `Decimal('NaN')` silently. `_from_number`'s `except InvalidOperation` never fires, and `result <= 0` on NaN is `False` when the trap is off, so `to_price` hands NaN back as a price.
- With `Emax=5`: `q2(Decimal("1e9"))` raises.
- With `Inexact` / `Rounded` trapped in the caller: `to_price(1.000000005)` and `q2(Decimal("2.345"))` raise `Inexact`.

Production risk is low because nothing in the codebase sets these. It is cheap to close, though: build an explicit `decimal.Context(prec=28, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999, traps=[InvalidOperation, DivisionByZero, Overflow])` and use `decimal.localcontext(ctx)`. That makes the guarantee real and also covers the NaN-return path in F3.

### F3 (Low): Non-float `Decimal` helpers accept or mishandle NaN and Infinity, and overflow raises a raw `InvalidOperation`
- `q2(Decimal("NaN"))` returns `NaN` silently. `q2(Decimal("Infinity"))` raises a raw `decimal.InvalidOperation`. The same happens in `q_price`, `q_ratio`, `q_weight`, `ceil8` and `floor8`.
- `floor_shares(NaN)` and `floor_qty8(NaN)` raise a raw `InvalidOperation` from the `d < 0` comparison (`:132`, `:140`). That is not a `BadQuantityError`.
- `check_sell_qty(Decimal("1e30"), EQUITY)` raises a raw `InvalidOperation`: it needs 31 digits against prec 28 (`:159`). Realistic quantities never get there.
- `q2(1.5)` raises `AttributeError`, and `check_sell_qty(3.0, ...)` raises `AttributeError` at `:155`. A float reaching the Decimal helpers is the bug class this module exists to prevent, but the failure is incidental, not a deliberate `TypeError`. Spec T1 does not require a type check here.

Suggestion: validate `isinstance(d, Decimal)` and `d.is_finite()` in the shared quantize helper, and wrap `InvalidOperation` in `MoneyError`. Low severity because the engine feeds these only from its own Decimal arithmetic.

### F4 (Low): Negative zero is produced and persisted as a distinct string
- `to_ratio(-0.0)` returns `Decimal('-0E-10')`. `-0.0 < 0` is `False`, so it passes the `< 0` check. `to_price` is fine because it rejects `<= 0`.
- `q2(Decimal("-0.001"))` yields `Decimal('-0.00')`, and `floor_shares(Decimal("-0"))` yields `-0`. `to_text` gives `"-0.00"` / `"-0E-10"`.

`-0 == 0`, so arithmetic is fine. But the text differs from `"0.00"`, which matters for `ledger_sha256` goldens (SPEC-005 section 5) and the canonical-persistence claim in ADR D7. A P&L that rounds to a tiny negative would persist as `-0.00`. Fix by normalizing zero (`if r == 0: r = abs(r)`, or `+ Decimal(0)`) in `q2`, `q_ratio` and `to_ratio`, and pin the behaviour with a test. If the decision is to keep `-0.00`, document it.

### F5 (Low): `from_text` accepts non-canonical input
`from_text(" 1.5 ")` returns `Decimal('1.5')` and `from_text("1_0")` returns `Decimal('10')`. Both are Python `Decimal` parsing rules, not canonical `str(Decimal)` output. Harmless unless the DB can contain hand-edited text. Optionally reject whitespace and underscores, or document the leniency.

### F6 (Info): `PRICE_Q` and `QTY8_Q` are equal-valued constants (`:27`, `:29`)
Swapping them is undetectable and harmless. Noted so nobody reads it as a gap.

## Mutation review (mentally applied, then confirmed against test lines)

| Mutation | Caught by | Verdict |
|---|---|---|
| `ceil8`: CEILING to FLOOR | `test_buy_fill_price_ceiling_at_five_bps` `:170`, `test_ceil8_rounds_up_even_when_next_digit_is_tiny` `:188`, `test_q_price_passes_rounding_mode_through` `:203`, the table `:452` | Caught |
| `floor8`: FLOOR to CEILING | `:177`, `:193`, `:203`, table `:453` | Caught |
| CEILING to FLOOR in `ceil8` on negative values | `:197` | Caught |
| `_from_number`: HALF_EVEN to HALF_UP | `test_to_price_tie_rounds_to_even_zero` `:73` (gives 1.00000001) | Caught. It guards `to_ratio` and `to_weight` too, since they share `_from_number`. |
| `_from_number`: HALF_EVEN to HALF_DOWN | `:78` (1.000000015 gives 1.00000001, expected 2) | Caught |
| `q2`: HALF_EVEN to HALF_UP | `:212`, `:222` | Caught |
| `q2`: HALF_EVEN to HALF_DOWN / DOWN | `:217` (2.355 expected 2.36) | Caught |
| `q2_half_up`: HALF_UP to HALF_EVEN | `:226`, `:244` | Caught |
| `q2_half_up`: HALF_UP to HALF_DOWN or DOWN | `:226`, `:230` | Caught |
| `q_ratio`: HALF_EVEN to HALF_UP | `:436-440` (5e-11 would give 1e-10, expected 0) | Caught |
| `q_weight`: HALF_EVEN to HALF_UP | `:441` (0.1234565 gives 0.123457, expected 0.123456) | Caught |
| `floor_shares`: DOWN to HALF_UP | `test_floor_shares_truncates_toward_zero` `:264` (12.9999 gives 13) | Caught |
| `floor_shares`: DOWN to CEILING | `:264`, `:270` | Caught |
| `floor_qty8`: DOWN to HALF_UP | `:284` (0.123456789 gives 0.12345679) | Caught |
| `floor_qty8`: DOWN to CEILING | `:284`, table `:464-465` | Caught |
| `floor_shares` / `floor_qty8`: DOWN to FLOOR | none | Equivalent mutant: negatives are rejected before the quantize |
| `check_sell_qty`: DOWN to any other mode | `:316`, `:325` | Equivalent: any mode that changes the value fails the `==` check |
| `to_price`: drop the `result <= 0` guard | `:92` | Caught |
| `to_ratio`: `< 0` to `<= 0` | `:132` (zero allowed) | Caught |
| `check_sell_qty`: `<= 0` to `< 0` | `:330` (0) | Caught |
| Remove `rounding=` from `_from_number`, `q_ratio` or `q_weight` | none | **Survives.** The default context rounding is HALF_EVEN, the same as intended. Only an ambient-rounding test would detect it (G1). |
| Remove `money_context()` from `q_ratio`, `q_weight`, `floor_shares`, `floor_qty8` or `check_sell_qty` | none | **Survives.** The ambient-precision tests cover only `q2`, `to_price`, `ceil8` and `floor8` (`:395-425`). |
| `floor_qty8` result exponent | value equality only | Minor: `floor_qty8(...).exponent == -8` is not asserted (`floor_shares` is, at `:267`). |

## Test gaps

- **G1 (Medium):** Ambient-rounding and ambient-precision tests do not cover `to_ratio`/`to_weight`, `q_ratio`, `q_weight`, `floor_shares`, `floor_qty8`, `check_sell_qty`. The only ambient-rounding tests are on `q2` and `q2_half_up` (`:254`, `:403`) and on `ceil8`/`floor8` (`:419`). Add a parametrized test that sets the ambient rounding to something different from each function's intended mode, plus `prec=4`, and calls every public function.
- **G2:** No test for numpy scalars (F1), negative zero (F4), NaN/Infinity into the Decimal helpers (F3), or trapped / `Emax`-limited ambient contexts (F2).
- **G3:** `to_ratio` of a value that quantizes to zero (for example `1e-12`, which gives `0E-10` and is accepted) is untested. The rule is intentional (zero is allowed for ratios), but it is unpinned.
- **G4:** `to_ratio` with an int input (`to_ratio(5)`) and a negative int are untested. `to_price(7)` is covered at `:66`.
- **G5:** The asymmetry between `check_sell_qty` accepting an on-grid value with extra trailing zeros (`Decimal("3.00")` for EQUITY) and `floor_shares` returning exponent 0 is untested. This matters for ledger text equality (`"3.00"` vs `"3"`).
- **G6 (Info):** Tests pin `to_price` and `to_ratio` at the extremes with only one huge value probed ad hoc (`1e30` gives `BadPriceError`). A regression test would document the overflow behaviour.

## Spec conformance notes

- T1 criterion 5 states `ceil8(Decimal("123.518517404505"))`. The tests use the exact product `123.5185174045050` (same value), which is correct and derived by hand.
- The tests were written from the spec (docstring `test_money.py:3`). Expected values spot-checked by hand: 123.45678901 x 1.0005 = 123.5185174045050 and x 0.9995 = 123.395060615495. Both match.
- `to_price`'s `TypeError` for `Decimal` input is tested (`:108`).

## Re-audit needed?
Only if F1, F2 or F4 are changed (small edits, then re-run `tests/portfolio/test_money.py`). A re-audit is triggered if the persistence format for zero changes, because the goldens' `ledger_sha256` would depend on it.


## Re-audit (fix pass)

**Verdict: PASS-WITH-NOTES.** F1, F2, F3, F4, F5 and G1 are fixed. There is no regression against SPEC-003 §2 rounding results, quanta or error types. One new Low finding (N1) is a persistence-text hazard. It is not a blocker. The fix is a one-line change to `to_text` plus one test.

Evidence: `uv run pytest tests/portfolio/test_money.py -q` gives 486 passed. I also ran throwaway `uv run python` probes, listed under each item.

### Status of earlier findings

| ID | Status | Evidence |
|---|---|---|
| F1 (numpy) | Fixed | `_normalize_number` (`money.py:107`) accepts `numbers.Real` and rejects `bool` and `np.bool_`. `Decimal`, `str` and `None` still raise `TypeError`. `np.float64`, `np.float32`, `np.float16`, `np.longdouble` and `np.int64` all convert correctly, also under a hostile ambient context. `float('nan')` raises `BadRatioError`. `Fraction` is accepted as a `Real` (`to_ratio(Fraction(1,3))` gives 0.3333333333). This is harmless, but it is looser than the spec's "float or int". Tests: `test_money.py:637-686`. |
| F2 (context) | Fixed | `_MONEY_CONTEXT` (`:43`) is built from scratch and used via `localcontext`. Probe: with ambient `prec=2, ROUND_UP, Emax=3, Emin=-3` and every trap enabled (including Inexact, Rounded, Subnormal and Clamped), `to_price`, `q2`, `from_text`, `to_ratio(np.float64)` and `to_price(np.int64)` give correct results. The caller's ambient context is untouched (`test_ambient_context_is_left_untouched`, `:607`). `_MONEY_CONTEXT.flags` are all False after the probes. |
| F3 (type, finite, overflow) | Fixed | `_quantize` (`:91`) checks the type and finiteness before the quantize, and wraps `InvalidOperation` in the per-helper error type. NaN, sNaN, +Inf and -Inf raise the right domain error on `q2`, `ceil8`, `q_ratio` and `floor_shares`. Overflow (`1E+27`, `1E+28`, `1E+900000`) gives `MoneyError`, `BadPriceError` or `BadQuantityError` as appropriate. Underflow (`1E-900000`) quantizes cleanly (`q2` gives 0.00). |
| F4 (-0) | Fixed | `_unsigned_zero` (`:84`) is applied in `_quantize`, `_from_number`, `to_text` and `from_text`. `to_ratio(-0.0)` gives `0E-10`. `q2(Decimal("-0.001"))` gives `0.00`. `from_text("-0E-10")` gives `0E-10`. The exponent is preserved. |
| F5 (strict text) | Fixed, with N1 below | `_CANONICAL_TEXT` (`:54`). Rejections tested at `:939`. |
| G1 | Fixed | `test_every_helper_ignores_ambient_context` (`:574`) runs 21 calls against 12 ambients (prec 3, small Emax/Emin, Inexact and Rounded trapped, InvalidOperation untrapped, and all 8 rounding modes). It checks both value and exponent. `test_error_paths_ignore_ambient_context` (`:591`) covers the error paths. The ambient list has no `capitals` case, and `to_text` is not among the 21 calls (see N1). |

### Specific checks requested

1. **Explicit context vs spec rounding table: no change.** `_MONEY_CONTEXT` has the same `prec=28` and the same `HALF_EVEN` default as before. Every quantize still passes an explicit `rounding=`. Probe results:
   - `q2(2.345)` = 2.34 and `q2(2.355)` = 2.36
   - `q2_half_up(2.345)` = 2.35 and `q2_half_up(-2.345)` = -2.35
   - `ceil8(-1.000000001)` = -1.00000000 and `floor8(-1.000000001)` = -1.00000001
   - the criterion 4 and 5 values are unchanged
   The 21-call G1 table also passes under all 8 rounding modes.
2. **`Emin`/`Emax` of ±999999 is safe.** This is the maximum allowed by the C `_decimal` module (MAX_EMAX = 999999999999999999 on 64-bit, so ±999999 is far inside the limit). It is also much wider than `prec=28` can use, so there is no spurious Overflow or Underflow for any value that fits in 28 digits at the quantum. Out-of-range values fail through `InvalidOperation` (a quantize that needs more than 28 digits), which `_quantize` wraps. Probe: `1E-900000` and `1E+900000` both behave as above. The max representable `q2` is below 1E+26 (28 digits including two decimals). That is far beyond any portfolio value, but `q2(Decimal("1E+27"))` does raise `MoneyError`, which is intended behaviour.
3. **`to_text` normalizing -0: acceptable.** The only deviation from the spec text "`str(d)`" (SPEC-003 §2 line 303 and criterion 9) is that `-0` and `-0E-n` serialise without the sign. The exponent is kept, so the `from_text(to_text(d))` same-exponent round-trip still holds. Negative zero is never produced by the module's own helpers now. It prevents two ledger hashes for the same economic state. Recommend a one-line amendment to SPEC-003 §2 saying "canonical form of `str(d)` with `-0` normalised to `0`", so the spec and code agree.
4. **Strict `from_text` regex vs `str(Decimal)`: no false rejection under the default or explicit context.** I tested `0.000000`, `1E+3`, `0E-10`, `0E-8`, `0E+3`, `1E-8`, `1E-7`, `1.5E-7`, `9E+27`, `12E+1` (gives `1.2E+2`) and a 27-digit value. All round-trip with the same value and exponent. The outputs of `q2`, `q_ratio`, `q_weight`, `floor_qty8` and `floor_shares` on zero and large values also round-trip as identical strings (`0.00`, `0E-10`, `0.000000`, `0E-8`, `0`, `12345.0000000000`). The regex needs a sign on every exponent, which is exactly what `str(Decimal)` emits. Only NaN, Infinity and a leading `+` are rejected. The spec asked for `MoneyError` if not finite, so that is consistent.

### Remaining findings

**N1 (Low, not blocking): `to_text` depends on the ambient `capitals` flag, so it can emit lowercase `e`, which `from_text` rejects.**
`to_text` (`money.py:243`) calls `str(...)`, and `Decimal.__str__` reads `capitals` from the *ambient* context. Probe: after `setcontext(Context(capitals=0))`, `to_text(Decimal("1E+3"))` gives `'1e+3'` and `from_text('1e+3')` raises `MoneyError: not a canonical decimal`. That breaks the persistence round-trip and also makes the persisted text (hence `ledger_sha256`) depend on ambient state. This is the one place where the module's "ambient context never matters" guarantee does not hold. Nothing in the codebase sets `capitals`, so production risk is very low. A caller that uses `Context(capitals=0)` would hit it, though.
Suggested fix: wrap the `str()` in `with money_context():` (capitals=1), or use `format(d, "")` / `d.__str__(context=_MONEY_CONTEXT)`. Add a `capitals=0` ambient (and a `to_text` call) to the G1 tables. Treat this as a nice-to-have fix, not a gate.

**N2 (Info): `to_text(Decimal("NaN"))` and `to_text(Decimal("Infinity"))` succeed, but `from_text` rejects the result.** This matches the spec, which says `TypeError` only. Persisted data stays safe, because every helper rejects non-finite values on input. Consider rejecting non-finite values in `to_text` too, so the asymmetry cannot reach the DB. This is optional.

**N3 (Info): `numbers.Real` also admits `Fraction`.** It is converted through `float()`, so the result is rounded and correct, just looser than "float or int". No action needed.

### Mutation spot-check on the new tests
Dropping `money_context()` from `q_ratio`, `q_weight`, `floor_*` or `check_sell_qty` is now caught by the 21-call table under `prec3` and `small_emax`. Dropping `rounding=` from `_from_number` is caught by the rounding-mode ambients. Together these close the two "Survives" rows in the mutation table above. I did not edit the source, so these were checked by reading which G1 rows exercise each helper, not by running mutants.

### Gate
No blocking findings. T1 can proceed to merge. N1 should be fixed with a small change plus one test when convenient. It does not need another audit pass: re-run `tests/portfolio/test_money.py` after the change.
