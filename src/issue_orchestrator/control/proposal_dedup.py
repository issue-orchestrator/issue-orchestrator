"""Lexical near-duplicate scoring for tech-lead ``create_issue`` proposals.

The tech lead must not keep re-filing the same follow-up. The SEMANTIC judgment
of "is this proposal the same as an existing open issue?" belongs to the agent
(it sets ``duplicate_of`` on the proposal, see
:class:`~issue_orchestrator.domain.tech_lead_artifacts.ProposedTechLeadAction`).
This module is the ORCHESTRATOR's deterministic cross-check: a cheap, offline,
dependency-free lexical similarity that ranks a proposal against a corpus of open
issues so the orchestrator never has to *trust* the agent's dedup alone
(orchestrator-authoritative — the agent carries intent, the orchestrator
decides).

It is deliberately LEXICAL, not neural: at the tech lead's proposal cadence and a
backlog of hundreds-to-low-thousands of open issues, token-overlap cosine is
microseconds, deterministic (identical input → identical score, so it is trivially
testable), needs no model/vendor/key, and sends nothing anywhere. It catches
re-worded-but-same-vocabulary duplicates — the common accidental re-file — and is
honestly weak on pure paraphrase, which is exactly why the AGENT (which reads the
board) owns the semantic call and this only backstops it. A future increment may
swap a semantic scorer behind the same :func:`find_duplicate` seam without
touching callers (#6878).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

# Markdown/template scaffolding a tech-lead body almost always carries ("##
# Problem", "## Summary", "Acceptance criteria", checkbox bullets). Stripped
# before scoring so two UNRELATED proposals that share the boilerplate do not
# read as similar — the signal is the substantive words, not the template.
_BOILERPLATE_TOKENS = frozenset(
    {
        "problem",
        "summary",
        "context",
        "scope",
        "acceptance",
        "criteria",
        "related",
        "background",
        "proposed",
        "approach",
        "note",
        "notes",
        "issue",
        "pr",
        "tech",
        "lead",
    }
)

# Function words carry no topical signal; dropping them keeps cosine focused on
# the substantive terms and stops long boilerplate-heavy bodies from dominating.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "if", "then", "else", "for", "of",
        "to", "in", "on", "at", "by", "with", "as", "is", "are", "was", "were",
        "be", "been", "being", "it", "its", "this", "that", "these", "those",
        "we", "our", "you", "your", "they", "their", "not", "no", "do", "does",
        "so", "from", "into", "when", "which", "should", "would", "could", "can",
        "will", "has", "have", "had", "than", "there", "here",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# The title names the thing; the body elaborates. A duplicate is best signalled
# by a matching title, so title tokens are weighted up relative to body tokens.
_TITLE_WEIGHT = 3


@dataclass(frozen=True)
class OpenIssueRef:
    """An open issue a proposal is scored against — the dedup corpus element."""

    number: int
    title: str
    body: str = ""


@dataclass(frozen=True)
class DuplicateMatch:
    """The best-scoring corpus issue at or above the similarity threshold."""

    number: int
    score: float


def _tokens(text: str) -> list[str]:
    """Substantive lowercase word tokens: markdown/punct dropped, stop + template
    words removed. Empty input (or all-boilerplate) yields ``[]``."""
    return [
        tok
        for tok in _TOKEN_RE.findall(text.lower())
        if tok not in _STOPWORDS and tok not in _BOILERPLATE_TOKENS
    ]


def _weighted_terms(title: str, body: str) -> Counter[str]:
    """Term-frequency vector for a (title, body), title tokens weighted up."""
    terms: Counter[str] = Counter()
    for tok in _tokens(title):
        terms[tok] += _TITLE_WEIGHT
    for tok in _tokens(body):
        terms[tok] += 1
    return terms


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    """Cosine similarity of two term-frequency vectors in ``[0.0, 1.0]``.

    ``0.0`` when either side is empty (no substantive terms → not comparable,
    never a spurious match).
    """
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in shared)
    if dot == 0:
        return 0.0
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    # Clamp: float sqrt rounding can push an identical-vector cosine to
    # 1.0000000002, which breaks callers/tests that expect a bounded [0, 1].
    return min(1.0, dot / (norm_a * norm_b))


def similarity(title: str, body: str, other_title: str, other_body: str) -> float:
    """Lexical similarity of two (title, body) issues in ``[0.0, 1.0]``.

    Deterministic and symmetric; title tokens weighted over body tokens.
    """
    return _cosine(
        _weighted_terms(title, body), _weighted_terms(other_title, other_body)
    )


def find_duplicate(
    title: str,
    body: str,
    corpus: Sequence[OpenIssueRef],
    *,
    threshold: float,
) -> DuplicateMatch | None:
    """Best corpus issue whose similarity to ``(title, body)`` is ``>= threshold``.

    Returns ``None`` when nothing clears the bar. Ties break toward the LOWEST
    issue number (deterministic, and the older issue is the canonical original).
    Brute force over the corpus — O(n) small-vector cosines, cheap at the tech
    lead's proposal cadence.
    """
    best: DuplicateMatch | None = None
    proposal = _weighted_terms(title, body)
    for ref in corpus:
        score = _cosine(proposal, _weighted_terms(ref.title, ref.body))
        # A zero score means no shared substantive terms — never a duplicate,
        # even when the caller passes threshold=0.0.
        if score <= 0.0 or score < threshold:
            continue
        if best is None or score > best.score or (
            score == best.score and ref.number < best.number
        ):
            best = DuplicateMatch(number=ref.number, score=score)
    return best
