"""A fake Meta, so the WhatsApp behaviour can be tested without a number.

    .venv/bin/python run_whatsapp_sim.py

Everything WhatsApp-specific — typing indicators, reactions, message pacing,
the owner's decision buttons, the 24-hour service window — is invisible in the
local chat window, because that window is not WhatsApp. This is.

What is simulated is *Meta*, and nothing else. Your real webhook runs, with real
HMAC signature verification on the raw body, the real fast-200-then-work
handling, the real agent, the real database. Inbound messages are genuine Cloud
API envelopes, signed the way Meta signs them. Outbound sends are intercepted at
the last possible moment and printed as a phone would show them.

So anything that works here works on a real number, and anything broken here is
broken there too.

    <type anything>     speak as the customer
    /approve /decline /call   tap a button as the owner
    /owner <text>       reply as the owner in free text
    /cases              open cases and what each is waiting on
    /state              what the agent believes right now
    /skip 30h           move the clock, e.g. to shut the 24-hour window
    /reset              start a fresh conversation
    /quit
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from rental_agent.agent.loop import build_agent
from rental_agent.context import ToolContext
from rental_agent.services import handover
from rental_agent.store.db import create_db_engine, init_db
from rental_agent.whatsapp import window as window_mod
from rental_agent.whatsapp.client import SendResult, WhatsAppClient, split_message, to_whatsapp_markup
from rental_agent.whatsapp.settings import WhatsAppSettings
from rental_agent.whatsapp.webhook import create_app

CUSTOMER = "971500000001"
OWNER = "971500009999"
APP_SECRET = "simulator-app-secret"
VERIFY_TOKEN = "simulator-verify-token"

TZ = ZoneInfo("Asia/Dubai")
REFERENCE_DATE = date(2026, 9, 1)

DIM, BOLD, OFF = "\033[2m", "\033[1m", "\033[0m"
GREEN, GOLD, BLUE, RED = "\033[32m", "\033[33m", "\033[36m", "\033[31m"


class Clock:
    """A movable now. `/skip` advances it, which is how the 24-hour window is
    made to close without waiting a day for it."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)

    def __call__(self) -> datetime:
        return self.now

    def skip(self, hours: float) -> None:
        self.now += timedelta(hours=hours)


class Phone(WhatsAppClient):
    """Intercepts outbound sends and renders them as a handset would.

    Subclasses the real client rather than replacing it, so the message
    splitting, markdown normalisation and pacing schedule under test are the
    ones that would actually run.
    """

    def __init__(self, clock: Clock) -> None:
        super().__init__(
            WhatsAppSettings(
                phone_number_id="SIM",
                access_token="SIM",
                app_secret=APP_SECRET,
                verify_token=VERIFY_TOKEN,
                staff_number=OWNER,
                media_base_url="https://demo.invalid",
            )
        )
        self.clock = clock

    # -- rendering -------------------------------------------------------

    @staticmethod
    def _who(to: str) -> str:
        return "OWNER" if to == OWNER else "CUSTOMER"

    def _bubble(self, to: str, text: str, tint: str = GREEN) -> None:
        label = f"{tint}{BOLD}AGENT → {self._who(to)}{OFF}"
        body = "\n".join(f"          {line}" for line in text.splitlines())
        print(f"\n{label}\n{body}")

    def _note(self, text: str) -> None:
        print(f"{DIM}  · {text}{OFF}")

    # -- intercepted sends ------------------------------------------------

    def send_text(self, to, text, typing_for=None):
        parts = split_message(to_whatsapp_markup(text))
        for index, part in enumerate(parts):
            if index > 0:
                pause = self.pacer.pacing.compose_seconds(part)
                self._note(f"typing… ({pause:.1f}s pause before the next message)")
            self._bubble(to, part)
        return SendResult(ok=True, message_ids=[f"wamid.OUT{id(text) % 9999}"])

    def send_image(self, to, image_url, caption=None):
        name = image_url.rsplit("/", 1)[-1]
        cap = f" — “{caption}”" if caption else ""
        print(f"{DIM}          [photo] {name}{cap}{OFF}")
        return SendResult(ok=True, message_ids=["wamid.IMG"])

    def send_images(self, to, image_urls, caption=None, typing_for=None):
        print(f"\n{GREEN}{BOLD}AGENT → {self._who(to)}{OFF}")
        for index, url in enumerate(image_urls):
            if index > 0:
                self._note(f"({self.pacer.pacing.min_seconds:.1f}s pause)")
            self.send_image(to, url, caption if index == 0 else None)
        return SendResult(ok=True, message_ids=["wamid.IMG"])

    def send_buttons(self, to, body, buttons):
        self._bubble(to, body, tint=GOLD)
        tapped = "  ".join(f"[ {b['title']} ]" for b in buttons)
        print(f"{GOLD}          {tapped}{OFF}")
        self._note("tap with /approve, /decline or /call")
        return SendResult(ok=True, message_ids=["wamid.ASK"])

    def send_reaction(self, to, message_id, emoji):
        if emoji:
            self._note(f"reacted {emoji} to their message")
        return SendResult(ok=True)

    def send_template(self, to, name, *, language="en", body_params=None):
        print(f"\n{BLUE}{BOLD}AGENT → {self._who(to)}  [TEMPLATE]{OFF}")
        print(f"{BLUE}          {name} ({language}) params={body_params}{OFF}")
        self._note("the 24-hour window is shut — only an approved template gets through")
        return SendResult(ok=True, message_ids=["wamid.TPL"])

    def mark_read(self, message_id):
        self._note("✓✓ read")
        return SendResult(ok=True)

    def send_typing(self, message_id):
        self._note("✓✓ read · typing…")
        return SendResult(ok=True)


