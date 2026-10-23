"""Receive and verify Moveworks outbound webhook deliveries.

Usage:
    cp .env.example .env  # fill in MW_ORG_DOMAIN and MW_BOT_NAME
    python examples/receive_webhook.py

Starts a FastAPI receiver bound to ``WEBHOOK_HOST:WEBHOOK_PORT``. On each
delivery it verifies the RSA signature against the Moveworks instance's JWKS, deduplicates
against ``webhook-id``, and prints the envelope. Deliveries arrive at
``POST /webhooks/moveworks``; ``GET /healthz`` is a health check. Expose the
receiver publicly (for example, via a tunnel like ngrok or cloudflared) and
register the public URL plus ``/webhooks/moveworks`` at Setup > Core Platform >
Outbound Webhooks.

See: https://docs.moveworks.com/api-reference/conversations-api/proactive-notifications/receive-via-outbound-webhook
"""

from __future__ import annotations

import io
import logging
import os
import sys
from collections import OrderedDict
from typing import MutableMapping

import uvicorn
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, HTTPException, Request

from moveworks_capi.errors import (
    InvalidWebhookTimestampError,
    JwksFetchError,
    SignatureVerificationError,
    UnknownKidError,
)
from moveworks_capi.notifications import JwksCache, WebhookVerifier

MAX_SEEN_IDS = 10_000

log = logging.getLogger(__name__)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def _build_app() -> FastAPI:
    org_domain = _required("MW_ORG_DOMAIN").rstrip("/")
    tolerance = float(os.environ.get("WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS", "300"))

    jwks = JwksCache(f"https://{org_domain}/.well-known/jwks.json")
    verifier = WebhookVerifier(jwks, timestamp_tolerance_seconds=tolerance)
    seen: MutableMapping[str, None] = OrderedDict()

    app = FastAPI(title="Moveworks Outbound Webhook Receiver")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/moveworks")
    async def receive(request: Request) -> dict[str, object]:
        raw_body = await request.body()
        try:
            envelope = verifier.verify(headers=request.headers, raw_body=raw_body)
        except (
            SignatureVerificationError,
            InvalidWebhookTimestampError,
            UnknownKidError,
        ) as exc:
            log.warning("webhook rejected: %s", exc)
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except JwksFetchError as exc:
            # Our side could not load the signing keys, so the delivery is
            # unverified rather than bad; a 5xx asks the sender to retry.
            log.error("cannot verify webhook, JWKS unavailable: %s", exc)
            raise HTTPException(status_code=503, detail="signing keys unavailable") from exc

        if envelope.id in seen:
            log.info("duplicate delivery id=%s (deduped)", envelope.id)
            return {"received": True, "deduped": True}
        seen[envelope.id] = None
        while len(seen) > MAX_SEEN_IDS:
            seen.popitem(last=False)

        print(
            f"webhook id={envelope.id} type={envelope.type} "
            f"user={envelope.data.get('user_id')} at={envelope.timestamp}"
        )
        return {"received": True}

    return app


def main() -> None:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(find_dotenv(usecwd=True))
    host = os.environ.get("WEBHOOK_HOST", "127.0.0.1")
    port = int(os.environ.get("WEBHOOK_PORT", "8080"))
    uvicorn.run(_build_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
