"""The voice demo's server: the page, the Realtime client secret, and the orchestrator tool.

Usage:
    cp .env.example .env  # fill in OPENAI_API_KEY, MW_BASE_URL, MW_BOT_NAME, and your credential
    python -m examples.voice            # then open http://127.0.0.1:8095
    python -m examples.voice --port 9000

The browser runs the voice harness: it talks to the OpenAI Realtime model over
WebRTC and, when the model calls ``ask_orchestrator``, asks this server to run
the turn. The server holds every secret: the OpenAI API key (the browser gets
a short-lived client secret instead) and the Conversations API credential.

- ``POST /api/session``: mint a Realtime client secret for the voice harness's
  session config and start a voice session (one Conversations API
  conversation).
- ``POST /api/warmup``: ask the orchestrator who the caller is, for the voice
  model's background context.
- ``POST /api/ask``: run one ``ask_orchestrator`` call. Streams NDJSON:
  ``progress`` lines while the orchestrator works, then the ``result``.
- ``POST /api/end``: drop the voice session.

The server binds to 127.0.0.1 and accepts only local ``Host`` headers and
same-origin JSON writes, because anything that can reach it acts as your
credential. Do not expose it on a network.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import io
import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterator, Optional, Tuple

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from moveworks_capi.auth import ApiKeyBearer, S2SJwtBearer
from moveworks_capi.client import BearerProvider, ConversationsApiClient

from .orchestrator import DEFAULT_TURN_TIMEOUT_SECONDS, Orchestrator, Sessions
from .realtime import CALLS_URL, RealtimeError, create_client_secret, session_config
from .tuning import Tuning

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
LOCAL_HOSTNAMES = ["127.0.0.1", "localhost"]
STATIC_DIR = Path(__file__).parent / "static"
REVALIDATE = {"Cache-Control": "no-cache"}


class SessionRef(BaseModel):
    session_id: str


class AskIn(BaseModel):
    session_id: str
    query: str = ""
    choice: Optional[str] = None


def create_app(
    *,
    openai_api_key: str,
    tuning: Tuning,
    orchestrator: Orchestrator,
    user: str,
    sessions: Optional[Sessions] = None,
    http: Optional[requests.Session] = None,
) -> FastAPI:
    """Build the app. ``user`` is shown on the page as the employee the session acts as."""
    voice_sessions = sessions if sessions is not None else Sessions()
    app = FastAPI(title="Moveworks Conversations API: voice demo")

    # A page on another site can reach 127.0.0.1 through DNS rebinding or a
    # simple cross-site POST. Both would act with your credentials, so accept
    # only local Host headers, and only JSON writes from this origin.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTNAMES)

    @app.middleware("http")
    async def same_origin_json_writes(request: Request, call_next):
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host', '')}":
                return JSONResponse({"detail": "Cross-origin request refused."}, status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "Send JSON."}, status_code=415)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def session_or_404(session_id: str):
        session = voice_sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="That voice session has ended. Connect again.")
        return session

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers=REVALIDATE)

    @app.post("/api/session")
    async def start_session() -> Dict[str, Any]:
        try:
            secret = await asyncio.to_thread(create_client_secret, openai_api_key, session_config(tuning), http=http)
        except (RealtimeError, requests.RequestException) as exc:
            log.error("could not start a Realtime session: %s", exc)
            raise HTTPException(status_code=502, detail=f"Could not start the voice model: {exc}") from exc
        session = voice_sessions.new()
        return {
            "session_id": session.session_id,
            "client_secret": secret["value"],
            "expires_at": secret.get("expires_at"),
            "calls_url": CALLS_URL,
            "model": tuning.model,
            "user": user,
            "tuning": tuning.client_view(),
        }

    @app.post("/api/warmup")
    async def warmup(body: SessionRef) -> Dict[str, str]:
        session = session_or_404(body.session_id)
        if not tuning.warmup:
            return {"context": ""}
        context = await asyncio.to_thread(orchestrator.warm_up, session, tuning.warmup)
        return {"context": context}

    @app.post("/api/ask")
    async def ask(body: AskIn) -> StreamingResponse:
        session = session_or_404(body.session_id)
        if not body.query.strip() and not body.choice:
            raise HTTPException(status_code=400, detail="Send a query.")
        events = orchestrator.ask(session, body.query, choice=body.choice)
        return StreamingResponse(_ndjson(events), media_type="application/x-ndjson", headers=REVALIDATE)

    @app.post("/api/end")
    async def end(body: SessionRef) -> Dict[str, bool]:
        voice_sessions.end(body.session_id)
        return {"ended": True}

    return app


async def _ndjson(events: Iterator[Dict[str, Any]]) -> AsyncIterator[bytes]:
    """Stream a blocking event iterator as NDJSON, drained on a worker thread.

    When the browser stops reading (the caller barged in and the voice harness
    cancelled the lookup), the iterator is closed at its next event, which
    closes the Conversations API stream.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    done = object()

    def put(item: object) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            stop.set()  # the event loop is gone

    def produce() -> None:
        try:
            for event in events:
                if stop.is_set():
                    break
                put(event)
        except Exception:
            log.exception("orchestrator events failed")
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                close()
            put(done)

    loop.run_in_executor(None, produce)
    try:
        while True:
            item = await queue.get()
            if item is done:
                return
            yield (json.dumps(item) + "\n").encode("utf-8")
    finally:
        stop.set()


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def _bearer(base_url: str) -> Tuple[BearerProvider, str]:
    """The Conversations API credential from ``MW_AUTH``, and who it acts as."""
    mode = os.environ.get("MW_AUTH", "s2s").lower()
    if mode == "s2s":
        user = _required("MW_S2S_USER_EMAIL")
        bearer = S2SJwtBearer(
            base_url=base_url,
            issuer=_required("MW_S2S_ISSUER"),
            audience=_required("MW_S2S_AUDIENCE"),
            key_id=_required("MW_S2S_KEY_ID"),
            private_key=Path(_required("MW_S2S_PRIVATE_KEY_PATH")),
            user_identifier=user,
        )
        return bearer, user
    if mode == "api-key":
        return ApiKeyBearer(_required("MW_API_KEY")), "the API key's user"
    raise SystemExit(f"MW_AUTH must be s2s or api-key for the voice demo (got {mode!r})")


