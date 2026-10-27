"""The Gemini wrapper's retry rules, against a fake SDK client (no network)."""
from types import SimpleNamespace

import pytest
from google.genai import errors

from app.integrations.gemini import GeminiChat, LlmError, ToolSpec, error_kind


def ok_response(text="hi", calls=None):
    fcs = [SimpleNamespace(name=n, args=a) for n, a in (calls or [])]
    return SimpleNamespace(
        candidates=[SimpleNamespace(content="model-turn")], function_calls=fcs or None,
        text=text, usage_metadata=SimpleNamespace(total_token_count=42))


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.models_called = []

    def generate_content(self, model, contents, config):
        self.models_called.append(model)
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def chat(outcomes, models=("main",), budget=55.0, clock=None):
    fake = SimpleNamespace(models=FakeModels(outcomes))
    tools = [ToolSpec("add_expense", "add", {"type": "OBJECT", "properties": {
        "amount": {"type": "NUMBER"}, "paid_by": {"type": "STRING", "enum": ["Sagar"]}}})]
    c = GeminiChat(fake, list(models), "system", tools, budget, sleep=lambda s: None,
                   clock=clock or (lambda: 0.0))
    return c, fake.models


def server(code):
    return errors.ServerError(code, {"error": {"message": "x"}})


def client_err(code):
    return errors.ClientError(code, {"error": {"message": "x"}})


def test_plain_answer():
    c, m = chat([ok_response("hello")])
    turn = c.send_user("hi")
    assert (turn.text, turn.calls, turn.tokens) == ("hello", [], 42)


def test_tool_call_is_returned_not_text():
    c, _ = chat([ok_response(text="ignored", calls=[("add_expense", {"amount": 5})])])
    turn = c.send_user("5 tea")
    assert turn.calls[0].name == "add_expense" and turn.calls[0].args == {"amount": 5}
    assert turn.text == ""


def test_overloaded_is_retried_then_succeeds():
    c, m = chat([server(503), server(503), ok_response()])
    assert c.send_user("hi").text == "hi"
    assert m.models_called == ["main", "main", "main"]


def test_overloaded_three_times_moves_to_fallback():
    c, m = chat([server(503)] * 3 + [ok_response()], models=("main", "backup"))
    c.send_user("hi")
    assert m.models_called == ["main"] * 3 + ["backup"]
    assert c.model_used == "backup"


def test_timeout_skips_straight_to_fallback():
    c, m = chat([server(504), ok_response()], models=("main", "backup"))
    c.send_user("hi")
    assert m.models_called == ["main", "backup"]


def test_bad_request_is_not_retried():
    c, m = chat([client_err(400)])
    with pytest.raises(LlmError) as e:
        c.send_user("hi")
    assert e.value.kind == "config" and m.models_called == ["main"]


def test_gives_up_with_the_right_kind():
    c, _ = chat([client_err(429)] * 3)
    with pytest.raises(LlmError) as e:
        c.send_user("hi")
    assert e.value.kind == "quota"


def test_time_budget_stops_retrying():
    t = {"now": 0.0}
    c, m = chat([server(503)] * 5, budget=30.0, clock=lambda: t["now"])
    t["now"] = 15.0           # 15 s used: a 20 s call would overrun the 30 s budget
    with pytest.raises(LlmError):
        c.send_user("hi")
    assert m.models_called == []


@pytest.mark.parametrize("err,kind", [
    (server(503), "overloaded"), (server(500), "overloaded"), (server(504), "timeout"),
    (client_err(429), "quota"), (client_err(403), "config"), (ValueError("x"), "unknown"),
    (TimeoutError("x"), "timeout"),
])
def test_error_kind(err, kind):
    assert error_kind(err) == kind


@pytest.mark.parametrize("people", [["Sagar", "Biren", "Lalo"], []])
def test_real_tool_specs_convert_to_gemini_declarations(people):
    from app.ai.tools import expense_tools
    from app.integrations.gemini import _declaration
    decls = [_declaration(t) for t in expense_tools(people)]
    assert [d.name for d in decls] == ["add_expense", "get_balances", "get_recent",
                                       "search_expenses", "delete_last_expense"]
    assert decls[0].parameters.properties["paid_by"].enum == (people or None)
