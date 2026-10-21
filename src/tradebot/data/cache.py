"""Immutable, generation-aware parquet bar cache (SPEC-001 Section 4).

Layout (read unchanged from the committed fixtures)::

    <root>/<source>/<SYMBOL_FS>/meta.json
    <root>/<source>/<SYMBOL_FS>/gen-0001/bars.parquet
    <root>/<source>/<SYMBOL_FS>/gen-0001/gen.json
    <root>/<source>/<SYMBOL_FS>/gen-0001/bars-<sha8>.parquet   (after an append)
    <root>/<source>/<SYMBOL_FS>/quarantine/<ts>.parquet

Reads never write. All writes go through temp file + ``os.replace``.

Crash safety: the published state of a generation is exactly its ``gen.json``.
``create``/``rebase`` write ``bars.parquet`` into a brand-new generation
directory (nothing is published until its ``gen.json`` lands). ``append``
writes the new rows to a content-addressed ``bars-<sha8>.parquet`` and then
publishes with ONE ``os.replace`` of ``gen.json``, which names the file
(``"file"``) and its full ``sha256``. A ``gen.json`` without ``"file"`` means
``bars.parquet`` (the committed fixture layout). Superseded bars files are
removed only after the publish. Writers hold a per-symbol ``flock``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tradebot.clock import Clock
from tradebot.data.errors import (
    CacheCorruptError,
    DataError,
    DataRevisionError,
    ImmutabilityError,
)
from tradebot.data.restatement import DEFAULT_TOL, ReconcileResult, reconcile_overlap
from tradebot.data.types import BAR_COLUMNS

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
REAL_BASIS_PREFIX = "adj_close_as_of_"
SYNTHETIC_BASIS_PREFIX = "synthetic"
SPOT_BASIS_PREFIX = "spot_"
DEFAULT_BARS_FILE = "bars.parquet"
LOCK_TIMEOUT_S = 30.0
AUDIT_COLUMNS: tuple[str, ...] = ("fetched_at", "source_rev")
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _fs_symbol(symbol: str) -> str:
    return symbol.replace("/", "_")


def _basis_ok(basis: str) -> bool:
    return basis.startswith((REAL_BASIS_PREFIX, SYNTHETIC_BASIS_PREFIX, SPOT_BASIS_PREFIX))


def _bars_name(info: dict[str, Any]) -> str:
    """File name holding a generation's bars (``bars.parquet`` when unnamed)."""
    name = str(info.get("file", DEFAULT_BARS_FILE))
    if name != Path(name).name or not name.endswith(".parquet"):
        raise CacheCorruptError(f"invalid bars file name in gen.json: {name!r}")
    return name


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, writer: Any) -> None:
    """Write via ``writer(tmp_path)`` to a temp file in the same dir, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        writer(tmp)
        with tmp.open("rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def _symbol_lock(sym_dir: Path, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Exclusive per-symbol writer lock (``flock`` on the symbol directory).

    Raises :class:`DataError` if another writer holds it for ``timeout`` seconds.
    """
    sym_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(sym_dir, os.O_RDONLY)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DataError(f"another writer holds the cache lock for {sym_dir}") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _atomic_json(path: Path, obj: dict[str, Any]) -> None:
    text = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    _atomic_write(path, lambda p: p.write_text(text, encoding="utf-8"))


def _check_bars(frame: pd.DataFrame) -> None:
    idx = frame.index
    if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None or str(idx.tz) != "UTC":
        raise DataError("bar index must be a tz-aware UTC DatetimeIndex")
    if idx.name != "ts":
        raise DataError("bar index must be named 'ts'")
    if not idx.is_monotonic_increasing or not idx.is_unique:
        raise DataError("bar index must be sorted ascending and unique")
    missing = [c for c in BAR_COLUMNS if c not in frame.columns]
    if missing:
        raise DataError(f"bar frame missing columns: {missing}")
    for c in BAR_COLUMNS:
        if frame[c].dtype != np.float64:
            raise DataError(f"column {c!r} must be float64")


