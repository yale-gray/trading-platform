"""FastAPI app factory (``/api/v1``, JSON, no auth, bound to 127.0.0.1 by ``tradebot api serve``).

The API reads the runner's tables and runs backtests; it never imports broker/sim code and has
no order-placing endpoint. CORS is off.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from sqlalchemy import Engine, text

from tradebot import __version__
from tradebot.api.jobs import BacktestJobs, DataFactory, fail_stale_runs
from tradebot.api.routers import backtests, killswitch, portfolios, runner, strategies
from tradebot.api.schemas import Health
from tradebot.clock import Clock, SystemClock
from tradebot.config import Settings
from tradebot.db import repo
from tradebot.db.engine import create_all, make_engine

__all__ = ["create_app"]


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    data_factory: DataFactory | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the app. Tests pass their own ``engine`` (not disposed here), ``data_factory``
    and ``clock``; ``tradebot api serve`` passes nothing and gets the configured DB."""
    settings = settings or Settings()
    clk = clock or SystemClock()
    owns_engine = engine is None
    if engine is None:
        engine = make_engine(settings.db_path)
        create_all(engine)

    if data_factory is None:

        def data_factory() -> object:  # type: ignore[misc]
            from tradebot.data import MarketData
            from tradebot.data.config import DataConfig

            return MarketData(DataConfig(cache_dir=settings.cache_dir), clock=clk)

    fail_stale_runs(engine, repo.naive_utc(clk.now()))
    jobs = BacktestJobs(engine, data_factory, clk)  # type: ignore[arg-type]

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        jobs.shutdown()
        if owns_engine:
            engine.dispose()

    app = FastAPI(title="tradebot", version=__version__, lifespan=lifespan)
    app.state.engine, app.state.settings, app.state.clock, app.state.jobs = (
        engine,
        settings,
        clk,
        jobs,
    )

    api = APIRouter(prefix="/api/v1")

    @api.get("/health", response_model=Health, tags=["health"])
    def health() -> Health:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        return Health(status="ok" if ok else "degraded", db_ok=ok, version=__version__)

    for module in (runner, portfolios, killswitch, strategies, backtests):
        api.include_router(module.router)
    app.include_router(api)
    return app
