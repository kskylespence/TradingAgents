"""SPA history fallback for the React build.

`StaticFiles(html=True)` does NOT give a single-page app what it needs for
client-side routing, despite the name suggesting otherwise. `html=True` does
exactly two things: serve `<dir>/index.html` on a *directory* hit, and serve
`404.html` (as a 404) when a file is missing. It never rewrites an unknown
path to `index.html`.

The practical effect: in-app navigation worked, because React Router handles
it client-side and never asks the server. But a hard load, refresh, bookmark,
or shared link on `/new` asked the server for a file named `new`, which does
not exist, and got a 404 — the browser then reported
`new:1 Failed to load resource: the server responded with a status of 404`.

`SPAStaticFiles` adds the missing history fallback, with two deliberate
carve-outs so the fallback cannot mask real errors:

- `/api/*` never falls back. An API 404 returning 200 + HTML would make a
  client's `fetch().json()` fail on a parse error instead of surfacing the
  404 it actually received.
- Paths whose final segment contains a dot (`/assets/main.js`) never fall
  back. Returning HTML for a missing script makes the browser complain about
  MIME type rather than the real problem — a bad asset path.
"""

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


def _is_client_side_route(path: str) -> bool:
    """True when `path` should fall back to the SPA shell.

    Starlette hands `get_response` a normalised, leading-slash-stripped path
    (`/runs/abc` arrives as `runs/abc`, `/` arrives as `.`).
    """
    if path == "api" or path.startswith("api/"):
        return False
    return "." not in path.rsplit("/", 1)[-1]


class SPAStaticFiles(StaticFiles):
    """StaticFiles that serves `index.html` for unmatched client-side routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404 and _is_client_side_route(path):
                return await super().get_response("index.html", scope)
            raise

        # Starlette returns (rather than raises) a 404 when a `404.html`
        # exists in the static root, so cover that path too.
        if response.status_code == 404 and _is_client_side_route(path):
            return await super().get_response("index.html", scope)
        return response