class BarCache:
    """Append-only parquet cache with numbered generations."""

    def __init__(self, root: Path | str, clock: Clock) -> None:
        self.root = Path(root)
        self._clock = clock

    # ---- paths ----------------------------------------------------------
    def _sym_dir(self, source: str, symbol: str) -> Path:
        return self.root / source / _fs_symbol(symbol)

    def _gen_dir(self, source: str, symbol: str, gen: int) -> Path:
        return self._sym_dir(source, symbol) / f"gen-{gen:04d}"

    def _now_str(self) -> str:
        return self._clock.now().strftime(_TS_FMT)

    # ---- metadata -------------------------------------------------------
    def exists(self, source: str, symbol: str) -> bool:
        return (self._sym_dir(source, symbol) / "meta.json").is_file()

    def current_gen(self, source: str, symbol: str) -> int:
        meta = self._sym_dir(source, symbol) / "meta.json"
        if not meta.is_file():
            raise DataError(f"no cache for {source}/{symbol}")
        return int(json.loads(meta.read_text(encoding="utf-8"))["current_gen"])

    def generations(self, source: str, symbol: str) -> list[int]:
        d = self._sym_dir(source, symbol)
        if not d.is_dir():
            return []
        return sorted(int(p.name[4:]) for p in d.glob("gen-[0-9][0-9][0-9][0-9]") if p.is_dir())

    def gen_info(self, source: str, symbol: str, generation: int | None = None) -> dict[str, Any]:
        """Return the parsed ``gen.json`` for a generation (default current)."""
        gen = self.current_gen(source, symbol) if generation is None else generation
        path = self._gen_dir(source, symbol, gen) / "gen.json"
        if not path.is_file():
            raise DataError(f"no generation {gen} for {source}/{symbol}")
        info: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return info

    # ---- read -----------------------------------------------------------
    def read(
        self,
        source: str,
        symbol: str,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        generation: int | None = None,
        *,
        include_audit: bool = False,
    ) -> pd.DataFrame:
        """Read bars (inclusive ``start``/``end``), verifying the sha256.

        Audit columns are stripped unless ``include_audit``. Never writes.
        """
        gen = self.current_gen(source, symbol) if generation is None else generation
        gdir = self._gen_dir(source, symbol, gen)
        for attempt in (0, 1):
            info = self.gen_info(source, symbol, gen)
            bars = gdir / _bars_name(info)
            if bars.is_file():
                break
            if attempt:
                raise CacheCorruptError(f"missing {bars}")
            # a concurrent append may have published and swept this file; re-read gen.json
        if _sha256_file(bars) != info["sha256"]:
            raise CacheCorruptError(f"sha256 mismatch for {bars}")
        frame = pd.read_parquet(bars)
        if start is not None:
            frame = frame.loc[frame.index >= start]
        if end is not None:
            frame = frame.loc[frame.index <= end]
        cols = list(BAR_COLUMNS) + (list(AUDIT_COLUMNS) if include_audit else [])
        return frame[cols].copy()

    # ---- write ----------------------------------------------------------
    def _prepare(self, frame: pd.DataFrame, source_rev: str) -> pd.DataFrame:
        _check_bars(frame)
        out = frame[[*BAR_COLUMNS]].copy()
        now = pd.Timestamp(self._clock.now()).tz_convert("UTC").as_unit("ms")
        if "fetched_at" in frame.columns:
            out["fetched_at"] = frame["fetched_at"]
        else:
            out["fetched_at"] = pd.Series(now, index=out.index).astype("datetime64[ms, UTC]")
        if "source_rev" in frame.columns:
            out["source_rev"] = frame["source_rev"]
        else:
            out["source_rev"] = source_rev
        return out

    def _commit_generation(
        self,
        source: str,
        symbol: str,
        gen: int,
        data: pd.DataFrame,
        info_base: dict[str, Any],
        *,
        switch_current: bool,
        content_addressed: bool = False,
    ) -> None:
        gdir = self._gen_dir(source, symbol, gen)
        info = {k: v for k, v in info_base.items() if k != "file"}
        info["first"] = data.index[0].strftime("%Y-%m-%d")
        info["last"] = data.index[-1].strftime("%Y-%m-%d")
        if content_addressed:
            gdir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=gdir, prefix=".bars.", suffix=".tmp")
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                data.to_parquet(tmp, compression="snappy")
                with tmp.open("rb") as fh:
                    os.fsync(fh.fileno())
                sha = _sha256_file(tmp)
                name = f"bars-{sha[:8]}.parquet"
                os.replace(tmp, gdir / name)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            info["file"] = name
            info["sha256"] = sha
        else:
            bars = gdir / DEFAULT_BARS_FILE
            _atomic_write(bars, lambda p: data.to_parquet(p, compression="snappy"))
            info["sha256"] = _sha256_file(bars)
        # The single publish point: one atomic replace of gen.json.
        _atomic_json(gdir / "gen.json", info)
        if content_addressed:
            keep = info["file"]
            for stale in [*gdir.glob("bars-*.parquet"), gdir / DEFAULT_BARS_FILE]:
                if stale.name != keep:
                    stale.unlink(missing_ok=True)
        if switch_current:
            _atomic_json(
                self._sym_dir(source, symbol) / "meta.json",
                {"current_gen": gen, "schema": SCHEMA_VERSION, "tf": "1d"},
            )

    def create(
        self, source: str, symbol: str, frame: pd.DataFrame, basis: str, source_rev: str = "unknown"
    ) -> int:
        """Create generation 1 for a new symbol. Refuses if a cache already exists."""
        if self.exists(source, symbol):
            raise ImmutabilityError(f"cache already exists for {source}/{symbol}; use append")
        return self._new_generation(source, symbol, frame, basis, source_rev)

    def rebase(
        self, source: str, symbol: str, frame: pd.DataFrame, basis: str, source_rev: str = "unknown"
    ) -> int:
        """Write a full re-fetch as a new generation and make it current."""
        if not self.exists(source, symbol):
            raise DataError(f"no cache for {source}/{symbol}; use create")
        return self._new_generation(source, symbol, frame, basis, source_rev)

    def _new_generation(
        self, source: str, symbol: str, frame: pd.DataFrame, basis: str, source_rev: str
    ) -> int:
        if not _basis_ok(basis):
            raise DataError(
                f"basis {basis!r} must start with {REAL_BASIS_PREFIX!r}, "
                f"{SPOT_BASIS_PREFIX!r} or {SYNTHETIC_BASIS_PREFIX!r}"
            )
        if frame.empty:
            raise DataError("cannot create a generation from an empty frame")
        data = self._prepare(frame, source_rev)
        with _symbol_lock(self._sym_dir(source, symbol)):
            existing = self.generations(source, symbol)
            gen = (existing[-1] + 1) if existing else 1
            self._commit_generation(
                source,
                symbol,
                gen,
                data,
                {"basis": basis, "created": self._now_str(), "events": []},
                switch_current=True,
            )
        return gen

    def append(
        self,
        source: str,
        symbol: str,
        frame: pd.DataFrame,
        source_rev: str = "unknown",
        *,
        event: dict[str, Any] | None = None,
    ) -> int:
        """Append rows to the current generation; returns the number of rows added.

        Rows whose timestamp is already cached must be identical (they are
        skipped); any difference raises :class:`ImmutabilityError`. New rows
        must lie strictly after the last or before the first cached bar.
        """
        new = self._prepare(frame, source_rev)
        with _symbol_lock(self._sym_dir(source, symbol)):
            return self._append_locked(source, symbol, new, event)

    def _append_locked(
        self, source: str, symbol: str, new: pd.DataFrame, event: dict[str, Any] | None
    ) -> int:
        gen = self.current_gen(source, symbol)
        old = self.read(source, symbol, generation=gen, include_audit=True)

        shared = old.index.intersection(new.index)
        if len(shared):
            a = old.loc[shared, list(BAR_COLUMNS)].to_numpy()
            b = new.loc[shared, list(BAR_COLUMNS)].to_numpy()
            if not np.array_equal(a, b):
                raise ImmutabilityError(
                    f"{source}/{symbol}: write would alter existing rows "
                    f"({len(shared)} overlapping dates differ)"
                )
        fresh = new.loc[~new.index.isin(old.index)]
        if fresh.empty:
            return 0
        interior = (fresh.index > old.index[0]) & (fresh.index < old.index[-1])
        if interior.any():
            raise ImmutabilityError(f"{source}/{symbol}: write would insert rows inside history")

        combined = pd.concat([old, fresh]).sort_index()
        combined.index.name = "ts"
        info = self.gen_info(source, symbol, gen)
        events = list(info.get("events", []))
        if event is not None:
            events.append(event)
        base = {k: v for k, v in info.items() if k not in ("first", "last", "sha256", "file")}
        base["events"] = events
        self._commit_generation(
            source, symbol, gen, combined, base, switch_current=False, content_addressed=True
        )
        return len(fresh)

    # ---- reconcile + append --------------------------------------------
    def quarantine(self, source: str, symbol: str, frame: pd.DataFrame) -> Path:
        """Persist a rejected fetch under ``<symbol>/quarantine/<ts>.parquet``."""
        stamp = self._clock.now().strftime("%Y%m%dT%H%M%S%fZ")
        path = self._sym_dir(source, symbol) / "quarantine" / f"{stamp}.parquet"
        _atomic_write(path, lambda p: frame.to_parquet(p, compression="snappy"))
        return path

    def apply_fetch(
        self,
        source: str,
        symbol: str,
        fetched: pd.DataFrame,
        source_rev: str = "unknown",
        *,
        overlap_bars: int = 10,
        tol: float = DEFAULT_TOL,
    ) -> ReconcileResult:
        """Reconcile a fetch (overlap window + new bars) against the cache and append.

        On a revision the fetch is quarantined, :class:`DataRevisionError` is
        raised and nothing is appended.
        """
        cached_tail = self.read(source, symbol).tail(overlap_bars)
        try:
            result = reconcile_overlap(cached_tail, fetched, tol)
        except DataRevisionError:
            path = self.quarantine(source, symbol, fetched)
            log.error("%s/%s: revision detected; fetch quarantined at %s", source, symbol, path)
            raise
        event = None
        if result.kind == "restatement":
            event = {
                "restated_at": self._now_str(),
                "ratio": result.ratio,
                "n_overlap": result.n_overlap,
            }
            log.warning("%s/%s: pure restatement, ratio %.10f", source, symbol, result.ratio)
        if result.new_rows.empty and event is None:
            return result
        if result.new_rows.empty:
            self._record_event(source, symbol, event)
        else:
            self.append(source, symbol, result.new_rows, source_rev, event=event)
        return result

    def _record_event(self, source: str, symbol: str, event: dict[str, Any] | None) -> None:
        with _symbol_lock(self._sym_dir(source, symbol)):
            gen = self.current_gen(source, symbol)
            info = self.gen_info(source, symbol, gen)
            info["events"] = [*info.get("events", []), event]
            _atomic_json(self._gen_dir(source, symbol, gen) / "gen.json", info)

    # ---- fingerprint ----------------------------------------------------
    def fingerprint(
        self,
        entries: Iterable[tuple[str, str]],
        start: datetime | pd.Timestamp | None = None,
        end: datetime | pd.Timestamp | None = None,
        generation: int | None = None,
    ) -> str:
        """Hash (source, symbol, generation id, sha256) for each entry, plus the window."""
        lines = []
        for source, symbol in sorted(set(entries)):
            gen = self.current_gen(source, symbol) if generation is None else generation
            info = self.gen_info(source, symbol, gen)
            lines.append(f"{source}|{symbol}|{gen}|{info['sha256']}")
        lines.append(
            f"window|{start.isoformat() if start else ''}|{end.isoformat() if end else ''}"
        )
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()
