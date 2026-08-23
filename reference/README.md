# Reference conversations

Thirteen real conversations from Delta Rentals, sent as their "10 Star" chats —
the ones they consider their best. Roughly 2,200 messages between customers and
their sales team, from May to July 2026.

They are the most useful thing the operator has handed over. Two reasons:

**They show how this company actually sells.** The greeting, what is asked
first, the pause while availability is checked by hand, the photographs, the
upsell to the no-deposit option, and the way every discount is framed as
management approval for one booking rather than as a rate.

**They quote figures the website has never published.** The no-deposit service
fee, the deposit that is actually taken, the real per-kilometre charges, the
insurance excess their team states — several of which contradict the published
terms. `docs/policy-questionnaire.md` puts each of those to the operator for
confirmation rather than encoding it from practice, because what one salesperson
quoted once is evidence, not policy.

## De-identified on the way in

The originals are somebody else's personal data: real names, forty-six phone
numbers, flight details, and passports and licences discussed by file name. None
of that is needed for either purpose above, so none of it is here.

`rental_agent/sources/chats.py` does the conversion — names become roles
(`Customer`, `Agent`, `Agent 2`, `Bot`), contact details and links become
placeholders, and attachments become `[photo]` or `[document]`. Timestamps stay,
because pace is part of what these show: how fast the first reply comes, and how
long someone waits while a person checks a spreadsheet.

**Every figure is kept exactly as written.** The figures are the point.

    .venv/bin/python -m rental_agent.sources.chats ~/Downloads/"Top 10 chat for AI"

`tests/test_reference_corpus.py` fails if a phone number, an email address or a
known name reappears, so a careless re-import cannot quietly put one back.

## What they are for

- **The playbook** (`config/playbook.md`) is written from them: sequence, tone
  and the objection handling, with no figures in it.
- **Measuring a prompt change.** These are the benchmark — given the same
  customer turns, what does the agent say, and is the difference better or worse?
  That is worth more than the written script, because it is what actually
  converted.

The originals stay out of the repository. They are in the operator's own export
and in `~/Downloads`, and they should not be committed anywhere.
