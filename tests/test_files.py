"""Files API upload: multipart shape, filename handling, parsing, errors, retries."""

from __future__ import annotations

import io
import json

import pytest
import requests
from requests.adapters import BaseAdapter

from moveworks_capi import client as client_module
from moveworks_capi import files as files_module
from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.errors import ConversationsApiError, DownstreamServiceError, RateLimitedError, RequestRejectedError
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


class _CapturingAdapter(BaseAdapter):
    """Records each prepared request, as sent through a real ``requests.Session``, and answers 201."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list = []

    def send(self, request, **kwargs):  # type: ignore[override]
        self.sent.append((request, kwargs))
        response = requests.Response()
        response.status_code = 201
        response._content = json.dumps(FILE_RESOURCE).encode()
        response.headers["Content-Type"] = "application/json"
        response.request = request
        return response

    def close(self) -> None:
        pass


def _real_session_client(session_headers: dict | None = None, timeout: float = 30.0) -> tuple:
    session = requests.Session()
    session.headers.update(session_headers or {})
    adapter = _CapturingAdapter()
    session.mount("https://", adapter)
    client = ConversationsApiClient("https://api.example.com", "test-bot", lambda: "token", session=session, timeout=timeout)
    return client, adapter


def _wire_body(call: tuple) -> bytes:
    """Encode a recorded call the way ``requests`` puts it on the wire."""
    method, url, kwargs = call
    prepared = requests.Session().prepare_request(requests.Request(method, url, files=kwargs["files"], headers=kwargs["headers"]))
    assert prepared.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert isinstance(prepared.body, bytes)
    return prepared.body


class TestRequest:
    def test_posts_multipart_to_the_alpha_files_path(self) -> None:
        client, session = _client(_Response(201, FILE_RESOURCE))
        upload_file(client, b"\x89PNG", filename="screenshot.png")
        method, url, kwargs = session.calls[0]
        assert (method, url) == ("POST", "https://api.example.com/rest/v1alpha1/files")
        assert "json" not in kwargs
        assert kwargs["headers"]["Content-Type"] is None
        assert kwargs["headers"]["Assistant-Name"] == "test-bot"

    def test_json_requests_do_not_pass_files(self) -> None:
        client, session = _client(_Response(200, {}))
        client.post("/rest/v1/conversations", json={})
        assert "files" not in session.calls[0][2]

    def test_a_session_content_type_does_not_replace_the_multipart_boundary(self) -> None:
        client, adapter = _real_session_client({"Content-Type": "application/json"})
        upload_file(client, b"hello", filename="notes.txt")
        prepared, _ = adapter.sent[0]
        assert prepared.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        assert b'filename="notes.txt"' in prepared.body

    def test_timeout_overrides_the_client_timeout(self) -> None:
        client, adapter = _real_session_client(timeout=30.0)
        upload_file(client, b"hello", filename="notes.txt", timeout=600.0)
        upload_file(client, b"hello", filename="notes.txt")
        assert [kwargs["timeout"] for _, kwargs in adapter.sent] == [600.0, 30.0]

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
    def test_bytes_and_streams_require_a_filename(self, source) -> None:
        client, session = _client()
        with pytest.raises(ValueError, match="filename is required"):
            upload_file(client, source)
        assert session.calls == []

    def test_an_empty_filename_for_a_path_is_rejected(self, tmp_path) -> None:
        path = tmp_path / "report.pdf"
        path.write_bytes(b"%PDF-1.7")
        client, session = _client()
        with pytest.raises(ValueError, match="filename must not be empty"):
            upload_file(client, path, filename="")
        assert session.calls == []

    @pytest.mark.parametrize(
        ("filename", "message"),
        [
            ("", "must not be empty"),
            ("   ", "must not be empty"),
            ("a" * 256, "at most 255"),
            (".", "not allowed"),
            ("..", "not allowed"),
            ("dir/report.pdf", "path separator"),
            ("dir\\report.pdf", "path separator"),
            ("report\x00.pdf", "control"),
            ("line\nbreak.txt", "control"),
            ("invoice\u202efdp.exe", "bidirectional"),
            ('say "hi".txt', "double quote"),
        ],
    )
    def test_filenames_that_break_the_api_rules_are_rejected(self, filename: str, message: str) -> None:
        client, session = _client()
        with pytest.raises(ValueError, match=message):
            upload_file(client, b"x", filename=filename)
        assert session.calls == []

    @pytest.mark.parametrize("filename", ["résumé.pdf", "報告書.docx", "e\u0301" * 200 + ".txt", "a" * 255])
    def test_non_ascii_and_full_length_filenames_are_accepted(self, filename: str) -> None:
        client, session = _client(_Response(201, FILE_RESOURCE))
        upload_file(client, b"x", filename=filename)
        assert dict(session.calls[0][2]["files"])["file"][0] == filename


class TestContent:
    def test_a_stream_is_read_from_its_current_position(self) -> None:
        stream = io.BytesIO(b"headerbody")
        stream.seek(6)
        client, session = _client(_Response(201, FILE_RESOURCE))
        upload_file(client, stream, filename="body.txt")
        assert dict(session.calls[0][2]["files"])["file"][1] == b"body"

    def test_a_stream_that_returns_short_reads_is_read_to_the_end(self) -> None:
        class _Trickle(io.RawIOBase):
            def __init__(self, data: bytes) -> None:
                self._data = data

            def readable(self) -> bool:
                return True

            def read(self, size: int = -1) -> bytes:
                chunk, self._data = self._data[:3], self._data[3:]
                return chunk

        client, session = _client(_Response(201, FILE_RESOURCE))
        upload_file(client, _Trickle(b"abcdefgh"), filename="a.bin")  # type: ignore[arg-type]
        assert dict(session.calls[0][2]["files"])["file"][1] == b"abcdefgh"

    def test_a_text_stream_is_refused(self) -> None:
        client, session = _client()
        with pytest.raises(TypeError, match="binary mode"):
            upload_file(client, io.StringIO("text"), filename="a.txt")  # type: ignore[arg-type]
        assert session.calls == []

    @pytest.mark.parametrize("source", [b"", io.BytesIO(b"")])
    def test_empty_content_is_refused(self, source) -> None:
        client, session = _client()
        with pytest.raises(ValueError, match="empty"):
            upload_file(client, source, filename="a.txt")
        assert session.calls == []

    def test_an_empty_path_is_refused(self, tmp_path) -> None:
        path = tmp_path / "empty.txt"
        path.write_bytes(b"")
        client, session = _client()
        with pytest.raises(ValueError, match="empty"):
            upload_file(client, path)
        assert session.calls == []


class TestSizeLimit:
    def test_the_limit_is_100_mib(self) -> None:
        assert files_module.MAX_UPLOAD_BYTES == 100 * 1024 * 1024

    @pytest.fixture
    def small_limit(self, monkeypatch) -> int:
        monkeypatch.setattr(files_module, "MAX_UPLOAD_BYTES", 8)
        return 8

    def test_a_file_at_the_limit_is_sent(self, small_limit: int) -> None:
        client, session = _client(_Response(201, FILE_RESOURCE))
        upload_file(client, b"x" * small_limit, filename="a.bin")
        assert len(session.calls) == 1

    def test_oversized_bytes_are_refused(self, small_limit: int) -> None:
        client, session = _client()
        with pytest.raises(ValueError, match="100 MB"):
            upload_file(client, b"x" * (small_limit + 1), filename="a.bin")
        assert session.calls == []

    def test_an_oversized_path_is_refused_before_it_is_opened(self, small_limit: int, tmp_path, monkeypatch) -> None:
        path = tmp_path / "big.bin"
        path.write_bytes(b"x" * (small_limit + 1))

        def _no_open(*_args, **_kwargs):
            raise AssertionError("opened an oversized file")

        monkeypatch.setattr("builtins.open", _no_open)
        client, session = _client()
        with pytest.raises(ValueError, match="100 MB"):
            upload_file(client, path)
        assert session.calls == []

    def test_an_oversized_stream_is_read_only_past_the_limit(self, small_limit: int) -> None:
        stream = io.BytesIO(b"x" * 100)
        client, session = _client()
        with pytest.raises(ValueError, match="100 MB"):
            upload_file(client, stream, filename="a.bin")
        assert stream.tell() == small_limit + 1
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


def test_a_success_without_an_id_raises() -> None:
    client, _ = _client(_Response(201, {"name": "a.txt"}))
    with pytest.raises(ConversationsApiError, match="without a file id") as exc:
        upload_file(client, b"x", filename="a.txt")
    assert (exc.value.status_code, exc.value.trace_id) == (201, "trace_1")


def test_a_success_without_json_raises() -> None:
    client, _ = _client(_Response(201, "created"))
    with pytest.raises(ConversationsApiError, match="without a file id"):
        upload_file(client, b"x", filename="a.txt")


class TestErrors:
    def test_404_when_the_files_api_is_not_enabled(self) -> None:
        client, _ = _client(_Response(404, "API not found"))
        with pytest.raises(RequestRejectedError) as exc:
            upload_file(client, b"x", filename="a.txt")
        assert exc.value.status_code == 404

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

    def test_429s_past_max_retries_raise(self) -> None:
        client, session = _client(*[_Response(429, "slow down") for _ in range(4)])
        with pytest.raises(RateLimitedError):
            upload_file(client, b"x", filename="a.txt")
        assert len(session.calls) == 4

    def test_a_401_refreshes_the_bearer_and_retries_once(self) -> None:
        bearer = _CachingBearer()
        client, session = _client(_Response(401, "expired"), _Response(201, FILE_RESOURCE), bearer=bearer)
        upload_file(client, b"x", filename="a.txt")
        assert bearer.invalidated == 1
        assert [call[2]["headers"]["Authorization"] for call in session.calls] == ["Bearer token_0", "Bearer token_1"]
