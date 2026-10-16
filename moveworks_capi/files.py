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
from dataclasses import dataclass
from typing import Any, BinaryIO, Dict, Optional, Union

from .client import ConversationsApiClient

FILES_PATH = "/rest/v1alpha1/files"
CONVERSATIONS_ATTACHMENT = "CONVERSATIONS_ATTACHMENT"


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
) -> UploadedFile:
    """Upload one file (``POST /rest/v1alpha1/files``) and return its File resource.

    Args:
        client: A ``ConversationsApiClient``. The upload uses its base URL,
            bearer, and ``Assistant-Name`` header.
        file: A path, the file's bytes, or a binary stream open for reading.
        filename: The name the file is uploaded under, returned as ``name``.
            Defaults to the base name of a path; required for bytes and
            streams.
        purpose: What the file is for. ``CONVERSATIONS_ATTACHMENT`` is the
            only accepted value.

    The ``purpose`` field is sent before the ``file`` part, as the API
    requires. Like ``create_response``, the upload is resent only after a 429
    or 401, never after a timeout or 5xx.

    Raises:
        ValueError: ``filename`` is missing for bytes or a stream, or empty.
        RequestRejectedError: The API rejected the upload. ``status_code`` is
            ``413`` for a file over the size limit and ``422`` for content
            that failed validation; ``400`` covers an empty file or an
            invalid filename.
    """
    if isinstance(file, (str, os.PathLike)):
        path = os.fspath(file)
        name = filename if filename is not None else os.path.basename(path)
        with open(path, "rb") as stream:
            content = stream.read()
    elif isinstance(file, (bytes, bytearray)):
        name = filename
        content = bytes(file)
    else:
        name = filename
        content = file.read()
    if not name:
        raise ValueError("filename is required when uploading bytes or a stream")

    response = client.post(
        FILES_PATH,
        files=[
            ("purpose", (None, purpose)),
            ("file", (name, content, "application/octet-stream")),
        ],
    )
    return UploadedFile.from_api(response.json())
