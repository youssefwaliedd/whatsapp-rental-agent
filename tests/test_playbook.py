"""The operator's sales playbook, loaded into the prompt.

Their brief promised a playbook and sample conversations. The conversations came
first, so the playbook is written from them: how they open, what they ask before
anything else, how they frame a discount, how they sell the no-deposit option.

It is held to the same rule as the policy documents — **it may not state a
figure**. The difference is that the playbook is read on *every* turn rather than
retrieved when relevant, which makes a number in it the most reliable way there
is to teach the agent to quote a price no tool produced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rental_agent import config as config_mod
from rental_agent.agent.prompt import build_system
from rental_agent.config import load_fleet, load_playbook, load_rules
from rental_agent.knowledge.retrieval import PolicyContainsFigures


@pytest.fixture
def playbook_dir(tmp_path, monkeypatch):
    """A complete config of its own — fleet and rules included, because building
    the system prompt reads them too."""
    import shutil
    source = Path(__file__).parent / "fixtures" / "config"
    for name in ("fleet.json", "rules.json"):
        shutil.copy(source / name, tmp_path / name)
    monkeypatch.setenv("RENTAL_AGENT_CONFIG_DIR", str(tmp_path))
    config_mod.reload()
    yield tmp_path
    config_mod.reload()


def test_the_real_playbook_states_no_figure():
    live = Path(__file__).parents[1] / "config" / "playbook.md"
    if not live.exists():
        pytest.skip("no playbook in the live config")
    from rental_agent.knowledge.retrieval import _forbidden_figures
    assert not _forbidden_figures(live.read_text(encoding="utf-8"))


def test_a_playbook_that_states_a_price_is_refused(playbook_dir):
    (playbook_dir / "playbook.md").write_text(
        "# How we sell\n\nAlways open by offering the Urus at AED 3,199 per day.\n"
    )
    with pytest.raises(PolicyContainsFigures, match="rules.json"):
        load_playbook()


def test_a_playbook_that_states_a_percentage_is_refused(playbook_dir):
    (playbook_dir / "playbook.md").write_text("# How we sell\n\nOffer 10% off if they hesitate.\n")
    with pytest.raises(PolicyContainsFigures):
        load_playbook()


def test_no_playbook_is_not_an_error(playbook_dir):
    assert load_playbook() == ""


def test_the_agent_is_given_the_playbook_every_turn(playbook_dir):
    (playbook_dir / "playbook.md").write_text(
        "# How we sell\n\nAsk what they are celebrating before you offer a car.\n"
    )
    config_mod.reload()
    operator, _ = load_fleet()
    text = build_system(load_rules(), operator)[0]["text"]
    assert "How this company sells" in text
    assert "before you offer a car" in text


def test_deltas_playbook_carries_their_own_qualifying_question():
    # The line that appears in chat after chat, and the reason the playbook is
    # written from the conversations rather than invented.
    live = Path(__file__).parents[1] / "config" / "playbook.md"
    if not live.exists():
        pytest.skip("no playbook in the live config")
    assert "what would be your budget" in live.read_text(encoding="utf-8")


def test_replacing_the_playbook_moves_the_cached_prefix(playbook_dir):
    operator, _ = load_fleet()
    rules = load_rules()
    (playbook_dir / "playbook.md").write_text("# How we sell\n\nLead with the convertibles.\n")
    config_mod.reload()
    first = build_system(rules, operator)[0]["text"]

    (playbook_dir / "playbook.md").write_text("# How we sell\n\nLead with the SUVs.\n")
    config_mod.reload()
    second = build_system(rules, operator)[0]["text"]

    # Cached by content, not by operator alone: an edited playbook must not be
    # silently ignored until the next restart.
    assert "convertibles" in first and "SUVs" in second
