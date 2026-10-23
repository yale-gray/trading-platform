"""The built web app is mounted at ``/`` without shadowing ``/api/v1`` (offline, fast)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tradebot.api.app import create_app

pytestmark = pytest.mark.unit


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA-ROOT</html>")
    (dist / "assets" / "a.js").write_text("console.log(1)")
    return dist


def test_mount_serves_spa_and_api_wins(db: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(engine=db, web_dist=_dist(tmp_path))) as c:
        assert "SPA-ROOT" in c.get("/").text
        assert c.get("/assets/a.js").text == "console.log(1)"
        assert "SPA-ROOT" in c.get("/investing/deep/link").text  # client-side route fallback
        r = c.get("/api/v1/health")
        assert r.json()["status"] == "ok"
        assert c.get("/api/v1/nope").status_code == 404  # not swallowed by the SPA
        assert c.get("/api/v1/nope").headers["content-type"].startswith("application/json")


def test_no_mount_without_dist(db: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(engine=db, web_dist=tmp_path / "missing")) as c:
        assert c.get("/").status_code == 404
        assert c.get("/api/v1/health").status_code == 200


def test_unknown_api_post_is_404_not_405(db: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(engine=db, web_dist=_dist(tmp_path))) as c:
        assert c.post("/api/v1/portfolios/1/kill-switch/reset", json={}).status_code == 404