# --------------------------------------------------------------------------
# Genuine Cloud API envelopes, signed the way Meta signs them
# --------------------------------------------------------------------------


def envelope(message: dict) -> dict:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "97144000000", "phone_number_id": "SIM"},
        "contacts": [{"profile": {"name": "Youssef"}, "wa_id": message["from"]}],
        "messages": [message],
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }


def text_from(sender: str, body: str, message_id: str) -> dict:
    return envelope({
        "from": sender, "id": message_id, "timestamp": "1756713600",
        "type": "text", "text": {"body": body},
    })


def button_from(sender: str, button_id: str, title: str, message_id: str) -> dict:
    return envelope({
        "from": sender, "id": message_id, "timestamp": "1756713600",
        "type": "interactive",
        "interactive": {"type": "button_reply",
                        "button_reply": {"id": button_id, "title": title}},
    })


def deliver(client: TestClient, payload: dict) -> None:
    """POST it exactly as Meta would, signature and all."""
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhook", content=body,
        headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
    )
    if response.status_code != 200:
        print(f"{RED}  webhook returned {response.status_code}{OFF}")


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------


def context(session_factory, clock: Clock) -> ToolContext:
    session = session_factory()
    ctx = ToolContext(session=session, now_fn=clock, reference_date=REFERENCE_DATE)
    customer, _ = ctx.customers.get_or_create(CUSTOMER, clock())
    conversation, _ = ctx.conversations.get_or_create(customer.customer_id, clock())
    ctx.customer_id, ctx.conversation_id = customer.customer_id, conversation.conversation_id
    session.flush()
    return ctx


def show_cases(session_factory, clock: Clock) -> None:
    ctx = context(session_factory, clock)
    try:
        cases = handover.open_cases(ctx)
        decided = handover.decided_awaiting_relay(ctx, ctx.conversation_id or "")
        if not cases and decided is None:
            print(f"{DIM}  no open cases{OFF}")
        for case in cases:
            print(f"  {GOLD}{case.case_code}{OFF}  {case.reason}  · {case.status}")
            print(f"{DIM}        asked: {(case.question or '')[:70]}{OFF}")
        if decided is not None:
            print(f"  {BLUE}{decided.case_code}{OFF}  decided '{decided.decision}' "
                  f"— owed to the customer, not yet delivered")
        open_now = window_mod.is_open(ctx, ctx.conversation_id or "")
        left = window_mod.hours_left(ctx, ctx.conversation_id or "")
        print(f"{DIM}  24-hour window: {'open' if open_now else 'SHUT'}"
              f"{f' ({left:.1f}h left)' if open_now else ''}{OFF}")
    finally:
        ctx.session.close()


