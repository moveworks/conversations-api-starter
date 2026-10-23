"""Fire a Tool Studio listener to produce a proactive notification.

Usage:
    # set MW_TEST_LISTENER_URL (and MW_TEST_LISTENER_SECRET if secured) in .env
    python examples/trigger_listener.py "optional message text"

A proactive notification originates inside Moveworks, so testing one on
demand needs a trigger you control. The simplest trigger is an Agent
Studio listener whose plugin ends in a ``notify`` step addressed to the user
in the payload. This script POSTs ``{"email", "message"}`` to that listener;
the notification then arrives through polling (``poll_events.py``), an
outbound webhook (``receive_webhook.py``), or the reference web app's
notification drawer (which can also send one itself).

A secured listener verifies an HMAC-SHA256 of the exact request body, hex
encoded, in ``X-Signature``. A secured listener accepts the request before
verifying the signature, so a rejected signature appears only in the
listener's logs in Tool Studio. Unsecured
listeners are rate-limited to about one request per ten seconds per Moveworks
instance. Notifications route to the user's most recent channel, so send at
least one message through your Conversations API integration first.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import find_dotenv, load_dotenv


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    listener_url = os.environ.get("MW_TEST_LISTENER_URL")
    if not listener_url:
        raise SystemExit("missing required env var: MW_TEST_LISTENER_URL")
    email = os.environ.get("MW_TEST_NOTIFY_EMAIL") or os.environ.get("MW_S2S_USER_EMAIL")
    if not email:
        raise SystemExit("missing required env var: MW_TEST_NOTIFY_EMAIL (the recipient)")
    sent_at = f"{datetime.now(timezone.utc):%H:%M:%S}"
    message = sys.argv[1] if len(sys.argv) > 1 else f"Test notification from trigger_listener.py at {sent_at} UTC"

    body = json.dumps({"email": email, "message": message}, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    secret = os.environ.get("MW_TEST_LISTENER_SECRET")
    if secret:
        headers["X-Signature"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    resp = requests.post(listener_url, data=body, headers=headers, timeout=15)
    print(f"listener responded {resp.status_code}: {resp.text[:300]}")
    resp.raise_for_status()


if __name__ == "__main__":
    main()
