"""Can several customers be served at once?

Not a pytest test — a measurement, run by hand:

    .venv/bin/python tests/concurrency_check.py 10

It fires N customers at the real webhook simultaneously, with a stub agent that
sleeps instead of calling a model, so what is measured is the plumbing —
threading, sessions, SQLite — and not the provider.

Two findings it produced, both worth keeping reproducible:

  * Before `PRAGMA busy_timeout`, 10 simultaneous customers got **5 replies**.
    SQLite refuses a second writer instantly rather than queueing it, so half
    the turns died at the first insert and those customers heard nothing.

  * With the timeout, all 10 are answered — but they **serialise**. A turn holds
    the write transaction from its first insert until commit, which spans the
    model call. On SQLite one customer at a time is the ceiling, so ten
    customers with a five-second turn means the last one waits nearly a minute.

The second is the case for PostgreSQL, whose writers do not block each other.
Re-run this after that migration; the expected result is concurrent again, with
no losses.
"""
import hashlib, hmac, json, sys, threading, time, tempfile, pathlib
from datetime import date, datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, ".")

from fastapi.testclient import TestClient
from rental_agent.agent.loop import AgentTurn
from rental_agent.store.db import create_db_engine, init_db
from rental_agent.whatsapp.client import SendResult, WhatsAppClient
from rental_agent.whatsapp.settings import WhatsAppSettings
from rental_agent.whatsapp.webhook import create_app

SECRET = "s"; TZ = ZoneInfo("Asia/Dubai"); NOW = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
TURN_SECONDS = 1.0

sent, errors, lock = [], [], threading.Lock()

class Recorder(WhatsAppClient):
    def __init__(self):
        super().__init__(WhatsAppSettings(phone_number_id="P", access_token="T", app_secret=SECRET))
    def send_text(self, to, text, typing_for=None):
        with lock: sent.append(to)
        return SendResult(ok=True, message_ids=["x"])
    def send_typing(self, m): return SendResult(ok=True)
    def mark_read(self, m): return SendResult(ok=True)

class SlowAgent:
    def respond(self, ctx, message, *, provider_message_id=None, media=None):
        ctx.messages.record(conversation_id=ctx.conversation_id or "", direction="inbound",
                            content=message, now=ctx.now(), provider_message_id=provider_message_id)
        time.sleep(TURN_SECONDS)          # a realistic turn
        state = ctx.load_state()
        state.delivery_location = f"zone-{message[-2:]}"
        ctx.save_state(state)              # a write, like a real turn
        return AgentTurn(reply=f"reply to {message}")

app = create_app(
    session_factory=init_db(create_db_engine(pathlib.Path(tempfile.mkdtemp()) / "c.db")),
    agent_factory=SlowAgent, client=Recorder(),
    settings=WhatsAppSettings(phone_number_id="P", access_token="T",
                              app_secret=SECRET, verify_token="v"),
    reference_date=date(2026, 9, 1), now_fn=lambda: NOW,
)
http = TestClient(app)

def envelope(sender, body, mid):
    v = {"messaging_product":"whatsapp","metadata":{"phone_number_id":"P"},
         "messages":[{"from":sender,"id":mid,"timestamp":"1","type":"text","text":{"body":body}}]}
    return {"object":"whatsapp_business_account","entry":[{"id":"W","changes":[{"field":"messages","value":v}]}]}

def customer(i):
    body = json.dumps(envelope(f"9715000000{i:02d}", f"hello from {i:02d}", f"wamid.{i}")).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    try:
        r = http.post("/webhook", content=body,
                      headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
        if r.status_code != 200:
            with lock: errors.append(f"HTTP {r.status_code}")
    except Exception as e:
        with lock: errors.append(f"{type(e).__name__}: {str(e)[:60]}")

start = time.time()
threads = [threading.Thread(target=customer, args=(i,)) for i in range(N)]
for t in threads: t.start()
for t in threads: t.join()
elapsed = time.time() - start

print(f"  {N} customers, each turn takes {TURN_SECONDS}s")
print(f"  wall clock      : {elapsed:.1f}s")
print(f"  if serial       : {N * TURN_SECONDS:.1f}s")
print(f"  replies sent    : {len(sent)}/{N}")
print(f"  errors          : {errors or 'none'}")
print(f"  -> {'CONCURRENT' if elapsed < N * TURN_SECONDS * 0.6 else 'SERIAL'}")
