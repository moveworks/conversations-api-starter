"""Run one user turn and yield the frames the browser renders.

Frames (server-sent events): ``conversation`` (new conversation id),
``delta`` (answer or reasoning text), ``completed``, then per message
``actions``, ``handoff``, ``feedback_ids``; ``late_message`` for a reply the
API filed under an earlier response; ``error`` on failure.

Turns stream by default. With ``streaming=False`` (``python -m app
--no-stream``) the message is submitted and polled until the response
completes, and the browser receives the same frames minus reasoning.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator

from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.conversations import create_conversation, list_messages, replies_after_input
from moveworks_capi.errors import StreamClosedError
from moveworks_capi.responses import Response, create_response, wait_for_completion
from moveworks_capi.streaming import OutputDelta, ResponseCompleted, ResponseCreated, stream_response
from moveworks_capi.rendering import normalize_markup

from app.frames import sse_frame, to_async
from app.present import present_message

log = logging.getLogger(__name__)

# Long answers approach two minutes end to end.
STREAM_FALLBACK_TIMEOUT_SECONDS = 150.0


@dataclass
class StreamProgress:
    response_id: str = ""
    answered: bool = False
    completed: bool = False


async def run_turn(
    client: ConversationsApiClient,
    conversation_id: str | None,
    text: str | None,
    callback_id: str | None,
    *,
    streaming: bool = True,
) -> AsyncIterator[bytes]:
    try:
        if not conversation_id:
            conversation_id = await _create_conversation(client, text)
            yield sse_frame("conversation", {"conversation_id": conversation_id})

        if streaming:
            progress = StreamProgress()
            try:
                async for frame in stream_turn(client, conversation_id, text, callback_id, progress):
                    yield frame
            except StreamClosedError as exc:
                if not progress.response_id:
                    raise
                log.info("stream closed early (%s); polling for the answer", exc)
            if not progress.response_id:
                return
            # The stream can close before the answer arrives. Then the response is
            # still running, so poll as long as a slow answer takes, and render it
            # from the poll the way the non-streaming path does.
            cut_short = not (progress.answered and progress.completed)
            try:
                final = await asyncio.to_thread(
                    wait_for_completion, client, conversation_id, progress.response_id,
                    timeout_seconds=STREAM_FALLBACK_TIMEOUT_SECONDS if cut_short else 10.0,
                )
            except TimeoutError:
                yield sse_frame("error", {"message": (
                    "The assistant is taking unusually long. "
                    "Its reply will appear in this conversation's history when it is ready."
                )})
                return
            if cut_short:
                for frame in _answer_frames(final):
                    yield frame
        else:
            final = await _submit_and_poll(client, conversation_id, text, callback_id)
            for frame in _answer_frames(final):
                yield frame

        for frame in await asyncio.to_thread(lambda: list(_message_frames(conversation_id, final))):
            yield frame
        if not any(item.get("type") == "MESSAGE" for item in final.outputs):
            async for frame in _late_message_frames(client, conversation_id, final.response_id):
                yield frame
    except Exception as exc:
        log.exception("turn failed")
        yield sse_frame("error", {"message": str(exc)})


async def stream_turn(
    client: ConversationsApiClient,
    conversation_id: str,
    text: str | None,
    callback_id: str | None,
    progress: StreamProgress,
) -> AsyncIterator[bytes]:
    """Reasoning and answer snapshots as they stream. Actions and feedback ids
    are read from the completed response afterwards, like the polling path."""
    stream = stream_response(client, conversation_id, text, callback_id=callback_id)
    async for event in to_async(stream):
        if isinstance(event, ResponseCreated):
            progress.response_id = event.response_id
        elif isinstance(event, OutputDelta) and event.output_type == "MESSAGE":
            progress.answered = True
            message = present_message(event.output.get("message") or {}, resolve_links=False)
            yield sse_frame("delta", {"output_type": "MESSAGE", "text": message["text"], "sources": message["sources"]})
        elif isinstance(event, OutputDelta):
            yield sse_frame("delta", {"output_type": event.output_type, "text": normalize_markup(event.text or "")})
        elif isinstance(event, ResponseCompleted):
            progress.completed = True
            yield sse_frame("completed", {"status": event.status})


def _answer_frames(final: Response):
    """The answer and completion frames for a response read by polling."""
    for item in final.outputs:
        if item.get("type") == "MESSAGE":
            message = present_message(item.get("message") or {}, response_id=final.response_id, resolve_links=False)
            yield sse_frame("delta", {"output_type": "MESSAGE", "text": message["text"], "sources": message["sources"]})
    yield sse_frame("completed", {"status": final.status})


async def _late_message_frames(
    client: ConversationsApiClient, conversation_id: str, response_id: str
) -> AsyncIterator[bytes]:
    """Replies to an input whose response came back empty (see
    ``conversations.replies_after_input``), found by re-reading recent history.

    The reply can land a moment after the response completes, so one retry.
    """
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(1.5)
        try:
            body = await asyncio.to_thread(list_messages, client, conversation_id, limit=10)
        except Exception:
            log.exception("could not read history for a late reply")
            return
        replies = replies_after_input(body.get("messages") or [], response_id)
        if replies:
            for message in replies:
                shown = await asyncio.to_thread(present_message, message)
                yield sse_frame("late_message", {"conversation_id": conversation_id, "message": shown})
            return


async def _create_conversation(client: ConversationsApiClient, text: str | None) -> str:
    # Title it from the opening message: ListConversations returns `title`,
    # so the sidebar costs ONE request instead of one history read per row.
    title = (text or "New conversation").strip().replace("\n", " ")[:80]
    created = await asyncio.to_thread(create_conversation, client, title=title)
    return created["conversation_id"]


async def _submit_and_poll(
    client: ConversationsApiClient, conversation_id: str, text: str | None, callback_id: str | None
) -> Response:
    submitted = await asyncio.to_thread(
        lambda: create_response(client, conversation_id, text, callback_id=callback_id)
    )
    return await asyncio.to_thread(
        wait_for_completion, client, conversation_id, submitted["response_id"],
        timeout_seconds=STREAM_FALLBACK_TIMEOUT_SECONDS,
    )


def _message_frames(conversation_id: str, final: Response):
    """Actions, Get Help menu, and feedback ids ride on the completed response."""
    for item in final.outputs:
        if item.get("type") != "MESSAGE":
            continue
        message = present_message(item.get("message") or {}, response_id=final.response_id)
        ref = {"conversation_id": conversation_id, "response_id": final.response_id, "message_id": message["message_id"]}
        if message["actions"]:
            yield sse_frame("actions", {**ref, "actions": message["actions"]})
        if message["handoff"]:
            yield sse_frame("handoff", {**ref, "categories": message["handoff"]})
        if message["feedback"]["helpful"] or message["feedback"]["unhelpful"]:
            yield sse_frame("feedback_ids", {**ref, **message["feedback"]})
