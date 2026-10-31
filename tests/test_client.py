"""TelegramClient against a fake HTTP server: retries, give-ups and the never-raise promise."""
import httpx
import pytest

from app.telegram import client as client_module
from app.telegram.client import TelegramClient


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_module.time, "sleep", lambda _s: None)


def make(responses):
    """Client whose HTTP calls return the given responses (or raise given exceptions) in order."""
    calls = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return TelegramClient("TOKEN123456", http=http), calls


def test_success_first_try():
    c, calls = make([(200, {"ok": True, "result": {"message_id": 1}})])
    assert c.send_message(5, "hi") is True
    assert len(calls) == 1
    assert calls[0].url.path == "/botTOKEN123456/sendMessage"


def test_retries_server_error_then_succeeds():
    c, calls = make([(502, {"ok": False}), (200, {"ok": True, "result": {}})])
    assert c.send_message(5, "hi") is True
    assert len(calls) == 2


def test_retries_network_error_then_succeeds():
    c, calls = make([httpx.ConnectTimeout("slow"), (200, {"ok": True, "result": {}})])
    assert c.send_message(5, "hi") is True
    assert len(calls) == 2


def test_gives_up_after_three_tries_without_raising():
    c, calls = make([httpx.ConnectError("down")] * 3)
    assert c.send_message(5, "hi") is False
    assert len(calls) == 3


def test_client_error_is_not_retried():
    c, calls = make([(400, {"ok": False, "description": "chat not found"})])
    assert c.send_message(5, "hi") is False
    assert len(calls) == 1


def test_rate_limit_is_retried():
    c, calls = make([(429, {"ok": False, "parameters": {"retry_after": 1}}),
                     (200, {"ok": True, "result": {}})])
    assert c.send_message(5, "hi") is True
    assert len(calls) == 2


def test_buttons_are_sent_as_inline_keyboard():
    c, calls = make([(200, {"ok": True, "result": {}})])
    c.send_message(5, "pick", buttons=[[("Yes", "y"), ("No", "n")]])
    body = calls[0].read().decode()
    assert '"inline_keyboard"' in body and '"callback_data":"y"' in body.replace(" ", "")


def test_callback_data_over_64_bytes_is_a_programming_error():
    c, _ = make([])
    with pytest.raises(ValueError):
        c.send_message(5, "x", buttons=[[("label", "ક" * 30)]])  # 90 bytes in UTF-8


# ── A send that may already have arrived is never sent again (live bug, 29-10-2026) ─────────

@pytest.mark.parametrize("exc", [httpx.ReadTimeout("no answer"), httpx.WriteTimeout("half sent"),
                                 httpx.RemoteProtocolError("dropped"), httpx.ReadError("reset")])
def test_send_message_is_not_resent_when_it_may_have_arrived(exc):
    c, calls = make([exc, (200, {"ok": True, "result": {}})])
    assert c.send_message(5, "hi") is False
    assert len(calls) == 1


def test_send_message_reply_that_is_not_json_is_not_resent():
    def handler(request):
        return httpx.Response(200, content=b"<html>gateway</html>")
    calls = []
    http = httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r), handler(r))[1]))
    assert TelegramClient("T", http=http).send_message(5, "hi") is False
    assert len(calls) == 1


@pytest.mark.parametrize("exc", [httpx.ConnectError("down"), httpx.ConnectTimeout("slow"),
                                 httpx.PoolTimeout("busy")])
def test_send_message_is_retried_when_it_never_left(exc):
    c, calls = make([exc, (200, {"ok": True, "result": {}})])
    assert c.send_message(5, "hi") is True
    assert len(calls) == 2


def test_send_photo_is_not_resent_after_read_timeout():
    c, calls = make([httpx.ReadTimeout("no answer"), (200, {"ok": True, "result": {}})])
    assert c.send_photo(5, "FILE") is False
    assert len(calls) == 1


def test_send_document_is_not_resent_after_read_timeout():
    c, calls = make([httpx.ReadTimeout("no answer"), (200, {"ok": True})])
    assert c.send_document(5, "r.pdf", b"%PDF") is False
    assert len(calls) == 1


def test_send_document_retried_when_it_never_left():
    c, calls = make([httpx.ConnectError("down"), (200, {"ok": True})])
    assert c.send_document(5, "r.pdf", b"%PDF") is True
    assert len(calls) == 2


@pytest.mark.parametrize("call", [
    lambda c: c.edit_message_text(5, 9, "x"),
    lambda c: c.clear_buttons(5, 9),
    lambda c: c.answer_callback("cb"),
])
def test_edits_and_button_answers_are_still_retried_after_read_timeout(call):
    # Repeating an edit changes nothing the user can see, so these keep their retries.
    c, calls = make([httpx.ReadTimeout("no answer"), (200, {"ok": True, "result": True})])
    assert call(c) is True
    assert len(calls) == 2
