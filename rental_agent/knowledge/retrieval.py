"""Searching the operator's own policy documents.

`rules.json` holds every fact the engine computes with: prices, deposits,
mileage, ceilings. It is small and exact on purpose.

A real rental company also has twenty pages of prose — what happens if you get a
fine, whether you may drive to Oman, who may be an additional driver, what to do
if the car is towed. None of that is a calculation, and none of it belongs in a
config file the engine reads. Without somewhere to put it, every such question
escalates to a human, and the owner answers "do you accept Egyptian licences?"
for the fiftieth time.

So: chunk the documents, index them, and give the agent a tool that returns the
relevant passages. The model then answers **from the operator's own words**
rather than from what it imagines a rental company's policy to be.

**Retrieval is lexical, not embeddings.** Three reasons, in order of weight:

1. A turn already costs two model calls against a 2–5 second target. Embedding
   the query adds a third round trip to answer a question about smoking policy.
2. It is deterministic. The same question retrieves the same passages every
   time, which means a bad answer can be traced to a passage rather than to a
   vector.
3. Customers asking policy questions use the domain's own words — "licence",
   "deposit", "fine", "Salik", "toll" — which is precisely the case where
   keyword scoring does well.

`Retriever` is an interface, so an embedding backend can be added later behind
it if recall ever proves insufficient. The tool would not change.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

#: Where the operator's documents live. Markdown, one file per document, split
#: on headings.
def _docs_dir() -> Path:
    from ..config import config_dir

    return config_dir() / "policy"


DEFAULT_DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "policy"

#: Words too common to discriminate between passages.
_STOPWORDS = frozenset("""
a an the and or but if then than that this these those is are was were be been
being do does did doing have has had having i you he she it we they me him her
us them my your his its our their of in on at to from by for with without about
into over under again further once here there when where why how all any both
each few more most other some such no nor not only own same so too very can will
just should now what which who whom
""".split())

_WORD = re.compile(r"[a-z0-9']+")

#: Customer vocabulary mapped onto the document's, loaded from
#: `config/policy/synonyms.json`. Lexical search matches words, and "can I bring
#: my dog" shares none with a section about pets — so without this the question
#: retrieves nothing and escalates to a human for no reason.
def _synonyms_path() -> Path:
    return _docs_dir() / "synonyms.json"


@lru_cache(maxsize=1)
def _synonyms() -> dict[str, str]:
    """variant -> canonical, flattened from the config's canonical -> variants."""
    path = _synonyms_path()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    flat: dict[str, str] = {}
    for canonical, variants in raw.items():
        if canonical.startswith("_"):
            continue
        for variant in variants:
            for word in variant.split():
                flat[word] = canonical
    return flat

#: A figure in a policy document is a fact the engine should own. Indexing one
#: would let the model quote a price that no pricing tool produced, through a
#: tool result the evaluator counts as evidence — which is the exact hole this
#: whole system is built to not have.
_MONEY = re.compile(r"(?:aed|usd|eur|\$|£|€)\s*\d|(?<![\w.])\d[\d,]*\s*(?:aed|dirhams?)", re.I)
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s*%")


class PolicyContainsFigures(ValueError):
    """Raised when a document tries to state a number the engine should own."""


def tokenise(text: str) -> list[str]:
    """Words worth matching on, with customer vocabulary folded onto the
    document's. Both the query and the documents go through this, so a section
    about pets is findable by someone asking about their dog."""
    aliases = _synonyms()
    words = []
    for word in _WORD.findall((text or "").lower()):
        if word in _STOPWORDS or len(word) < 2:
            continue
        words.append(word)
        canonical = aliases.get(word)
        if canonical and canonical != word:
            words.append(canonical)
    return words


@dataclass
class Passage:
    """One retrievable chunk, with enough provenance to cite it."""

    document: str
    section: str
    text: str
    tokens: list[str] = field(default_factory=list, repr=False)

    @property
    def reference(self) -> str:
        return f"{self.document} — {self.section}"


