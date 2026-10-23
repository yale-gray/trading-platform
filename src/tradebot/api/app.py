"""FastAPI app factory (``/api/v1``, JSON, no auth, bound to 127.0.0.1 by ``tradebot api serve``).

The API reads the runner's tables and runs backtests; it never imports broker/sim code and has
no order-placing endpoint. CORS is off.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, text
from starlette.exceptions import HTTPException as StarletteHTTPException

from tradebot import __version__
from tradebot.api.jobs import BacktestJobs, DataFactory, fail_stale_runs
from tradebot.api.routers import backtests, killswitch, portfolios, runner, strategies
from tradebot.api.schemas import Health
from tradebot.clock import Clock, SystemClock
from tradebot.config import Settings
from tradebot.db import repo
from tradebot.db.engine import create_all, make_engine

__all__ = ["create_app"]


DEFAULT_WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
"""Where ``npm run build`` in ``web/`` puts the single-page app."""


class SpaFiles(StaticFiles):
    """Static files with an SPA fallback: unknown non-``api`` paths get ``index.html``."""

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        """Unknown ``/api`` paths stay a plain 404 for any method (never a 405 from the files)."""
        if scope["type"] == "http" and scope["path"].startswith("/api"):
            raise StarletteHTTPException(404)
        await super().__call__(scope, receive, send)

    async def get_response(self, path, scope):  # type: ignore[no-untyped-def]
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith("api/") or path == "api":
                raise
            return await super().get_response("index.html", scope)


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    data_factory: DataFactory | None = None,
    clock: Clock | None = None,
    web_dist: Path | None = None,
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
    dist = web_dist if web_dist is not None else DEFAULT_WEB_DIST
    if (dist / "index.html").is_file():  # mounted last so every /api/v1 route wins
        app.mount("/", SpaFiles(directory=dist, html=True), name="web")
    return app
