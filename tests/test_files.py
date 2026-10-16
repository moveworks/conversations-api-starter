"""Files API upload: multipart shape, filename handling, parsing, errors, retries."""

from __future__ import annotations

import io

import pytest
import requests

from moveworks_capi import client as client_module
from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.errors import ConversationsApiError, DownstreamServiceError, RequestRejectedError
from moveworks_capi.files import UploadedFile, upload_file
from tests.test_client import _CachingBearer, _Response, _Session

FILE_RESOURCE = {
    "id": "file_2yE5b2NC3Ypa2FszHbkh7",
    "purpose": "CONVERSATIONS_ATTACHMENT",
    "name": "screenshot.png",
    "content_type": "image/png",
    "size_bytes": 482133,
    "create_time": "2026-08-26T05:11:00Z",
    "expire_time": "2026-08-27T05:11:00Z",
}


@pytest.fixture(autouse=True)
def sleeps(monkeypatch) -> list:
    recorded: list = []
    monkeypatch.setattr(client_module.time, "sleep", recorded.append)
    return recorded


def _client(*script, bearer=lambda: "token") -> tuple:
    session = _Session(*script)
    client = ConversationsApiClient("https://api.example.com", "test-bot", bearer, session=session)  # type: ignore[arg-type]
    return client, session


def _wire_body(call: tuple) -> bytes:
    """Encode a recorded call the way ``requests`` puts it on the wire."""
    method, url, kwargs = call
    prepared = requests.Request(method, url, files=kwargs["files"], headers=kwargs["headers"]).prepare()
    assert prepared.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert isinstance(prepared.body, bytes)
    return prepared.body


class TestRequest:
    def test_posts_multipart_to_the_alpha_files_path(self) -> None:
        client, session = _client(_Response(201, FILE_RESOURCE))
        upload_file(client, b"\x89PNG", filename="screenshot.png")
        method, url, kwargs = session.calls[0]
        assert (method, url) == ("POST", "https://api.example.com/rest/v1alpha1/files")
        assert kwargs["json"] is None
        assert "Content-Type" not in kwargs["headers"]
        assert kwargs["headers"]["Assistant-Name"] == "test-bot"

    def test_purpose_is_sent_before_the_file_part(self) -> None:
        client, session = _client(_Response(201, FILE_RESOURCE))
        upload_file(client, b"hello", filename="notes.txt")
        body = _wire_body(session.calls[0])
        purpose_at = body.index(b'name="purpose"\r\n\r\nCONVERSATIONS_ATTACHMENT\r\n')
        file_at = body.index(b'name="file"; filename="notes.txt"')
        assert purpose_at < file_at
        assert body.count(b'name="file"') == 1
        assert b"\r\n\r\nhello\r\n" in body[file_at:]

    def test_json_and_files_together_are_refused(self) -> None:
        client, session = _client()
        with pytest.raises(ValueError):
            client.request("POST", "/rest/v1alpha1/files", json={}, files=[("purpose", (None, "x"))])
        assert session.calls == []


class TestFilename:
    def test_a_path_defaults_to_its_base_name(self, tmp_path) -> None:
        path = tmp_path / "report.pdf"
        path.write_bytes(b"%PDF-1.7")
        client, session = _client(_Response(201, FILE_RESOURCE))
        upload_file(client, path)
        name, content, _ = dict(session.calls[0][2]["files"])["file"]
        assert (name, content) == ("report.pdf", b"%PDF-1.7")

    def test_a_string_path_is_read_and_can_be_renamed(self, tmp_path) -> None:
        path = tmp_path / "scan.bin"
        path.write_bytes(b"data")
        client, session = _client(_Response(201, FILE_RESOURCE))
        upload_file(client, str(path), filename="scan.png")
        assert dict(session.calls[0][2]["files"])["file"][:2] == ("scan.png", b"data")

    def test_a_stream_is_read_to_bytes(self) -> None:
        client, session = _client(_Response(201, FILE_RESOURCE))
        upload_file(client, io.BytesIO(b"streamed"), filename="log.txt")
        assert dict(session.calls[0][2]["files"])["file"][:2] == ("log.txt", b"streamed")

    @pytest.mark.parametrize("source", [b"bytes", io.BytesIO(b"stream")])
    @pytest.mark.parametrize("filename", [None, ""])
    def test_bytes_and_streams_require_a_filename(self, source, filename) -> None:
        client, session = _client()
        with pytest.raises(ValueError, match="filename"):
            upload_file(client, source, filename=filename)
        assert session.calls == []


def test_the_file_resource_is_parsed() -> None:
    client, _ = _client(_Response(201, FILE_RESOURCE))
    uploaded = upload_file(client, b"\x89PNG", filename="screenshot.png")
    assert uploaded == UploadedFile(
        id="file_2yE5b2NC3Ypa2FszHbkh7",
        name="screenshot.png",
        content_type="image/png",
        size_bytes=482133,
        purpose="CONVERSATIONS_ATTACHMENT",
        create_time="2026-08-26T05:11:00Z",
        expire_time="2026-08-27T05:11:00Z",
    )


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "code"),
        [(400, "BAD_REQUEST"), (403, "FORBIDDEN"), (413, "PAYLOAD_TOO_LARGE"), (422, "UNPROCESSABLE_ENTITY")],
    )
    def test_rejections_carry_status_and_code(self, status: int, code: str) -> None:
        client, session = _client(_Response(status, {"error": {"code": code, "message": "rejected upload"}}))
        with pytest.raises(RequestRejectedError) as exc:
            upload_file(client, b"x", filename="a.txt")
        assert (exc.value.status_code, exc.value.code, exc.value.trace_id) == (status, code, "trace_1")
        assert "rejected upload" in str(exc.value)
        assert len(session.calls) == 1

    def test_a_5xx_is_not_resent(self) -> None:
        failure = _Response(500, {"error": {"code": "INTERNAL_SERVER_ERROR", "message": "x"}})
        client, session = _client(failure, _Response(201, FILE_RESOURCE))
        with pytest.raises(DownstreamServiceError):
            upload_file(client, b"x", filename="a.txt")
        assert len(session.calls) == 1

    def test_a_timeout_is_not_resent(self) -> None:
        client, session = _client(requests.Timeout("read timed out"), _Response(201, FILE_RESOURCE))
        with pytest.raises(ConversationsApiError, match="may have processed it"):
            upload_file(client, b"x", filename="a.txt")
        assert len(session.calls) == 1

    def test_a_429_resends_the_same_body(self) -> None:
        client, session = _client(_Response(429, "slow down"), _Response(201, FILE_RESOURCE))
        assert upload_file(client, io.BytesIO(b"once"), filename="a.txt").id == FILE_RESOURCE["id"]
        assert [dict(call[2]["files"])["file"][1] for call in session.calls] == [b"once", b"once"]

    def test_a_401_refreshes_the_bearer_and_retries_once(self) -> None:
        bearer = _CachingBearer()
        client, session = _client(_Response(401, "expired"), _Response(201, FILE_RESOURCE), bearer=bearer)
        upload_file(client, b"x", filename="a.txt")
        assert bearer.invalidated == 1
        assert [call[2]["headers"]["Authorization"] for call in session.calls] == ["Bearer token_0", "Bearer token_1"]