def _forbidden_figures(text: str) -> list[str]:
    return _MONEY.findall(text) + _PERCENT.findall(text)


def chunk_markdown(document: str, markdown: str) -> list[Passage]:
    """Split a document on its headings, one passage per section.

    Headings are kept in the searchable text: a section titled "Traffic fines
    and Salik tolls" is often the best match for the question, even when the
    body never repeats those words.
    """
    passages: list[Passage] = []
    section = "General"
    body: list[str] = []

    def flush() -> None:
        text = "\n".join(body).strip()
        if not text:
            return
        figures = _forbidden_figures(f"{section}\n{text}")
        if figures:
            raise PolicyContainsFigures(
                f"{document} — {section} states {figures[0]!r}. Amounts and "
                "percentages belong in rules.json where the engine owns them; a "
                "figure indexed here could be quoted to a customer without any "
                "pricing tool having produced it."
            )
        passages.append(
            Passage(
                document=document,
                section=section,
                text=text,
                tokens=tokenise(f"{section} {section} {text}"),  # heading weighted
            )
        )

    for line in markdown.splitlines():
        heading = re.match(r"^#{1,6}\s+(.*)", line)
        if heading:
            flush()
            section = heading.group(1).strip()
            body = []
        else:
            body.append(line)
    flush()
    return passages


class Retriever:
    """BM25 over the operator's documents.

    Rebuilt from disk rather than persisted: the whole corpus is a handful of
    pages, indexing takes milliseconds, and a stale index that disagrees with
    the document on disk would be a very confusing bug to chase.
    """

    #: Standard BM25 constants. k1 controls how fast term frequency saturates,
    #: b how much a long passage is penalised for its length.
    K1 = 1.5
    B = 0.75

    def __init__(self, passages: Iterable[Passage]):
        self.passages = list(passages)
        self._document_frequency: Counter[str] = Counter()
        for passage in self.passages:
            self._document_frequency.update(set(passage.tokens))
        lengths = [len(p.tokens) for p in self.passages] or [0]
        self._average_length = sum(lengths) / len(lengths) or 1.0

    @classmethod
    def from_directory(cls, directory: Path | None = None) -> "Retriever":
        directory = directory or _docs_dir()
        passages: list[Passage] = []
        if directory.exists():
            for path in sorted(directory.glob("*.md")):
                title = path.stem.replace("-", " ").replace("_", " ").title()
                passages.extend(chunk_markdown(title, path.read_text(encoding="utf-8")))
        return cls(passages)

    def _idf(self, term: str) -> float:
        total = len(self.passages)
        seen = self._document_frequency.get(term, 0)
        if not seen:
            return 0.0
        return math.log(1 + (total - seen + 0.5) / (seen + 0.5))

    def _score(self, passage: Passage, query: list[str]) -> float:
        if not passage.tokens:
            return 0.0
        counts = Counter(passage.tokens)
        length_ratio = len(passage.tokens) / (self._average_length or 1.0)
        score = 0.0
        for term in query:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            numerator = frequency * (self.K1 + 1)
            denominator = frequency + self.K1 * (1 - self.B + self.B * length_ratio)
            score += self._idf(term) * numerator / denominator
        return score

    def search(self, question: str, limit: int = 3) -> list[tuple[Passage, float]]:
        """The passages most likely to answer this, best first.

        Returns nothing rather than the least-bad passage when no term matches.
        A confident answer assembled from an irrelevant paragraph is worse than
        admitting the document does not cover it, because the second outcome
        escalates to someone who knows.
        """
        query = tokenise(question)
        if not query:
            return []
        scored = [(p, self._score(p, query)) for p in self.passages]
        hits = [(p, s) for p, s in scored if s > 0]
        hits.sort(key=lambda pair: pair[1], reverse=True)
        return hits[:limit]


@lru_cache(maxsize=1)
def load_retriever() -> Retriever:
    """The index, built once per process. Call `reload()` after editing a
    document, the same way the rules and fleet caches work."""
    return Retriever.from_directory()


def reload() -> None:
    load_retriever.cache_clear()
    _synonyms.cache_clear()
