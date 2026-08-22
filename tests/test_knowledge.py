"""Retrieval over the operator's policy documents.

The long tail — fines, cross-border travel, licences by nationality — is prose,
not calculation, so it lives in documents rather than in `rules.json`. Without
somewhere to put it every such question escalates, and the owner answers "do you
accept Egyptian licences?" for the fiftieth time.

The property that matters most here is the one that keeps the fact boundary
intact: **a policy document may not state a figure.** If it could, a price could
reach a customer through a tool result the evaluator counts as evidence, without
any pricing tool having produced it — which is the exact hole this system exists
to not have.
"""

from __future__ import annotations

import pytest

from rental_agent.knowledge.retrieval import (
    PolicyContainsFigures,
    Retriever,
    chunk_markdown,
    load_retriever,
    tokenise,
)
from rental_agent.tools.registry import execute_tool


@pytest.fixture
def retriever():
    return load_retriever()


# --------------------------------------------------------------------------
# The fact boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "offending",
    [
        "# Deposit\nWe hold AED 5,000 against the card.",
        "# Fuel\nRefuelling costs 150 AED plus the fuel.",
        "# Late return\nA 10% surcharge applies after the grace period.",
        "# Cleaning\nDeep cleaning is $200.",
    ],
)
def test_a_policy_document_may_not_state_a_figure(offending):
    """Amounts belong in rules.json where the engine owns them. Indexed here,
    the model could quote one to a customer and the evaluator would count the
    retrieval as evidence for it."""
    with pytest.raises(PolicyContainsFigures):
        chunk_markdown("Test", offending)


def test_the_shipped_policy_document_states_no_figures(retriever):
    """The guard is only worth having if the corpus actually passes it."""
    assert retriever.passages, "the demo policy document should be indexed"


def test_prose_about_money_without_a_figure_is_fine():
    """Refusing the word "deposit" would make the document unwritable. It is
    the numbers that are forbidden, not the subject."""
    passages = chunk_markdown(
        "Test", "# Deposits\nThe deposit is held on the driver's own credit card."
    )
    assert len(passages) == 1


# --------------------------------------------------------------------------
# Finding the right passage
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question,expected_section",
    [
        ("do you accept egyptian driving licences", "licence"),
        ("can i drive to oman", "Cross-border"),
        ("my car got towed", "towing"),
        ("can i smoke in the car", "Smoking"),
        ("is beach driving allowed", "may be driven"),
        ("what if i get a speeding fine", "fines"),
        ("can my wife drive it too", "Additional"),
        ("do you have baby seats", "Child"),
        ("the car broke down", "Breakdown"),
        ("when do i get my deposit back", "Deposits"),
        ("i lost the key", "Returning"),
        ("can i bring my dog", "pets"),
        ("can i take it in the desert", "may be driven"),
        ("do i pay the salik gates", "Salik"),
    ],
)
def test_a_customers_question_finds_the_section_that_answers_it(
    retriever, question, expected_section
):
    sections = [p.section for p, _ in retriever.search(question, limit=3)]
    assert any(expected_section.lower() in s.lower() for s in sections), sections


def test_customer_words_are_mapped_onto_the_documents_words():
    """"Can I bring my dog" shares no word with a section about pets. Without
    the synonym layer that question retrieves nothing and escalates for no
    reason."""
    assert "pet" in tokenise("can i bring my dog")
    assert "breakdown" in tokenise("the car broke down")
    assert "accident" in tokenise("i crashed it")


def test_an_unrelated_question_retrieves_nothing_rather_than_the_least_bad_match():
    """A confident answer assembled from an irrelevant paragraph is worse than
    admitting the document does not cover it — the second escalates to someone
    who knows."""
    retriever = Retriever(chunk_markdown("Test", "# Fuel\nReturn the car full."))
    assert retriever.search("what is the capital of France") == []


def test_the_heading_counts_towards_the_match(retriever):
    """A section titled "Traffic fines and Salik tolls" is often the best answer
    to a question whose words never appear in the body."""
    top = retriever.search("salik", limit=1)
    assert top and "Salik" in top[0][0].section


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------


def test_the_tool_returns_passages_with_their_source(ctx):
    result = execute_tool(
        ctx, "search_company_policy", {"question": "can i drive to oman"}
    )
    assert result["found"] >= 1
    first = result["passages"][0]
    assert "Rental Terms" in first["source"], "the answer must be citable"
    assert "Oman" in first["text"]


def test_the_tool_tells_the_agent_to_escalate_when_nothing_matches(ctx):
    result = execute_tool(
        ctx, "search_company_policy", {"question": "zzzz qqqq xxxx"}
    )
    assert result["found"] == 0
    assert "outside_knowledge_base" in result["hint"]
    assert "general knowledge" in result["hint"]


def test_the_tool_reminds_the_agent_that_figures_come_from_elsewhere(ctx):
    result = execute_tool(ctx, "search_company_policy", {"question": "deposit"})
    assert "pricing tool" in result["note"]


def test_an_empty_question_is_reported_not_raised(ctx):
    assert execute_tool(ctx, "search_company_policy", {"question": "  "})["error"]


def test_the_tool_description_sends_the_agent_here_before_escalating():
    """The point of the tool is to stop the owner answering the same question
    fifty times, which only happens if the agent reaches for it first."""
    from rental_agent.agent.schemas import TOOLS

    [schema] = [t for t in TOOLS if t["name"] == "search_company_policy"]
    assert "before escalating" in schema["description"].lower()
