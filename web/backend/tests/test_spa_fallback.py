"""SPA history fallback for client-side routes.

`StaticFiles(html=True)` only serves `index.html` on *directory* hits; it has
no history fallback, so a hard load or refresh of a client-side route like
`/new` returned 404 ({"detail":"Not Found"}) while in-app navigation worked
(React Router never touches the server). See `app/spa.py`.

These tests build their own static directory rather than using
`app/static/` — the real build output is gitignored, so it is absent in CI.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.spa import SPAStaticFiles


@pytest.fixture
def spa_client(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><div id='root'></div>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('app')")

    app = FastAPI()

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    app.mount(
        "/",
        SPAStaticFiles(directory=str(tmp_path), html=True),
        name="static",
    )
    return TestClient(app)


def test_client_side_route_serves_index_html(spa_client):
    """The reported bug: refreshing on /new 404'd instead of booting the SPA."""
    response = spa_client.get("/new")

    assert response.status_code == 200
    assert "<div id='root'></div>" in response.text


def test_nested_client_side_route_serves_index_html(spa_client):
    """/runs/:runId is a nested route — the fallback must not stop at depth 1."""
    response = spa_client.get("/runs/abc123")

    assert response.status_code == 200
    assert "<div id='root'></div>" in response.text


def test_root_still_serves_index_html(spa_client):
    response = spa_client.get("/")

    assert response.status_code == 200
    assert "<div id='root'></div>" in response.text


def test_existing_asset_is_still_served(spa_client):
    response = spa_client.get("/assets/app.js")

    assert response.status_code == 200
    assert "console.log('app')" in response.text


def test_missing_asset_still_404s(spa_client):
    """A missing .js must 404, not return HTML.

    Serving index.html for a missing script makes the browser report a MIME
    type error instead of the real problem (a bad asset path), which is
    materially harder to diagnose than a plain 404.
    """
    response = spa_client.get("/assets/missing.js")

    assert response.status_code == 404


def test_app_mounts_spa_static_files():
    """The wiring: create_app() must use SPAStaticFiles, not plain StaticFiles.

    Without this the class above is dead code — the fallback only reaches
    production through the mount in `app/main.py`.
    """
    from starlette.routing import Mount

    from app.main import create_app

    app = create_app()
    static_mounts = [
        route
        for route in app.routes
        if isinstance(route, Mount) and route.name == "static"
    ]

    assert static_mounts, "no static mount found on the app"
    assert isinstance(static_mounts[0].app, SPAStaticFiles)


def test_unknown_api_path_still_404s(spa_client):
    """API 404s must stay JSON — never fall through to the SPA shell.

    An /api/* path that returns 200 + HTML would make a client's fetch()
    fail on JSON parse rather than surface the 404 it actually got.
    """
    response = spa_client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert "<div id='root'></div>" not in response.text
