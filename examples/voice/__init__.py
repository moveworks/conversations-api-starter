"""A voice demo for the Moveworks assistant: two harnesses working together.

The Conversations API is text in, text out. Voice comes from a second harness
in front of it:

- The **voice harness** runs the conversation. An OpenAI Realtime
  speech-to-speech model listens, speaks, handles turn-taking and
  interruptions, answers small talk itself, and covers the wait while the
  orchestrator works. It runs in the browser (``static/voice.js``).
- The **orchestrator harness** does the work. The voice model hands it every
  request that needs company knowledge, the caller's data, or an action,
  through its ``ask_orchestrator`` tool, as a Conversations API turn.

Modules:

- ``tuning``: every voice-side knob, with the prompts in ``prompts/``.
- ``realtime``: the Realtime session config and its short-lived client secret.
- ``orchestrator``: ``ask_orchestrator`` as Conversations API turns.
- ``speech``: the orchestrator's answers, prepared for the ear.
- ``server``: the FastAPI app that serves the page and holds every secret.

Run it with ``python -m examples.voice`` from the repo root. See
``examples/voice/README.md``.
"""
