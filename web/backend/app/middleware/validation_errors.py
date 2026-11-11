"""Drop the rejected value from every 422 validation error.

FastAPI's default ``RequestValidationError`` response is
``{"detail": [{"type", "loc", "msg", "input", "ctx"?}, ...]}``, where
``input`` is the value the client sent. For a password or an API key that
hands the secret back in the response body, and to any proxy or APM tool
that records error bodies. ``loc`` and ``msg`` still say what was wrong,
which is all a client needs; the frontend never reads ``input``.

Installed through the middleware registry because the registry is the
supported hook that receives the FastAPI app (``main.py`` stays untouched).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import register


async def _validation_error_without_input(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = [{k: v for k, v in err.items() if k != "input"} for err in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


def install(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_error_without_input)


register(install)
