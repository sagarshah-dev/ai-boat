"""The tools the model can ask for — its menu card. The model only CHOOSES; app/ai/agent.py
checks every argument and runs the tool.

Payer names and categories are enums, not just text in the description: a description is a
hint the model may ignore ("sagar bhai"), an enum limits what it can produce. The agent still
validates, because an enum is a strong hint, not a guarantee.
"""
from app.integrations.gemini import ToolSpec
from app.services.expenses import CATEGORIES

ADD_EXPENSE = "add_expense"
GET_BALANCES = "get_balances"
GET_RECENT = "get_recent"
SEARCH_EXPENSES = "search_expenses"
DELETE_LAST_EXPENSE = "delete_last_expense"

WRITE_TOOLS = {ADD_EXPENSE, DELETE_LAST_EXPENSE}


def _s(type_: str, description: str = "", **extra) -> dict:
    # An empty enum is invalid for Gemini; leave the field free and let the agent validate.
    out = {"type": type_, **{k: v for k, v in extra.items() if v != []}}
    if description:
        out["description"] = description
    return out


def expense_tools(payers: list[str]) -> list[ToolSpec]:
    names = ", ".join(payers)
    return [
        ToolSpec(
            name=ADD_EXPENSE,
            description="Add a new expense. Use whenever the user mentions an amount spent or paid.",
            parameters=_s("OBJECT", properties={
                "amount": _s("NUMBER", "Amount in rupees, exactly as written in the message. "
                                       "Never guess. Use 0 if the message has no amount."),
                "description": _s("STRING", "What the expense was for"),
                "paid_by": _s("STRING", f"Who paid. Exactly one of: {names}. "
                                        "If not mentioned, use the sender's name.", enum=payers),
                "category": _s("STRING", f"One of: {', '.join(CATEGORIES)}. Infer from context.",
                               enum=list(CATEGORIES)),
                "merchant": _s("STRING", "Who was paid — vendor or shop. Empty if not mentioned."),
                "date": _s("STRING", "Date as DD-MM-YYYY if the message mentions one ('yesterday', "
                                     "'on the 5th'). Empty if not mentioned."),
            }, required=["amount", "description", "paid_by"]),
        ),
        ToolSpec(
            name=GET_BALANCES,
            description="Current balances: total spent, fair share each, who has an amount pending "
                        "and who gets back, and the settlement plan. Use for 'balance', 'hisab', "
                        "'kitna baaki', 'who has to pay', 'pending amount' and the like.",
        ),
        ToolSpec(
            name=GET_RECENT,
            description="The most recent expense entries.",
            parameters=_s("OBJECT", properties={
                "n": _s("INTEGER", "How many entries. Default 5, at most 20."),
            }),
        ),
        ToolSpec(
            name=SEARCH_EXPENSES,
            description="Search and filter expenses: by payer, category, amount range, words in the "
                        "description or shop, and dates ('after 1 oct', 'last month', 'between 24 "
                        "and 26 sept'). ALWAYS pass date_from/date_to for any date condition — "
                        "never filter dates yourself.",
            parameters=_s("OBJECT", properties={
                "paid_by": _s("STRING", f"Filter by who paid. One of: {names}", enum=payers),
                "category": _s("STRING", f"One of: {', '.join(CATEGORIES)}", enum=list(CATEGORIES)),
                "amount_above": _s("NUMBER", "Only entries above this amount"),
                "amount_below": _s("NUMBER", "Only entries below this amount"),
                "text": _s("STRING", "Words to find in the description or shop name"),
                "date_from": _s("STRING", "Start date, inclusive, DD-MM-YYYY. Work out phrases like "
                                          "'last week' or 'gaya mahine' from today's date."),
                "date_to": _s("STRING", "End date, inclusive, DD-MM-YYYY. Empty = until today."),
            }),
        ),
        ToolSpec(
            name=DELETE_LAST_EXPENSE,
            description="Undo: remove the sender's OWN most recent unsettled expense. Use for "
                        "'undo', 'delete last entry', 'galti ho gayi'. It can never remove anyone "
                        "else's entry, so it takes no arguments.",
        ),
    ]
