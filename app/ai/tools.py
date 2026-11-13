"""The tools the model can ask for — its menu card. The model only CHOOSES; app/ai/agent.py
checks every argument and runs the tool.

Payer names and categories are enums, not just text in the description: a description is a
hint the model may ignore ("sagar bhai"), an enum limits what it can produce. The agent still
validates, because an enum is a strong hint, not a guarantee.
"""
from app.integrations.gemini import ToolSpec
from app.services.expenses import CATEGORIES

ADD_EXPENSES = "add_expenses"   # what the model calls: one call, every expense in the message
ADD_EXPENSE = "add_expense"     # one item of it, as the agent runs and checks it
GET_BALANCES = "get_balances"
GET_RECENT = "get_recent"
SEARCH_EXPENSES = "search_expenses"
DELETE_LAST_EXPENSE = "delete_last_expense"
EDIT_LAST_EXPENSE = "edit_last_expense"

WRITE_TOOLS = {ADD_EXPENSE, ADD_EXPENSES, DELETE_LAST_EXPENSE, EDIT_LAST_EXPENSE}


def _s(type_: str, description: str = "", **extra) -> dict:
    # An empty enum is invalid for Gemini; leave the field free and let the agent validate.
    out = {"type": type_, **{k: v for k, v in extra.items() if v != []}}
    if description:
        out["description"] = description
    return out


def expense_tools(payers: list[str]) -> list[ToolSpec]:
    names = ", ".join(payers)
    return [
        # ONE call with a list, not one call per expense. Live (29-10-2026) the small model made
        # 1 call for "150 tea, 250 vadapav, 500 sevsad" and stopped; whether a model makes
        # parallel calls is up to the model, while an array is part of the schema.
        ToolSpec(
            name=ADD_EXPENSES,
            description="Add new expenses. Use whenever the user mentions money spent or paid. "
                        "Put EVERY expense of the message in `items`, one item per amount, in "
                        "the order written. Example: '150 tea, 250 vadapav by lalo, coffee' -> "
                        "items: [{150, tea}, {250, vadapav, Lalo}, {0, coffee}]. An item with no "
                        "amount written gets amount 0 — the system asks for it. Gujarati '<name> e "
                        "<amount> <item> lidhi/lidhu/aapya' means <name> paid: 'Biren e 100 tea "
                        "lidhi' -> items: [{100, tea, Biren}].",
            parameters=_s("OBJECT", properties={
                "items": _s("ARRAY", "Every expense in the message, one per amount.",
                            items=_s("OBJECT", properties={
                                "amount": _s("NUMBER", "Amount in rupees, exactly as written for "
                                                       "THIS item. Never guess. 0 if not written."),
                                "description": _s("STRING", "What this item was for"),
                                "paid_by": _s("STRING", f"Who paid. Exactly one of: {names}. If "
                                                        "not mentioned, use the sender's name.",
                                              enum=payers),
                                "category": _s("STRING", f"One of: {', '.join(CATEGORIES)}. "
                                                         "Infer from context.",
                                               enum=list(CATEGORIES)),
                                "merchant": _s("STRING", "Who was paid — vendor or shop. Empty if "
                                                         "not mentioned."),
                                "date": _s("STRING", "DD-MM-YYYY if the message mentions one "
                                                     "('yesterday', 'on the 5th'). Empty if not."),
                            }, required=["amount", "description", "paid_by"])),
            }, required=["items"]),
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
            description="Undo: remove the sender's OWN most recent unsettled expense. Use only "
                        "when the user asks to remove it: 'undo', 'delete last entry', 'kadhi "
                        "nakh'. NOT for a correction like 'na, 150 hata' — that is "
                        "edit_last_expense. The user confirms before anything is deleted.",
        ),
        ToolSpec(
            name=EDIT_LAST_EXPENSE,
            description="Correct the sender's OWN most recent unsettled expense: a new amount "
                        "and/or item. Use for 'na, 150 hata', 'sorry 150 hata', 'tea nahi coffee "
                        "hati'. The user confirms before anything changes.",
            parameters=_s("OBJECT", properties={
                "amount": _s("NUMBER", "The corrected amount, exactly as written in the latest "
                                       "message. 0 if the amount is not being changed."),
                "description": _s("STRING", "The corrected item, only if the user changed it. "
                                            "Empty otherwise."),
            }),
        ),
    ]
