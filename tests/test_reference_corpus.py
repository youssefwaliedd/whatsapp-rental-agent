"""The operator's real conversations, with the people taken out.

Thirteen chats between Delta's sales team and their customers. Kept because they
show how this company sells and what it really charges — and de-identified
because they are somebody else's personal data, and a repository is forever.

These tests are the guard on that. They fail if a re-import puts a phone number,
an email address or a real name back, which is the way this would go wrong: not
by anyone deciding to commit personal data, but by someone re-running the
importer after a change and not looking closely at the diff.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CORPUS = Path(__file__).parents[1] / "reference" / "conversations"

#: Names that appear in the raw export. If any resurfaces, the mapping broke.
REAL_NAMES = [
    "Vandana", "Rajput", "Deena", "Nikol", "Romain", "Cavillot", "Joshua",
    "Canellis", "Souleimane", "Aslam", "Peerzada", "Bukhari", "Sleegers",
    "Verissimo", "Yanakiev", "Mebrahtu", "Depaul",
]

EMAIL = re.compile(r"[\w.%-]+@[\w.-]+\.[A-Za-z]{2,}")
#: A run of digits long enough to be a number somebody could be reached on.
PHONE = re.compile(r"\+\d[\d\s().-]{7,}\d|\b0\d{8,}\b")


@pytest.fixture(scope="module")
def transcripts() -> list[tuple[str, str]]:
    files = sorted(CORPUS.glob("chat-*.txt"))
    assert files, f"no reference conversations found in {CORPUS}"
    return [(p.name, p.read_text(encoding="utf-8")) for p in files]


def test_the_corpus_is_there(transcripts):
    assert len(transcripts) >= 12


@pytest.mark.parametrize("name", REAL_NAMES)
def test_no_real_name_survives(name, transcripts):
    guilty = [f for f, text in transcripts if re.search(rf"\b{name}\b", text, re.I)]
    assert not guilty, f"{name!r} is still in {guilty}"


def test_no_phone_numbers(transcripts):
    guilty = {f: PHONE.findall(text)[:3] for f, text in transcripts if PHONE.search(text)}
    assert not guilty, f"contact numbers survived: {guilty}"


def test_no_email_addresses(transcripts):
    guilty = {f: EMAIL.findall(text)[:3] for f, text in transcripts if EMAIL.search(text)}
    assert not guilty, f"email addresses survived: {guilty}"


def test_everyone_is_a_role(transcripts):
    speakers = set()
    for _, text in transcripts:
        # The bracket must hold a timestamp: a line can also open with a
        # "[photo]" placeholder followed by one, which is not a speaker.
        speakers |= set(re.findall(r"^\[\d[^\]]*\]\s+([^:]{1,20}):", text, re.M))
    assert speakers <= {"Customer", "Agent", "Agent 2", "Agent 3", "Agent 4", "Bot"}, speakers


def test_the_figures_are_left_alone(transcripts):
    """De-identifying must not scrub what the corpus exists for."""
    joined = "\n".join(text for _, text in transcripts)
    for figure in ("5,000", "500 AED", "250 KMs", "3,500"):
        assert figure in joined, f"{figure!r} was lost in de-identification"