def build_from_env() -> FastAPI:
    """The app, configured from environment variables (see the README)."""
    base_url = os.environ.get("MW_BASE_URL", "https://api.moveworks.ai")
    bearer, user = _bearer(base_url)
    client = ConversationsApiClient(base_url=base_url, bot_name=_required("MW_BOT_NAME"), bearer_provider=bearer)
    stream = os.environ.get("VOICE_STREAM", "true").strip().lower() not in ("0", "false", "no", "off")
    orchestrator = Orchestrator(
        client,
        stream=stream,
        timeout_seconds=float(os.environ.get("VOICE_TURN_TIMEOUT_SECONDS") or DEFAULT_TURN_TIMEOUT_SECONDS),
    )
    return create_app(openai_api_key=_required("OPENAI_API_KEY"), tuning=Tuning.load(), orchestrator=orchestrator, user=user)


def main(argv: Optional[list] = None) -> None:
    import uvicorn
    from dotenv import find_dotenv, load_dotenv

    parser = argparse.ArgumentParser(prog="python -m examples.voice", description="Voice demo for the Conversations API")
    parser.add_argument("--port", type=int, default=int(os.environ.get("VOICE_PORT", "8095")))
    args = parser.parse_args(argv)

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(find_dotenv(usecwd=True))
    app = build_from_env()

    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((HOST, args.port))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        sys.exit(f"Port {args.port} is already in use. Stop the other process, or pass --port to choose another.")

    log.info("voice demo on http://%s:%d", HOST, args.port)
    uvicorn.run(app, host=HOST, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
