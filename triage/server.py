"""HTTP face of the patrol desk: the page, a server-sent-events feed, and the
two actions a patroller can take (a verdict, and a hand-typed Mubit console status)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .patrol import Patrol

PAGE = Path(__file__).parent / "static" / "patrol.html"
PING_S = 15
# Host header values a loopback desk answers to (Starlette keeps the brackets on IPv6).
LOOPBACK_HOSTS = ["localhost", "127.0.0.1", "[::1]"]


class Verdict(BaseModel):
    revid: int
    verdict: Literal["keep", "revert"]


class Status(BaseModel):
    text: str


def _sse(kind: str, data: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n"


async def _feed(patrol: Patrol) -> AsyncIterator[str]:
    """A snapshot, then every broadcast, with a comment line every PING_S to keep proxies awake."""
    q = patrol.subscribe()
    try:
        yield _sse("snapshot", await asyncio.to_thread(patrol.snapshot))
        while True:
            try:
                kind, data = await asyncio.wait_for(q.get(), PING_S)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            yield _sse(kind, data)
    finally:
        patrol.unsubscribe(q)


def create_app(patrol: Patrol, allowed_hosts: list[str] = LOOPBACK_HOSTS) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        patrol.start(asyncio.get_running_loop())
        yield
        patrol.stop()

    app = FastAPI(title="Mubit patrol desk", lifespan=lifespan)
    # uvicorn does not check the Host header, so a DNS-rebinding page could otherwise
    # reach a loopback desk and POST /verdict or /status.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return PAGE.read_text()

    @app.get("/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(
            _feed(patrol),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/verdict")
    def verdict(v: Verdict) -> dict:
        try:
            return patrol.verdict(v.revid, v.verdict)
        except KeyError:
            raise HTTPException(404, f"edit {v.revid} is no longer on the desk") from None
        except ValueError:
            raise HTTPException(409, f"edit {v.revid} already has a verdict") from None

    @app.get("/stats")
    def stats() -> dict:
        return patrol.stats()

    @app.post("/status")
    def status(s: Status) -> dict:
        return patrol.set_status(s.text)

    return app


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(patrol: Patrol, host: str, port: int, allow_remote: bool = False) -> None:
    """The desk has no authentication: /verdict spends Mubit credit and writes guideline
    lines that reach every later prompt, so it binds to loopback unless told otherwise."""
    if not (allow_remote or is_loopback(host)):
        raise SystemExit(
            f"refusing to serve the patrol desk on {host}: it has no authentication. "
            "Bind to 127.0.0.1, or pass --allow-remote if the network is trusted."
        )
    hosts = ["*"] if allow_remote else [*LOOPBACK_HOSTS, host]
    uvicorn.run(create_app(patrol, hosts), host=host, port=port, log_level="warning")
