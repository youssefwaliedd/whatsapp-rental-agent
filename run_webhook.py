"""Start the WhatsApp webhook.

    .venv/bin/python run_webhook.py

Then expose it so Meta can reach it:

    cloudflared tunnel --url http://localhost:8000

and paste the resulting https URL + your WHATSAPP_VERIFY_TOKEN into
Meta → WhatsApp → Configuration → Webhook, subscribed to `messages`.

Runs happily without credentials — /health reports what is still missing.
"""

from __future__ import annotations

import logging

import uvicorn

from rental_agent.agent.loop import build_agent
from rental_agent.store.db import create_db_engine, init_db
from rental_agent.whatsapp.settings import WhatsAppSettings
from rental_agent.whatsapp.webhook import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")

settings = WhatsAppSettings()
session_factory = init_db(create_db_engine())

#: One agent for the process. Conversation state lives in the database, not in
#: the agent, so a single instance serves every customer safely.
_agent = None


def agent_factory():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


app = create_app(
    session_factory=session_factory,
    agent_factory=agent_factory,
    settings=settings,
)

if __name__ == "__main__":
    missing = settings.missing()
    if missing:
        print("  Not yet configured — set these in .env before going live:")
        for name in missing:
            print(f"    {name}")
        print("  Starting anyway; /health will report the same.\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
