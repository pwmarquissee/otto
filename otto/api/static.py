"""The dashboard itself: index.html, the /static mount, and the no-cache rule.

Moved out of otto/daemon.py as it was. `install(app)` is called last by
otto/api/__init__.py so the revalidate middleware sits where it always did,
outermost on the stack.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def dashboard():
    index = WEB_DIR / "index.html"
    if not index.is_file():
        return JSONResponse({"error": "dashboard assets missing", "expected": str(index)}, 500)
    # no-cache means "revalidate every time", not "do not store": the ETag makes
    # that one cheap loopback round trip. Without it WebView2 applied heuristic
    # freshness to a file that had not changed in weeks and kept serving an
    # index.html from before term.js existed, while the freshly modified app.js
    # DID revalidate. The page then died on `OttoTerm is not defined` and the
    # desktop app blamed the daemon (2026-10-02).
    return FileResponse(str(index), headers={"Cache-Control": "no-cache"})

async def _revalidate_static(request: Request, call_next):
    """Same rule for everything under /static: the files change whenever the repo
    does, and a dashboard half on new code and half on old is worse than a 304."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def install(app: FastAPI) -> None:
    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    app.get("/")(dashboard)
    app.middleware("http")(_revalidate_static)
