"""End-to-end through the real webhook route: Telegram JSON in, database + replies out."""
import itertools

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Member, ProcessedUpdate
from app.db.session import session_scope
from app.main import app
from app.telegram import webhook
from app.telegram.webhook import get_llm, get_telegram
from tests.conftest import ADMIN_ID

SECRET = "test-secret-0123456789"
FRIEND_ID = 2002
_ids = itertools.count(1)


@pytest.fixture
def client(tg, llm):
    app.dependency_overrides[get_telegram] = lambda: tg
    app.dependency_overrides[get_llm] = lambda: llm
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def msg(user_id, text, first_name="User", username=None, chat_id=None):
    sender = {"id": user_id, "first_name": first_name}
    if username:
        sender["username"] = username
    return {"update_id": next(_ids), "message": {
        "message_id": next(_ids), "from": sender, "chat": {"id": chat_id or user_id}, "text": text}}


def button(user_id, data, chat_id=None, message_id=500):
    return {"update_id": next(_ids), "callback_query": {
        "id": f"cb{next(_ids)}", "from": {"id": user_id, "first_name": "X"}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat_id or user_id}}}}


def post(client, update, secret=SECRET):
    return client.post("/telegram/webhook", json=update,
                       headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def member(telegram_id) -> Member | None:
    with session_scope() as s:
        return s.scalar(select(Member).where(Member.telegram_user_id == telegram_id))


def setup_admin(client):
    post(client, msg(ADMIN_ID, "/start", first_name="Sagar"))


def request_from_friend(client, tg) -> int:
    post(client, msg(FRIEND_ID, "/start", first_name="Ramesh", username="ramesh_k"))
    return member(FRIEND_ID).id


# ── Webhook security ───────────────────────────────────────────────────────────────────────

def test_wrong_secret_is_rejected_and_nothing_happens(client, tg):
    r = post(client, msg(ADMIN_ID, "/start"), secret="wrong-secret-wrong-secret")
    assert r.status_code == 401
    assert tg.sent == []
    assert member(ADMIN_ID) is None


def test_missing_secret_header_is_rejected(client, tg):
    r = client.post("/telegram/webhook", json=msg(ADMIN_ID, "/start"))
    assert r.status_code == 401


# ── Admin bootstrap ────────────────────────────────────────────────────────────────────────

def test_admin_start_creates_active_admin(client, tg):
    r = post(client, msg(ADMIN_ID, "/start", first_name="Sagar"))
    assert r.status_code == 200
    m = member(ADMIN_ID)
    assert (m.role, m.status, m.name) == ("admin", "active", "Sagar")
    assert m.joined_at is not None
    assert "You are the admin" in tg.texts_to(ADMIN_ID)[0]


def test_admin_start_twice_keeps_one_row(client, tg):
    setup_admin(client)
    setup_admin(client)
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Member)) == 1


def test_start_in_group_form_with_bot_name(client, tg):
    post(client, msg(ADMIN_ID, "/start@AIBoatHQ_bot", chat_id=-100555))
    assert member(ADMIN_ID).role == "admin"
    assert tg.sent[0]["chat_id"] == -100555


def test_without_admin_configured_user_is_told_their_id(client, tg, monkeypatch):
    settings = get_settings().model_copy(update={"admin_telegram_id": 0})
    app.dependency_overrides[get_settings] = lambda: settings
    post(client, msg(FRIEND_ID, "/start"))
    assert member(FRIEND_ID) is None
    assert str(FRIEND_ID) in tg.texts_to(FRIEND_ID)[0]


# ── Access requests ────────────────────────────────────────────────────────────────────────

def test_stranger_message_is_refused(client, tg):
    post(client, msg(FRIEND_ID, "100 tea"))
    assert "don't have access" in tg.texts_to(FRIEND_ID)[0]
    assert member(FRIEND_ID) is None


def test_start_creates_pending_request_and_admin_gets_buttons(client, tg):
    setup_admin(client)
    member_id = request_from_friend(client, tg)
    m = member(FRIEND_ID)
    assert (m.status, m.role, m.name, m.telegram_username) == ("pending", "guest", "Ramesh", "ramesh_k")
    assert "Request sent" in tg.texts_to(FRIEND_ID)[0]
    admin_msg = [x for x in tg.sent if x["chat_id"] == ADMIN_ID][-1]
    assert "Ramesh (@ramesh_k)" in admin_msg["text"] and str(FRIEND_ID) in admin_msg["text"]
    datas = [d for row in admin_msg["buttons"] for _, d in row]
    assert datas == [f"join:{member_id}:owner", f"join:{member_id}:guest", f"join:{member_id}:reject"]


def test_pending_user_cannot_use_bot(client, tg):
    setup_admin(client)
    request_from_friend(client, tg)
    post(client, msg(FRIEND_ID, "100 tea"))
    assert "don't have access" in tg.texts_to(FRIEND_ID)[-1]


def test_repeated_start_does_not_spam_admin(client, tg):
    setup_admin(client)
    request_from_friend(client, tg)
    before = len(tg.texts_to(ADMIN_ID))
    post(client, msg(FRIEND_ID, "/start", first_name="Ramesh"))
    assert len(tg.texts_to(ADMIN_ID)) == before
    assert "waiting for the admin" in tg.texts_to(FRIEND_ID)[-1]


