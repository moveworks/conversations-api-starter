"""The webhooks Twilio calls during a phone call.

Usage:
    cp .env.example .env  # fill in MW_*, TWILIO_AUTH_TOKEN, VOICE_PUBLIC_URL, VOICE_CALLERS
    python -m examples.voice

A call moves through these endpoints. Each answers with TwiML, and every
request must carry a valid ``X-Twilio-Signature``.

- ``POST /voice/incoming``: a call arrives. Look up the caller, start a
  session, greet, and listen (``<Gather input="speech">``).
- ``POST /voice/turn``: Twilio posts what the caller said (``SpeechResult``).
  Start the Conversations API turn on a worker thread, say a short filler,
  and redirect to ``/voice/wait``.
- ``POST /voice/wait``: wait a few seconds for the turn. When it is done,
  speak the first chunk of the answer and listen again; otherwise say
  "still working" now and then and redirect back here. Twilio stops waiting
  for a webhook after about 15 seconds, so a long turn spans several
  requests instead of holding one open.
- ``POST /voice/reprompt``: the caller said nothing. Ask again, then hang up.
- ``POST /voice/status``: Twilio's call status callback. A finished call's
  session is dropped.

See examples/voice/README.md for setup.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Dict, Optional
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from moveworks_capi.auth import ApiKeyBearer, S2SJwtBearer
from moveworks_capi.client import ConversationsApiClient

from .bridge import (
    DEFAULT_TURN_TIMEOUT_SECONDS,
    CallerDirectory,
    CallSession,
    CallSessions,
    ClientPool,
    Reply,
    TurnRunner,
    mask_number,
)
from .speech import MAX_SPEECH_CHARS, chunk_for_speech, match_option, wants_agent, wants_more, wants_repeat
from .twiml import TwiML, is_valid_signature

log = logging.getLogger(__name__)

INCOMING = "/voice/incoming"
TURN = "/voice/turn"
WAIT = "/voice/wait"
REPROMPT = "/voice/reprompt"
STATUS = "/voice/status"

GREETING = "Hi, this is the employee help line. What can I help you with?"
UNKNOWN_CALLER = "Sorry, this line doesn't recognize the number you're calling from. Goodbye."
EXPIRED = "Sorry, this call has timed out. Please call again."
DIDNT_CATCH = "Sorry, I didn't catch that. What can I help you with?"
STILL_THERE = "Is there anything else I can help you with?"
GOODBYE = "Thanks for calling. Goodbye."
FILLERS = ("One moment.", "Let me check.", "Okay, one moment.")
STILL_WORKING = "Still working on it."
MORE_PROMPT = "Say more to keep going, or ask me something else."
LINK_NOTE = "There's also a link or a form for this that I can't open on a call."
TRANSFERRING = "Okay, transferring you to an agent."
GOODBYE_WORDS = frozenset({"bye", "goodbye", "bye bye", "that's all", "i'm done", "nothing else"})
DECLINE_WORDS = frozenset({"no", "no thanks", "no thank you", "nope", "that's it"})
MAX_SPOKEN_OPTIONS = 4
MAX_SILENT_PROMPTS = 2
FINISHED_CALL_STATUSES = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})


@dataclass(frozen=True)
class VoiceSettings:
    """Settings for the Twilio side of the bridge.

    Args:
        twilio_auth_token: The Twilio account's auth token, for signatures.
        public_url: The public base URL Twilio calls, such as
            ``https://abc123.ngrok-free.app``. Signatures cover the URL
            Twilio requested, which a tunnel or proxy hides from the server.
        language: Speech recognition and text-to-speech language.
        voice: A Twilio ``<Say>`` voice, such as ``Polly.Joanna``. Empty
            uses the account default.
        speech_hints: Comma-separated words and phrases to bias speech
            recognition, such as product and system names.
        greeting: What the caller hears first.
        transfer_number: When set, a caller who asks for an agent is
            transferred here with ``<Dial>``.
        max_speech_chars: The longest stretch read before offering "more".
        wait_slice_seconds: How long one ``/voice/wait`` request waits for the
            turn. Keep it well under Twilio's 15-second webhook timeout.
        still_working_every: Say "still working" on every Nth wait request.
    """

    twilio_auth_token: str
    public_url: str
    language: str = "en-US"
    voice: str = ""
    speech_hints: str = ""
    greeting: str = GREETING
    transfer_number: str = ""
    max_speech_chars: int = MAX_SPEECH_CHARS
    wait_slice_seconds: float = 4.0
    still_working_every: int = 3


def create_app(
    settings: VoiceSettings,
    *,
    directory: CallerDirectory,
    clients: ClientPool,
    runner: TurnRunner,
    executor: Optional[Executor] = None,
    sessions: Optional[CallSessions] = None,
) -> FastAPI:
    """Build the webhook app. Pass ``executor`` and ``sessions`` to share or test them."""
    pool = executor if executor is not None else ThreadPoolExecutor(max_workers=8, thread_name_prefix="voice-turn")
    calls = sessions if sessions is not None else CallSessions()
    public_url = settings.public_url.rstrip("/")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if executor is None:
            pool.shutdown(wait=False, cancel_futures=True)
        clients.close()

    app = FastAPI(title="Moveworks Conversations API voice bridge", lifespan=lifespan)

    def twiml() -> TwiML:
        return TwiML(voice=settings.voice or None, language=settings.language)

    def listen(doc: TwiML, prompt: str) -> Response:
        doc.gather(action=TURN, prompt=prompt, hints=settings.speech_hints).redirect(REPROMPT)
        return _xml(doc)

    async def twilio_params(request: Request) -> Dict[str, str]:
        """The form fields of a request Twilio signed, or 403."""
        params = dict(parse_qsl((await request.body()).decode("utf-8"), keep_blank_values=True))
        url = public_url + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if not is_valid_signature(settings.twilio_auth_token, url, params, request.headers.get("X-Twilio-Signature")):
            log.warning("rejected a request to %s with a missing or invalid Twilio signature", request.url.path)
            raise HTTPException(status_code=403, detail="invalid Twilio signature")
        return params

    def speak_next(session: CallSession) -> Response:
        """Speak the next chunk of the answer, then listen."""
        chunk = session.pending.pop(0) if session.pending else ""
        parts = [chunk]
        if session.pending:
            parts.append(MORE_PROMPT)
        else:
            if session.link_note:
                parts.append(LINK_NOTE)
                session.link_note = False
            if 0 < len(session.options) <= MAX_SPOKEN_OPTIONS:
                parts.append("You can say " + ", or ".join(session.options) + ".")
        session.last_spoken = " ".join(part for part in parts if part)
        return listen(twiml(), session.last_spoken)

    @app.get("/healthz")
    async def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post(INCOMING)
    async def incoming(request: Request) -> Response:
        params = await twilio_params(request)
        call_sid, caller = params.get("CallSid", ""), params.get("From", "")
        user = directory.lookup(caller)
        if not call_sid or not user:
            log.info("call %s from %s: caller not in the directory; hanging up", call_sid, mask_number(caller))
            return _xml(twiml().say(UNKNOWN_CALLER).hangup())
        calls.start(call_sid, user, clients.get(user))
        log.info("call %s from %s: acting as %s", call_sid, mask_number(caller), user)
        return listen(twiml(), settings.greeting)

    @app.post(TURN)
    async def turn(request: Request) -> Response:
        params = await twilio_params(request)
        session = calls.get(params.get("CallSid", ""))
        if session is None:
            return _xml(twiml().say(EXPIRED).hangup())
        said = " ".join(params.get("SpeechResult", "").split())
        if not said:
            return listen(twiml(), DIDNT_CATCH)
        session.silent_prompts = 0
        if session.turn is not None:
            # A turn is running or its answer is unread. Never send a second one.
            return _xml(twiml().redirect(WAIT))
        if settings.transfer_number and wants_agent(said):
            calls.end(session.call_sid)
            return _xml(twiml().say(TRANSFERRING).dial(settings.transfer_number))
        if session.pending and wants_more(said):
            return speak_next(session)
        if session.last_spoken and wants_repeat(said):
            return listen(twiml(), session.last_spoken)
        callback_id = match_option(said, session.options)
        words = said.lower().strip(".!? ")
        # A bare "no" ends the call only as the answer to "anything else?", never to the assistant's own question.
        if callback_id is None and (words in GOODBYE_WORDS or (session.offered_more_help and words in DECLINE_WORDS)):
            calls.end(session.call_sid)
            return _xml(twiml().say(GOODBYE).hangup())

        session.pending, session.options, session.link_note = [], {}, False
        session.offered_more_help = False
        session.wait_cycles = 0
        filler = FILLERS[session.turns_sent % len(FILLERS)]
        if callback_id is not None:
            session.turn = pool.submit(runner.run, session, callback_id=callback_id)
        else:
            session.turn = pool.submit(runner.run, session, said)
        return _xml(twiml().say(filler).redirect(WAIT))

    @app.post(WAIT)
    async def wait(request: Request) -> Response:
        params = await twilio_params(request)
        session = calls.get(params.get("CallSid", ""))
        if session is None:
            return _xml(twiml().say(EXPIRED).hangup())
        pending_turn = session.turn
        if pending_turn is None:
            session.offered_more_help = True
            return listen(twiml(), STILL_THERE)
        if not await asyncio.to_thread(_finished_within, pending_turn, settings.wait_slice_seconds):
            session.wait_cycles += 1
            doc = twiml()
            if session.wait_cycles % max(settings.still_working_every, 1) == 0:
                doc.say(STILL_WORKING)
            return _xml(doc.redirect(WAIT))

        session.turn = None
        reply = _result(pending_turn)
        session.pending = chunk_for_speech(reply.text, settings.max_speech_chars) or [reply.text]
        session.options = reply.options
        session.link_note = reply.has_link_or_form
        return speak_next(session)

    @app.post(REPROMPT)
    async def reprompt(request: Request) -> Response:
        params = await twilio_params(request)
        session = calls.get(params.get("CallSid", ""))
        if session is None:
            return _xml(twiml().say(EXPIRED).hangup())
        session.silent_prompts += 1
        if session.silent_prompts > MAX_SILENT_PROMPTS:
            calls.end(session.call_sid)
            return _xml(twiml().say(GOODBYE).hangup())
        session.offered_more_help = True
        return listen(twiml(), STILL_THERE)

    @app.post(STATUS)
    async def status(request: Request) -> Response:
        params = await twilio_params(request)
        if params.get("CallStatus") in FINISHED_CALL_STATUSES:
            calls.end(params.get("CallSid", ""))
        return Response(status_code=204)

    return app


def _xml(doc: TwiML) -> Response:
    return Response(content=doc.to_xml(), media_type="application/xml")


def _finished_within(future: "Future[Reply]", seconds: float) -> bool:
    done, _ = wait_for_futures([future], timeout=seconds)
    return bool(done)


def _result(future: "Future[Reply]") -> Reply:
    try:
        return future.result()
    except Exception:
        # TurnRunner.run turns failures into a Reply; this covers a runner of your own.
        log.exception("turn raised")
        return Reply("Sorry, something went wrong.", failed=True)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def _make_client_factory(base_url: str, bot_name: str, mode: str) -> Callable[[str], ConversationsApiClient]:
    """A function from an employee to a client that acts as them."""
    if mode == "s2s":
        issuer, audience = _required("MW_S2S_ISSUER"), _required("MW_S2S_AUDIENCE")
        key_id = _required("MW_S2S_KEY_ID")
        private_key = Path(_required("MW_S2S_PRIVATE_KEY_PATH")).read_text()

        def s2s_client(user: str) -> ConversationsApiClient:
            bearer = S2SJwtBearer(
                base_url=base_url, issuer=issuer, audience=audience, key_id=key_id,
                private_key=private_key, user_identifier=user,
            )
            return ConversationsApiClient(base_url=base_url, bot_name=bot_name, bearer_provider=bearer)

        return s2s_client

    if mode == "api-key":
        shared = ConversationsApiClient(base_url=base_url, bot_name=bot_name, bearer_provider=ApiKeyBearer(_required("MW_API_KEY")))
        return lambda _user: shared

    raise SystemExit(f"MW_AUTH must be s2s or api-key for the voice bridge (got {mode!r})")


def _caller_directory(mode: str) -> CallerDirectory:
    numbers: Dict[str, str] = {}
    if os.environ.get("VOICE_CALLERS_FILE"):
        numbers.update(CallerDirectory.load(Path(os.environ["VOICE_CALLERS_FILE"])))
    if os.environ.get("VOICE_CALLERS"):
        numbers.update(CallerDirectory.parse(os.environ["VOICE_CALLERS"]))
    if mode == "api-key":
        if not numbers:
            log.warning("VOICE_CALLERS is empty: every caller is answered as the API key's user")
        # Every call runs as the key's user; the directory only decides who may call.
        return CallerDirectory({n: "api-key user" for n in numbers}, default_user=None if numbers else "api-key user")
    if not numbers:
        raise SystemExit(
            "set VOICE_CALLERS or VOICE_CALLERS_FILE: with MW_AUTH=s2s, each caller's number maps to the employee the call acts as"
        )
    return CallerDirectory(numbers)


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def build_from_env() -> FastAPI:
    """The app, configured from environment variables (see the README)."""
    base_url = os.environ.get("MW_BASE_URL", "https://api.moveworks.ai")
    bot_name = _required("MW_BOT_NAME")
    mode = os.environ.get("MW_AUTH", "s2s").lower()
    settings = VoiceSettings(
        twilio_auth_token=_required("TWILIO_AUTH_TOKEN"),
        public_url=_required("VOICE_PUBLIC_URL"),
        language=os.environ.get("VOICE_LANGUAGE", "en-US"),
        voice=os.environ.get("VOICE_TTS_VOICE", ""),
        speech_hints=os.environ.get("VOICE_SPEECH_HINTS", ""),
        greeting=os.environ.get("VOICE_GREETING") or GREETING,
        transfer_number=os.environ.get("VOICE_TRANSFER_NUMBER", ""),
        max_speech_chars=int(os.environ.get("VOICE_MAX_SPEECH_CHARS", str(MAX_SPEECH_CHARS))),
    )
    runner = TurnRunner(
        stream=_flag("VOICE_STREAM", True),
        timeout_seconds=float(os.environ.get("VOICE_TURN_TIMEOUT_SECONDS", str(DEFAULT_TURN_TIMEOUT_SECONDS))),
        context_hint=os.environ.get("VOICE_CONTEXT_HINT", ""),
    )
    return create_app(
        settings,
        directory=_caller_directory(mode),
        clients=ClientPool(_make_client_factory(base_url, bot_name, mode)),
        runner=runner,
    )


def main() -> None:
    import uvicorn
    from dotenv import find_dotenv, load_dotenv

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(find_dotenv(usecwd=True))
    host = os.environ.get("VOICE_HOST", "127.0.0.1")
    port = int(os.environ.get("VOICE_PORT", "8095"))
    uvicorn.run(build_from_env(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
