"""A phone bridge to the Moveworks assistant over the Conversations API.

The Conversations API is text in, text out. This example puts a phone number
in front of it: the telephony provider (Twilio Programmable Voice) turns the
caller's speech into text, the bridge sends that text as a Conversations API
turn, and the reply comes back as text that the provider reads aloud.

- ``speech``: turn an assistant reply into short, speakable chunks.
- ``twiml``: build TwiML and check Twilio's request signature.
- ``bridge``: callers, per-call sessions, and the Conversations API turn.
- ``server``: the FastAPI webhooks Twilio calls.

Run it with ``python -m examples.voice`` from the repo root. See
``examples/voice/README.md``.
"""
