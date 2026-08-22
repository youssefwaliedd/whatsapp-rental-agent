"""Drive the eight required scenarios through the real webhook and grade them.

Not a pytest test — a run, done by hand against a live model:

    .venv/bin/python tests/scenario_run.py          # all of them
    .venv/bin/python tests/scenario_run.py 2 4 8    # just those

Every scenario gets its own customer number, so nothing one conversation
established can leak into the next and flatter the results.

Grading is in two parts. The **expectations** are what a rental salesperson
would obviously have to do — call the pricing tool before naming a total, reach
for alternatives rather than improvising when a car is gone, escalate an
accident. Those are asserted here.

The **findings** come from the project's own evaluator, unchanged, reading the
recorded transcript and tool-call audit log exactly as it would in production.
That is the stronger half: it checks for figures no tool produced, discounts
offered without authority, escalations missed, options over-listed. If a
scenario looks fine to a human reading it and the evaluator disagrees, the
evaluator is usually right — that is the entire reason it exists.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from rental_agent.agent.loop import build_agent  # noqa: E402
from rental_agent.context import ToolContext  # noqa: E402
from rental_agent.evaluation.evaluator import evaluate_conversation  # noqa: E402
from rental_agent.store.db import create_db_engine, init_db  # noqa: E402
from rental_agent.whatsapp.client import SendResult, WhatsAppClient  # noqa: E402
from rental_agent.whatsapp.settings import WhatsAppSettings  # noqa: E402
from rental_agent.whatsapp.webhook import create_app  # noqa: E402

TZ = ZoneInfo("Asia/Dubai")
REFERENCE_DATE = date(2026, 9, 1)
NOW = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)
SECRET = "scenario-secret"
STAFF = "971566699228"

DIM, BOLD, OFF = "\033[2m", "\033[1m", "\033[0m"
GREEN, RED, GOLD, BLUE = "\033[32m", "\033[31m", "\033[33m", "\033[36m"


@dataclass
class Scenario:
    number: int
    name: str
    says: list[str]
    #: Tools that must have been called somewhere in the conversation.
    must_call: list[str] = field(default_factory=list)
    #: Tools that must NOT have been called — usually the improvised path.
    must_not_call: list[str] = field(default_factory=list)
    must_escalate: bool = False
    #: Substrings that must appear somewhere in what the customer was told.
    must_say: list[str] = field(default_factory=list)


SCENARIOS = [
    Scenario(
        1, "Requested vehicle is available",
        ["hi, do you have a g63 this friday 7pm until sunday 7pm?",
         "deliver it to dubai marina please",
         "yes go ahead and book it"],
        must_call=["search_available_vehicles", "create_demo_quote", "create_demo_reservation"],
    ),
    Scenario(
        2, "Unavailable → targeted alternatives",
        ["i want the lamborghini huracan from friday 7pm to sunday 7pm, dubai marina",
         "what else is like it?"],
        must_call=["find_alternatives"],
    ),
    Scenario(
        3, "Customer has a budget",
        ["i need an suv this friday 7pm to sunday 7pm in dubai marina, budget is 1200 a day",
         "whats the cheapest you have"],
        must_call=["search_available_vehicles"],
    ),
    Scenario(
        4, "Customer asks for a discount",
        ["g63 friday 7pm to sunday 7pm, dubai marina",
         "any discount if i pay cash?",
         "come on, 20% and i'll book right now"],
        must_call=["get_allowed_discount"],
    ),
    Scenario(
        5, "Changes the delivery time",
        ["book me the g63 friday 7pm to sunday 7pm, dubai marina",
         "yes book it",
         "actually can you make it 8 instead"],
        must_call=["modify_demo_reservation"],
    ),
    Scenario(
        6, "Returns with a contextual follow-up",
        ["book the g63 friday 7pm to sunday 7pm, dubai marina",
         "yes please",
         "whats the total on that again?"],
        must_call=["get_active_reservation"],
    ),
    Scenario(
        7, "Requests an extension",
        ["book the g63 friday 7pm to sunday 7pm, dubai marina",
         "yes book it",
         "can i keep it until wednesday instead?"],
        must_call=["extend_demo_rental"],
    ),
    Scenario(
        8, "Reports an accident",
        ["i've just had an accident on sheikh zayed road, the car is damaged"],
        must_escalate=True,
        must_say=["999"],
    ),
    Scenario(
        9, "Policy question the rules file cannot answer",
        ["do you accept egyptian driving licences?"],
        must_call=["search_company_policy"],
    ),
    Scenario(
        10, "Dates that have already passed",
        ["i want a g63 from 25 august to 30 august"],
        must_not_call=["create_demo_reservation"],
    ),
]


class Recorder(WhatsAppClient):
    """Captures what the customer and the owner would have received."""

    def __init__(self):
        super().__init__(WhatsAppSettings(
            phone_number_id="SIM", access_token="SIM", app_secret=SECRET,
            verify_token="v", staff_number=STAFF, media_base_url="https://demo.invalid"))
        self.to_customer: list[str] = []
        self.to_owner: list[str] = []
        self.photos = 0
        self.reactions: list[str] = []

    def send_text(self, to, text, typing_for=None):
        (self.to_owner if to == STAFF else self.to_customer).append(text)
        return SendResult(ok=True, message_ids=["wamid.OUT"])

    def send_buttons(self, to, body, buttons):
        self.to_owner.append(body)
        return SendResult(ok=True, message_ids=["wamid.ASK"])

    def send_image(self, to, image_url, caption=None):
        self.photos += 1
        return SendResult(ok=True, message_ids=["wamid.IMG"])

    def send_reaction(self, to, message_id, emoji):
        self.reactions.append(emoji)
        return SendResult(ok=True)

    def send_template(self, to, name, *, language="en", body_params=None):
        return SendResult(ok=True, message_ids=["wamid.TPL"])

    def mark_read(self, message_id):
        return SendResult(ok=True)

    def send_typing(self, message_id):
        return SendResult(ok=True)


def envelope(sender: str, body: str, message_id: str) -> dict:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": "SIM"},
        "contacts": [{"profile": {"name": "Youssef"}, "wa_id": sender}],
        "messages": [{"from": sender, "id": message_id, "timestamp": "1",
                      "type": "text", "text": {"body": body}}],
    }
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "W", "changes": [{"field": "messages", "value": value}]}]}


def deliver(http: TestClient, payload: dict) -> None:
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    http.post("/webhook", content=raw,
              headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"})


def run(scenario: Scenario, session_factory, agent) -> tuple[bool, list[str]]:
    number = f"9715000{scenario.number:05d}"
    recorder = Recorder()
    app = create_app(session_factory=session_factory, agent_factory=lambda: agent,
                     settings=recorder.settings, client=recorder,
                     reference_date=REFERENCE_DATE, now_fn=lambda: NOW)
    http = TestClient(app)

    print(f"\n{BOLD}── {scenario.number}. {scenario.name}{OFF}")
    started = time.time()
    for index, said in enumerate(scenario.says, 1):
        print(f"  {BLUE}»{OFF} {said}")
        deliver(http, envelope(number, said, f"wamid.S{scenario.number}M{index}"))
        reply = recorder.to_customer[-1] if recorder.to_customer else "(nothing)"
        for line in reply.splitlines() or [""]:
            print(f"    {line}")
    elapsed = time.time() - started

    # What actually happened, read from the audit log rather than the reply text.
    with session_factory() as session:
        ctx = ToolContext(session=session, now_fn=lambda: NOW, reference_date=REFERENCE_DATE)
        customer, _ = ctx.customers.get_or_create(number, NOW)
        conversation, _ = ctx.conversations.get_or_create(customer.customer_id, NOW)
        ctx.customer_id, ctx.conversation_id = customer.customer_id, conversation.conversation_id
        session.flush()

        called = [c.tool_name for c in ctx.tool_calls.for_conversation(ctx.conversation_id)]
        succeeded = [c.tool_name for c in ctx.tool_calls.for_conversation(ctx.conversation_id)
                     if c.status != "error"]
        state = ctx.load_state()
        result = evaluate_conversation(ctx, ctx.conversation_id)
        session.commit()

    problems: list[str] = []
    for tool in scenario.must_call:
        if tool not in succeeded:
            problems.append(f"never called {tool} (called: {sorted(set(called)) or 'nothing'})")
    for tool in scenario.must_not_call:
        if tool in succeeded:
            problems.append(f"called {tool} and should not have")
    if scenario.must_escalate and not state.escalated:
        problems.append("did not escalate")
    spoken = " ".join(recorder.to_customer).lower()
    for phrase in scenario.must_say:
        if phrase.lower() not in spoken:
            problems.append(f"never said {phrase!r}")
    for finding in result.findings:
        problems.append(f"[evaluator/{finding.severity}] {finding.type}: {finding.bad_behavior}")

    print(f"  {DIM}{elapsed:.0f}s · tools: {', '.join(sorted(set(succeeded))) or 'none'}"
          f"{' · escalated' if state.escalated else ''}"
          f"{f' · {recorder.photos} photos' if recorder.photos else ''}"
          f"{f' · owner notified' if recorder.to_owner else ''}{OFF}")
    if problems:
        for problem in problems:
            print(f"  {RED}✗ {problem}{OFF}")
    else:
        print(f"  {GREEN}✓ passed{OFF}")
    return not problems, problems


def main() -> None:
    wanted = {int(a) for a in sys.argv[1:] if a.isdigit()}
    scenarios = [s for s in SCENARIOS if not wanted or s.number in wanted]

    import tempfile
    session_factory = init_db(create_db_engine(Path(tempfile.mkdtemp()) / "scenarios.db"))
    agent = build_agent()
    agent.warm_up()

    results = [(s, *run(s, session_factory, agent)) for s in scenarios]
    passed = sum(1 for _, ok, _ in results if ok)

    print(f"\n{BOLD}{'═' * 60}{OFF}")
    for scenario, ok, problems in results:
        mark = f"{GREEN}pass{OFF}" if ok else f"{RED}FAIL{OFF}"
        print(f"  {mark}  {scenario.number}. {scenario.name}")
        for problem in problems:
            print(f"        {DIM}{problem}{OFF}")
    print(f"\n  {passed}/{len(results)} scenarios clean\n")


if __name__ == "__main__":
    main()
