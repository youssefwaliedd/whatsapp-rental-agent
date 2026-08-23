"""Turning the operator's real conversations into a reference corpus.

Delta sent twelve of their best conversations — the "10 Star" chats — as WhatsApp
exports. They are the most useful thing anyone has handed over: they show how the
company actually sells, and they quote figures their website has never published.

They are also somebody else's personal data. Real names, forty-six phone numbers,
flight details, photographs of passports and licences discussed by file name.
None of that belongs in a git repository, and none of it is needed for the two
things the corpus is for — how these people sell, and what they charge.

So the export is de-identified on the way in. Names become roles, contact details
become placeholders, and attachments become a note of what was sent. **Every
figure is kept exactly as written**, because the figures are the point.

    .venv/bin/python -m rental_agent.sources.chats ~/Downloads/"Top 10 chat for AI"
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

#: `[07/15/2026 13:29:16] Speaker: text`
LINE = re.compile(r"^\[(?P<when>[^\]]+)\]\s+(?P<who>[^:]{1,60}?):\s?(?P<text>.*)$")

PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
EMAIL = re.compile(r"[\w.%-]+@[\w.-]+\.[A-Za-z]{2,}")
#: The exports name attachments by uuid; the file itself was never sent to us.
ATTACHMENT = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(\w+)\b")
URL = re.compile(r"https?://\S+")

#: Their automated greeter, which is a role rather than a person.
BOT = "Bot"


def customer_name(path: Path) -> str:
    """The customer is named in the file name, before the "ll 10 Star" marker.

    The separator must be surrounded by whitespace. Splitting on a bare "ll"
    turns Cavillot into "Cavi", which then matches nobody and quietly relabels
    the customer as a member of staff.
    """
    stem = unicodedata.normalize("NFC", path.stem)
    return re.split(r"\s+ll\s+", stem)[0].strip()


def speakers(text: str) -> list[str]:
    """Everyone who said something, in the order they first spoke."""
    seen: list[str] = []
    for line in text.splitlines():
        match = LINE.match(line)
        if match:
            who = match.group("who").strip()
            if who not in seen:
                seen.append(who)
    return seen


def name_map(path: Path, text: str) -> dict[str, str]:
    """Who becomes what. The customer is the one the file is named after;
    everyone else is staff, numbered in the order they appear so a conversation
    handed between two colleagues still reads as two people."""
    customer = customer_name(path)
    mapping: dict[str, str] = {}
    staff = 0
    for who in speakers(text):
        if who == BOT:
            mapping[who] = "Bot"
        elif who.lower() == customer.lower() or _same_person(who, customer):
            mapping[who] = "Customer"
        else:
            staff += 1
            mapping[who] = "Agent" if staff == 1 else f"Agent {staff}"
    return mapping


def _same_person(a: str, b: str) -> bool:
    """Export names drift — a middle name here, a double space there."""
    def parts(name: str) -> set[str]:
        return {p for p in re.split(r"\W+", name.lower()) if len(p) > 2}

    left, right = parts(a), parts(b)
    return bool(left and right and len(left & right) >= min(2, len(right)))


def scrub(text: str, names: dict[str, str]) -> str:
    """Contact details, attachments and links out; every figure left alone."""
    for real, role in names.items():
        if role == "Bot":
            continue
        for token in sorted(re.split(r"\W+", real), key=len, reverse=True):
            if len(token) > 2:
                text = re.sub(rf"\b{re.escape(token)}\b", role, text, flags=re.I)
    # "Romain Cavillot" becomes "Customer Customer" once both halves are
    # replaced; a customer giving their full name should read as one person.
    for role in set(names.values()):
        text = re.sub(rf"\b{re.escape(role)}(?:\s+{re.escape(role)})+\b", role, text)
    text = ATTACHMENT.sub(lambda m: f"[{_kind(m.group(1))}]", text)
    text = EMAIL.sub("[email]", text)
    text = URL.sub("[link]", text)
    text = PHONE.sub("[phone]", text)
    return text


def _kind(extension: str) -> str:
    ext = extension.lower()
    if ext in {"png", "jpg", "jpeg", "webp", "heic"}:
        return "photo"
    if ext in {"mp4", "mov"}:
        return "video"
    return "document"


def convert(path: Path) -> str:
    """One export, de-identified, with the timestamps kept.

    Timestamps stay because pace is part of what these show: how fast the first
    reply comes, how long a customer is left waiting while availability is
    checked by hand.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    names = name_map(path, raw)
    out: list[str] = []
    for line in raw.splitlines():
        match = LINE.match(line)
        if match:
            who = names.get(match.group("who").strip(), "Agent")
            out.append(f"[{match.group('when')}] {who}: {scrub(match.group('text'), names)}")
        else:
            out.append(scrub(line, names))
    return "\n".join(out).rstrip() + "\n"


def main(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in source.glob("*.txt"))
    for index, path in enumerate(files, start=1):
        cleaned = convert(path)
        out = target / f"chat-{index:02d}.txt"
        out.write_text(cleaned, encoding="utf-8")
        print(f"  {path.name[:44]:46} -> {out.name}  ({len(cleaned.splitlines())} lines)")
    print(f"\n{len(files)} conversations de-identified into {target}")


if __name__ == "__main__":
    source = Path(sys.argv[1]).expanduser()
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("reference/conversations")
    main(source, target)
