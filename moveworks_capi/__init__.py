"""Reference implementations for common Moveworks Conversations API patterns.

The package is organized around independent modules that mirror the docs:

- ``moveworks_capi.client`` - a thin HTTP client that injects the
  ``Assistant-Name`` header, handles retries, and delegates auth to a callable.
- ``moveworks_capi.auth`` - bearer providers, one per supported auth path.
- ``moveworks_capi.conversations`` - create, list, fetch, and update
  conversations; list their messages.
- ``moveworks_capi.responses`` - build turn input, submit a turn, poll it to
  completion, and extract its text and actions.
- ``moveworks_capi.streaming`` - server-sent events variant of create-response.
- ``moveworks_capi.files`` - upload a file with the Files API (alpha) to
  attach to a turn.
- ``moveworks_capi.notifications`` - events polling with a checkpoint, and the
  outbound webhook verifier.
- ``moveworks_capi.rendering`` - helpers for displaying assistant messages in
  a UI.

Each module can be lifted independently; nothing here assumes you use every piece.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("moveworks-capi-starter")
except PackageNotFoundError:
    # Running from a checkout that was never installed.
    __version__ = "0.0.0+unknown"