def show_state(session_factory, clock: Clock) -> None:
    ctx = context(session_factory, clock)
    try:
        state = ctx.load_state()
        prefs = state.vehicle_preferences
        print(f"{DIM}  stage      {state.stage.value}")
        print(f"  wants      {', '.join(prefs.models + prefs.makes + [c.value for c in prefs.categories]) or '—'}")
        print(f"  pickup     {state.pickup_at or '—'}")
        print(f"  return     {state.return_at or '—'}")
        print(f"  location   {state.delivery_location or '—'}")
        print(f"  quote      {state.quote_id or '—'}")
        print(f"  booking    {state.reservation_id or '—'}")
        print(f"  missing    {', '.join(state.missing_requirements()) or 'nothing'}")
        print(f"  escalated  {state.escalated}{OFF}")
    finally:
        ctx.session.close()


def newest_case_code(session_factory, clock: Clock) -> str | None:
    ctx = context(session_factory, clock)
    try:
        cases = handover.open_cases(ctx)
        return cases[0].case_code if cases else None
    finally:
        ctx.session.close()


# --------------------------------------------------------------------------


BANNER = f"""
{BOLD}  Sandline Rentals — WhatsApp simulator{OFF}
{DIM}  Your real webhook, real signatures, real agent. Only Meta is fake.

  <type anything>          speak as the customer
  /approve /decline /call  tap a button as the owner
  /owner <text>            reply as the owner in free text
  /cases                   open cases · /state  what the agent knows
  /skip 30h                move the clock (shuts the 24-hour window)
  /reset  /quit{OFF}
"""


def main() -> None:
    clock = Clock()
    phone = Phone(clock)
    session_factory = init_db(create_db_engine("whatsapp_sim.db"))

    agent = build_agent()
    print(BANNER)
    print(f"{DIM}  warming the model…{OFF}", end="", flush=True)
    took = agent.warm_up()
    print(f"{DIM} {took:.1f}s{OFF}\n" if took else f"{DIM} unavailable{OFF}\n")

    app = create_app(
        session_factory=session_factory,
        agent_factory=lambda: agent,
        settings=phone.settings,
        client=phone,
        reference_date=REFERENCE_DATE,
        now_fn=clock,
    )
    http = TestClient(app)
    counter = {"n": 0}

    def message_id(prefix: str) -> str:
        counter["n"] += 1
        return f"wamid.{prefix}{counter['n']}"

    while True:
        try:
            line = input(f"\n{BOLD}customer >{OFF} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue

        if line in ("/quit", "/exit"):
            return
        if line == "/cases":
            show_cases(session_factory, clock); continue
        if line == "/state":
            show_state(session_factory, clock); continue
        if line == "/help":
            print(BANNER); continue
        if line == "/reset":
            ctx = context(session_factory, clock)
            conversation = ctx.conversations.get(ctx.conversation_id or "")
            if conversation is not None:
                conversation.outcome = "reset"
            ctx.session.commit(); ctx.session.close()
            print(f"{DIM}  new conversation{OFF}"); continue

        if line.startswith("/skip"):
            parts = line.split()
            hours = float(parts[1].rstrip("hH")) if len(parts) > 1 else 30.0
            clock.skip(hours)
            print(f"{DIM}  clock is now {clock().strftime('%a %d %b, %-I:%M %p')}{OFF}")
            continue

        if line.startswith(("/approve", "/decline", "/call")):
            decision = line[1:].split()[0]
            code = newest_case_code(session_factory, clock)
            if code is None:
                print(f"{DIM}  no open case to answer{OFF}"); continue
            titles = {"approve": "Approve", "decline": "Decline", "call": "I'll call them"}
            print(f"\n{GOLD}{BOLD}OWNER{OFF} tapped [ {titles[decision]} ] on case {code}")
            deliver(http, button_from(OWNER, f"{decision}:{code}", titles[decision],
                                      message_id("OWN")))
            continue

        if line.startswith("/owner"):
            said = line[len("/owner"):].strip()
            if not said:
                show_cases(session_factory, clock); continue
            print(f"\n{GOLD}{BOLD}OWNER{OFF} {said}")
            deliver(http, text_from(OWNER, said, message_id("OWN")))
            continue

        deliver(http, text_from(CUSTOMER, line, message_id("CUS")))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