# ── Admin decisions ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("choice,label", [("owner", "Owner"), ("guest", "Guest")])
def test_admin_approves(client, tg, choice, label):
    setup_admin(client)
    member_id = request_from_friend(client, tg)
    post(client, button(ADMIN_ID, f"join:{member_id}:{choice}"))
    m = member(FRIEND_ID)
    assert (m.status, m.role) == ("active", choice)
    assert m.joined_at is not None
    assert tg.edits[-1]["text"] == f"✅ Ramesh approved as {label}."
    assert f"approved as {label}" in tg.texts_to(FRIEND_ID)[-1]
    # Approved member can now use the bot.
    post(client, msg(FRIEND_ID, "balance"))
    assert "Balance Status" in tg.texts_to(FRIEND_ID)[-1]


def test_admin_rejects(client, tg):
    setup_admin(client)
    member_id = request_from_friend(client, tg)
    post(client, button(ADMIN_ID, f"join:{member_id}:reject"))
    assert member(FRIEND_ID).status == "removed"
    assert tg.edits[-1]["text"] == "❌ Ramesh rejected."
    assert "not approved" in tg.texts_to(FRIEND_ID)[-1]
    post(client, msg(FRIEND_ID, "hello"))
    assert "don't have access" in tg.texts_to(FRIEND_ID)[-1]


def test_rejected_user_can_ask_again(client, tg):
    setup_admin(client)
    member_id = request_from_friend(client, tg)
    post(client, button(ADMIN_ID, f"join:{member_id}:reject"))
    post(client, msg(FRIEND_ID, "/start", first_name="Ramesh"))
    assert member(FRIEND_ID).status == "pending"
    assert "New access request" in tg.texts_to(ADMIN_ID)[-1]


def test_non_admin_cannot_press_approve(client, tg):
    setup_admin(client)
    member_id = request_from_friend(client, tg)
    post(client, button(FRIEND_ID, f"join:{member_id}:owner"))
    assert member(FRIEND_ID).status == "pending"
    assert tg.answers[-1]["text"] == "Only the admin can do this."


def test_approved_owner_still_cannot_approve_others(client, tg):
    setup_admin(client)
    friend_id = request_from_friend(client, tg)
    post(client, button(ADMIN_ID, f"join:{friend_id}:owner"))
    post(client, msg(3003, "/start", first_name="Stranger"))
    stranger_id = member(3003).id
    post(client, button(FRIEND_ID, f"join:{stranger_id}:owner"))  # active owner, not admin
    assert member(3003).status == "pending"
    assert tg.answers[-1]["text"] == "Only the admin can do this."


def test_approve_pressed_twice_is_harmless(client, tg):
    setup_admin(client)
    member_id = request_from_friend(client, tg)
    post(client, button(ADMIN_ID, f"join:{member_id}:owner"))
    post(client, button(ADMIN_ID, f"join:{member_id}:reject"))
    assert (member(FRIEND_ID).status, member(FRIEND_ID).role) == ("active", "owner")
    assert tg.answers[-1]["text"] == "Already decided."


@pytest.mark.parametrize("data", ["join:999:owner", "join:abc:owner", "join:1:admin", "junk", ""])
def test_bad_or_stale_buttons(client, tg, data):
    setup_admin(client)
    r = post(client, button(ADMIN_ID, data))
    assert r.status_code == 200
    assert tg.answers[-1]["text"] in ("This button is no longer valid.", "Request not found.")


# ── Duplicates and failures ────────────────────────────────────────────────────────────────

def test_telegram_retry_of_same_update_is_processed_once(client, tg):
    update = msg(ADMIN_ID, "/start", first_name="Sagar")
    post(client, update)
    post(client, update)
    assert len(tg.texts_to(ADMIN_ID)) == 1
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(ProcessedUpdate)) == 1


def test_internal_error_returns_200_and_apologises(client, tg, monkeypatch):
    def boom(*_):
        raise RuntimeError("simulated bug")
    monkeypatch.setattr(webhook, "route", boom)
    r = post(client, msg(ADMIN_ID, "hello"))
    assert r.status_code == 200
    assert "Something went wrong" in tg.texts_to(ADMIN_ID)[0]


def test_failed_route_rolls_back_but_update_stays_claimed(client, tg, monkeypatch):
    real_route = webhook.route

    def route_then_crash(session, update, *rest):
        real_route(session, update, *rest)     # creates the admin row...
        raise RuntimeError("crash after the write")  # ...which must then be rolled back
    monkeypatch.setattr(webhook, "route", route_then_crash)
    update = msg(ADMIN_ID, "/start")
    post(client, update)
    assert member(ADMIN_ID) is None
    monkeypatch.setattr(webhook, "route", real_route)
    post(client, update)  # Telegram retry of the same update: at-most-once, so skipped
    assert member(ADMIN_ID) is None


def test_replies_failing_do_not_break_the_request(client, tg):
    tg.fail = True
    r = post(client, msg(ADMIN_ID, "/start"))
    assert r.status_code == 200
    assert member(ADMIN_ID).status == "active"


@pytest.mark.parametrize("update", [
    {"message": {"text": "no update id"}},
    {"update_id": 77},
    {"update_id": 78, "edited_message": {"text": "x"}},
    {"update_id": 79, "message": {"message_id": 1, "chat": {"id": 5}, "text": "channel, no from"}},
])
def test_unusual_updates_are_ignored_quietly(client, tg, update):
    assert post(client, update).status_code == 200
    assert tg.sent == []
