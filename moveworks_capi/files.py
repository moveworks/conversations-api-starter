"""Upload a file with the Files API and attach it to a turn.

``POST /rest/v1alpha1/files`` takes a ``multipart/form-data`` body with a
``purpose`` field and one ``file`` part, and returns ``201`` with a File
resource. Pass its ``id`` in ``file_ids`` to ``responses.create_response`` or
``streaming.stream_response`` to attach the file to a turn.

The Files API is in alpha and enabled per Moveworks instance; until it is
enabled, uploads return ``404``. Uploaded files expire 24 hours after they
are created, and there is no operation to read, list, or delete one.

See:
https://docs.moveworks.com/api-reference/alpha-files-api/files/upload-file
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from typing import Any, BinaryIO, Dict, Optional, Union

from .client import ConversationsApiClient
from .errors import ConversationsApiError

FILES_PATH = "/rest/v1alpha1/files"
CONVERSATIONS_ATTACHMENT = "CONVERSATIONS_ATTACHMENT"
MAX_UPLOAD_MB = 100
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_FILENAME_CHARACTERS = 255
BIDI_FORMATTING_CHARACTERS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


@dataclass(frozen=True)
class UploadedFile:
    """The File resource returned by an upload.

    ``content_type`` is the type Moveworks detected from the bytes, not the
    one the request declared. ``create_time`` and ``expire_time`` are RFC
    3339 UTC timestamps; after ``expire_time`` the ``id`` no longer resolves.
    """

    id: str
    name: str
    content_type: str
    size_bytes: int
    purpose: str
    create_time: str
    expire_time: str

    @classmethod
    def from_api(cls, obj: Dict[str, Any]) -> "UploadedFile":
        return cls(
            id=obj.get("id", ""),
            name=obj.get("name", ""),
            content_type=obj.get("content_type", ""),
            size_bytes=int(obj.get("size_bytes") or 0),
            purpose=obj.get("purpose", ""),
            create_time=obj.get("create_time", ""),
            expire_time=obj.get("expire_time") or "",
        )


def upload_file(
    client: ConversationsApiClient,
    file: Union[str, "os.PathLike[str]", bytes, BinaryIO],
    *,
    filename: Optional[str] = None,
    purpose: str = CONVERSATIONS_ATTACHMENT,
    timeout: Optional[float] = None,
) -> UploadedFile:
    """Upload one file (``POST /rest/v1alpha1/files``) and return its File resource.

    Args:
        client: A ``ConversationsApiClient``. The upload uses its base URL,
            bearer, and ``Assistant-Name`` header.
        file: A path, the file's bytes, or a binary stream open for reading.
            A stream is read from its current position to the end.
        filename: The name the file is uploaded under, returned as ``name``.
            Defaults to the base name of a path; required for bytes and
            streams.
        purpose: What the file is for. ``CONVERSATIONS_ATTACHMENT`` is the
            only accepted value.
        timeout: Seconds to wait for the upload, in place of the client's
            per-request timeout. A large file can need longer.

    The ``purpose`` field is sent before the ``file`` part, as the API
    requires. Like ``create_response``, the upload is resent only after a 429
    or 401, never after a timeout or 5xx. After a timeout the file may have
    been stored even though no ``id`` came back.

    Raises:
        ValueError: The filename is missing or breaks the API's filename
            rules, or the file is empty or over 100 MB. Checked before
            anything is sent.
        TypeError: A stream returned text rather than bytes.
        RequestRejectedError: The API rejected the upload. ``status_code`` is
            ``404`` when the Files API is not enabled for the Moveworks
            instance, ``403`` when the credential may not upload files,
            ``413`` for a file over the size limit, ``422`` for content that
            failed validation, and ``400`` for other invalid requests.
        ConversationsApiError: A success response carried no file ``id``.
    """
    if isinstance(file, (str, os.PathLike)):
        path = os.fspath(file)
        name = filename if filename is not None else os.path.basename(path)
        _check_filename(name)
        if os.stat(path).st_size > MAX_UPLOAD_BYTES:
            raise ValueError(f"{path} is over the {MAX_UPLOAD_MB} MB upload limit")
        with open(path, "rb") as stream:
            content = _read_capped(stream)
    else:
        if filename is None:
            raise ValueError("filename is required when uploading bytes or a stream")
        name = filename
        _check_filename(name)
        content = bytes(file) if isinstance(file, (bytes, bytearray)) else _read_capped(file)
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"the file is over the {MAX_UPLOAD_MB} MB upload limit")
    if not content:
        raise ValueError("the file is empty")

    response = client.post(
        FILES_PATH,
        files=[
            ("purpose", (None, purpose)),
            ("file", (name, content, "application/octet-stream")),
        ],
        timeout=timeout,
    )
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not body.get("id"):
        raise ConversationsApiError(
            f"upload returned {response.status_code} without a file id",
            status_code=response.status_code,
            trace_id=response.headers.get("x-trace-id"),
        )
    return UploadedFile.from_api(body)


def _read_capped(stream: BinaryIO) -> bytes:
    """Read to the end, stopping one byte past the limit so an oversized stream is not held in memory.

    Loops because an unbuffered stream can return fewer bytes than asked for before its end.
    """
    chunks = []
    remaining = MAX_UPLOAD_BYTES + 1
    while remaining > 0:
        chunk = stream.read(remaining)
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError("the stream must be opened in binary mode")
        if not chunk:
            break
        chunks.append(bytes(chunk))
        remaining -= len(chunk)
    return b"".join(chunks)


def _check_filename(name: str) -> None:
    """Apply the Files API's filename rules, so a bad name fails before the upload is sent."""
    normalized = unicodedata.normalize("NFC", name).strip()
    if not normalized:
        raise ValueError("filename must not be empty")
    if len(normalized) > MAX_FILENAME_CHARACTERS:
        raise ValueError(f"filename must be at most {MAX_FILENAME_CHARACTERS} characters")
    if normalized in (".", ".."):
        raise ValueError(f"filename {normalized!r} is not allowed")
    if "/" in normalized or "\\" in normalized:
        raise ValueError("filename must not contain a path separator")
    if any(unicodedata.category(char) == "Cc" or char in BIDI_FORMATTING_CHARACTERS for char in normalized):
        raise ValueError("filename must not contain control or bidirectional formatting characters")
    # requests percent-encodes a double quote in the part header, which would store a different name.
    if '"' in normalized:
        raise ValueError("filename must not contain a double quote")
