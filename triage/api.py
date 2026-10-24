"""HTTP face of the watchlist pipeline (PIPELINE.md section 9): the dashboard, the extension's
API and the live feed. Loopback only, with no authentication, so every POST (the only routes
that reach Mubit or Wikipedia) must come from an allowed Origin, and CORS answers only those."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .server import LOOPBACK_HOSTS, _feed
from .watch import Refused, Service

PAGE = Path(__file__).parent / "static" / "watch.html"
PORT = 8322
ORIGINS = [f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"]


def _found(x, what: str):
    if x is None:
        raise HTTPException(404, what)
    return x


def create_app(
    svc: Service, allowed_hosts: list[str] = LOOPBACK_HOSTS, origins: list[str] = ORIGINS
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        svc.start(asyncio.get_running_loop())
        yield
        svc.stop()

    app = FastAPI(title="Mubit watchlist", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=list(origins), allow_methods=["GET", "POST"])
    # uvicorn does not check the Host header: without this a DNS-rebinding page could reach it.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.exception_handler(Refused)
    def refused(_: Request, e: Refused) -> JSONResponse:
        return JSONResponse({"reason": e.reason}, status_code=e.status)

    _reads(app, svc)
    _posts(app, svc, origins)
    return app


def _posts(app: FastAPI, svc: Service, origins: list[str]) -> None:
    """The only routes that reach Mubit or Wikipedia."""

    def allowed_origin(request: Request) -> None:
        if request.headers.get("origin") not in origins:
            raise HTTPException(403, "a POST needs an allowed Origin header")

    def post(path: str, **kw):
        return app.post(path, dependencies=[Depends(allowed_origin)], **kw)

    @post("/api/page/refresh")
    def refresh(title: str) -> dict:
        return svc.refresh(title)

    @post("/api/page/brief")
    def brief_generate(title: str) -> dict:
        return svc.brief(title)

    @post("/api/page/classify", status_code=202)
    def classify(title: str) -> dict:
        return svc.classify(title)


def _reads(app: FastAPI, svc: Service) -> None:
    """Disk and the call log only."""

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return PAGE.read_text()

    @app.get("/api/watchlist")
    def watchlist() -> list[dict]:
        return svc.watchlist_rows()

    @app.get("/api/page")
    def page_analysis(title: str) -> dict:
        return _found(svc.page(title), f"{title} is not ingested")

    @app.get("/api/page/brief")
    def brief(title: str) -> dict:
        return _found(svc.cached_brief(title), f"no brief for {title}")

    @app.get("/api/edit/{revid}")
    def edit(revid: int) -> dict:
        return _found(svc.edit(revid), f"revision {revid} is neither ingested nor logged")

    @app.get("/api/live")
    async def live() -> StreamingResponse:
        return StreamingResponse(
            _feed(svc.live),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/mubit/status")
    def status() -> dict:
        return svc.status()

    @app.get("/api/cost")
    def cost() -> dict:
        return svc.cost()


def serve(svc: Service, host: str, port: int, origins: list[str]) -> None:
    """`host` is loopback (the CLI refuses any other)."""
    uvicorn.run(create_app(svc, [*LOOPBACK_HOSTS, host], origins), host=host, port=port, log_level="warning")
