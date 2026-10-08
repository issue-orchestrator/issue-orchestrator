"""Standing rulings: a maintainer's binding decision about how an issue is built (#8141).

A ruling is not prose in a comment. porchpin#364 / PR #379 got its maintainer
ruling four ways (an issue comment, a PR comment, pasted at the top of the
issue body, an approved tech-lead decision) and a conflict rework still
extended the retired design while the reviewer approved it, because no agent
prompt and no review check carried the ruling. porchpin#327's approved
``resolve_block`` answer never reached the issue body at all.

This module is the typed vocabulary every reader and writer shares:

* :class:`StandingRuling` - the ruling text, its :class:`RulingAuthority` (a
  maintainer, or an approved tech-lead decision or block resolution), the
  source it came from, and its :class:`RulingScope`: the files it governs and
  the claims it settles. A ruling with no files governs the whole issue.
* **The issue-body block** (:func:`with_rulings_block` /
  :func:`parse_rulings_block`) - the durable, crash-safe record on GitHub.
  It sits at the top of the issue body because agents read the body; each
  ruling's metadata is one marker line and its text sits verbatim between two
  markers, so the block round-trips exactly and the text is readable as is.
* **The prompt section** (:func:`rulings_prompt`) - what every agent session
  on the issue is told, framed for its role (:func:`audience_for`): a coder
  builds to the rulings, a rework of a PR that does not implement them yet
  carries them as its BRIEF, a reviewer checks the diff against each and must
  attest each one it upholds, a tech lead never acts against one.
* **The review rule** (:func:`unattested_rulings`) - an approval of a diff
  that touches a ruling's scope must attest that ruling; one that does not is
  refused and turned into implementation-required feedback
  (:func:`refused_approval_feedback`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any, cast

from .launch_prompt import RULINGS_SEPARATOR
from .session_kind import SandboxRole, SessionKind

#: The block that carries an issue's standing rulings at the top of its body.
RULINGS_BLOCK_BEGIN = "<!-- io:standing-rulings:begin -->"
RULINGS_BLOCK_END = "<!-- io:standing-rulings:end -->"
_META_PREFIX = "<!-- io:standing-ruling:meta:"
_META_SUFFIX = " -->"
_TEXT_BEGIN = "<!-- io:standing-ruling:text:begin -->"
_TEXT_END = "<!-- io:standing-ruling:text:end -->"
_RULING_HEADING = "### Ruling `"
#: Every marker this module writes starts with this; ruling text may not
#: contain it, so no ruling can forge or truncate another.
_MARKER_STEM = "<!-- io:standing-ruling"
#: On the review comment of an approval the rulings refused.
REFUSED_APPROVAL_MARKER = "<!-- io:standing-ruling:refused-approval -->"

#: GitHub's limit on an issue body, in characters.
GITHUB_BODY_MAX_CHARS = 65_536
MAX_RULING_TEXT_CHARS = 30_000
MAX_RULING_SOURCE_CHARS = 300
MAX_SCOPE_FILES = 50
MAX_SCOPE_CLAIMS = 20
MAX_SCOPE_ENTRY_CHARS = 300
#: Attestations a review may carry (a bound on untrusted input).
MAX_UPHELD_RULINGS = 50

_RULING_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_SUMMARY_CHARS = 160


class RulingsBlockError(ValueError):
    """The issue body's rulings block is malformed, or would not fit."""


class RulingAuthority(StrEnum):
    """Who ruled. Every authority binds the same way; it is shown, not weighed."""

    #: A maintainer recorded it directly (the operator's ruling command).
    MAINTAINER = "maintainer"
    #: A tech lead's ``propose_decision`` a maintainer approved.
    APPROVED_DECISION = "approved_decision"
    #: A tech lead's ``resolve_block`` answer, approved by a maintainer or by
    #: the operator's charter (``resolve_block: execute``).
    APPROVED_RESOLUTION = "approved_resolution"

    @property
    def described(self) -> str:
        return _AUTHORITY_WORDS[self]


_AUTHORITY_WORDS: Mapping[RulingAuthority, str] = {
    RulingAuthority.MAINTAINER: "maintainer ruling",
    RulingAuthority.APPROVED_DECISION: "approved tech-lead decision",
    RulingAuthority.APPROVED_RESOLUTION: "approved tech-lead block resolution",
}


def _bounded_entries(values: object, *, what: str, limit: int) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > limit:
        raise ValueError(f"a ruling's {what} are a tuple of at most {limit} entries")
    for value in values:
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or len(value) > MAX_SCOPE_ENTRY_CHARS
            or "\n" in value
            or _MARKER_STEM in value
        ):
            raise ValueError(f"a ruling's {what} entry must be one trimmed line, got {value!r}")
    if len(set(values)) != len(values):
        raise ValueError(f"a ruling's {what} repeat an entry")
    return values


@dataclass(frozen=True, slots=True)
class RulingScope:
    """What a ruling governs: repository paths (globs) and the claims it settles.

    ``files`` empty means the ruling governs the whole issue, so it covers
    every diff. A pattern without a glob character also covers everything
    under it as a directory (``tools/walk`` covers ``tools/walk/check.py``).
    """

    files: tuple[str, ...] = ()
    claims: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _bounded_entries(self.files, what="files", limit=MAX_SCOPE_FILES)
        _bounded_entries(self.claims, what="claims", limit=MAX_SCOPE_CLAIMS)
        for pattern in self.files:
            if pattern.startswith("/") or ".." in pattern.split("/") or "\\" in pattern:
                raise ValueError(f"a ruling's file pattern is repository-relative, got {pattern!r}")

    def covers(self, paths: Iterable[str]) -> bool:
        """Whether a diff touching *paths* is one this ruling governs."""
        if not self.files:
            return True
        return any(_matches(pattern, path) for pattern in self.files for path in paths)


def _matches(pattern: str, path: str) -> bool:
    if any(char in pattern for char in "*?["):
        return fnmatchcase(path, pattern)
    directory = pattern.rstrip("/")
    return path == directory or path.startswith(f"{directory}/")


@dataclass(frozen=True, slots=True)
class StandingRuling:
    """One binding ruling on an issue (module docstring)."""

    ruling_id: str
    text: str
    authority: RulingAuthority
    #: Where it came from, in words a person can follow ("proposal #501").
    source: str
    scope: RulingScope
    #: ISO-8601 instant it was recorded.
    recorded_at: str

    def __post_init__(self) -> None:
        ruling_id = cast(object, self.ruling_id)
        if not isinstance(ruling_id, str) or not _RULING_ID.match(ruling_id):
            raise ValueError(f"a ruling id is 3-64 of [a-z0-9-], got {self.ruling_id!r}")
        if not isinstance(cast(object, self.authority), RulingAuthority):
            raise ValueError("a ruling's authority must be a RulingAuthority")
        if not isinstance(cast(object, self.scope), RulingScope):
            raise ValueError("a ruling's scope must be a RulingScope")
        text = cast(object, self.text)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_RULING_TEXT_CHARS:
            raise ValueError(f"a ruling's text is non-empty and at most {MAX_RULING_TEXT_CHARS} characters")
        if text != text.strip() or "\r" in text:
            raise ValueError("a ruling's text is trimmed and uses \\n line endings")
        if _MARKER_STEM in text:
            raise ValueError("a ruling's text may not contain an io standing-ruling marker")
        source = cast(object, self.source)
        if (
            not isinstance(source, str) or not source.strip() or source != source.strip()
            or len(source) > MAX_RULING_SOURCE_CHARS or "\n" in source or _MARKER_STEM in source
        ):
            raise ValueError("a ruling's source is one trimmed line")
        try:
            datetime.fromisoformat(self.recorded_at)
        except (TypeError, ValueError):
            raise ValueError(f"a ruling's recorded_at must be ISO-8601, got {self.recorded_at!r}") from None

    @property
    def summary(self) -> str:
        """Its first line, bounded: what a page row or a log line shows."""
        first = next(line.strip() for line in self.text.splitlines() if line.strip())
        first = first.lstrip("#>*- ").strip() or first
        return first if len(first) <= _SUMMARY_CHARS else f"{first[: _SUMMARY_CHARS - 1]}…"

    def meta(self) -> dict[str, Any]:
        """Everything but the text, as the block's metadata marker carries it."""
        return {
            "id": self.ruling_id,
            "authority": self.authority.value,
            "source": self.source,
            "files": list(self.scope.files),
            "claims": list(self.scope.claims),
            "recorded_at": self.recorded_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.meta(), "text": self.text}

    @classmethod
    def from_dict(cls, data: Any) -> "StandingRuling":
        """Parse a stored or transported ruling; any violation raises ValueError."""
        if not isinstance(data, Mapping):
            raise ValueError("a ruling must be an object")
        files, claims = data.get("files"), data.get("claims")
        if not isinstance(files, list) or not isinstance(claims, list):
            raise ValueError("a ruling's files and claims must be lists")
        try:
            authority = RulingAuthority(data.get("authority"))
        except ValueError:
            raise ValueError(f"unknown ruling authority {data.get('authority')!r}") from None
        return cls(
            ruling_id=_str_field(data, "id"),
            text=_str_field(data, "text"),
            authority=authority,
            source=_str_field(data, "source"),
            scope=RulingScope(files=tuple(files), claims=tuple(claims)),
            recorded_at=_str_field(data, "recorded_at"),
        )


def _str_field(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"a ruling's {key} must be a string, got {type(value).__name__}")
    return value


# -- ids ------------------------------------------------------------------------


def decision_ruling_id(proposal_issue_number: int) -> str:
    """The ruling an approved ``propose_decision`` records: one per proposal."""
    if proposal_issue_number < 1:
        raise ValueError("a decision ruling names its proposal issue")
    return f"pd-{proposal_issue_number}"


def resolution_ruling_id(decision_id: str) -> str:
    """The ruling an approved ``resolve_block`` answer records: one per decision."""
    if not decision_id:
        raise ValueError("a resolution ruling names its decision")
    return f"rb-{hashlib.sha256(decision_id.encode('utf-8')).hexdigest()[:12]}"


def maintainer_ruling_id(token: str) -> str:
    """A maintainer ruling's id from a caller-supplied random token (hex)."""
    if not re.fullmatch(r"[0-9a-f]{8,32}", token):
        raise ValueError("a maintainer ruling token is 8-32 lowercase hex characters")
    return f"m-{token}"


# -- the issue-body block -------------------------------------------------------


def _encode_meta(ruling: StandingRuling) -> str:
    raw = json.dumps(ruling.meta(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_meta(encoded: str) -> dict[str, Any]:
    try:
        raw = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError) as error:
        raise RulingsBlockError(f"a ruling's metadata marker does not decode: {error}") from None
    if not isinstance(data, dict):
        raise RulingsBlockError("a ruling's metadata marker is not an object")
    return data


def _render_ruling(ruling: StandingRuling) -> str:
    scope = ruling.scope
    governs = ", ".join(f"`{pattern}`" for pattern in scope.files) or "the whole issue"
    settles = "".join(f"\n- {claim}" for claim in scope.claims)
    return (
        f"{_RULING_HEADING}{ruling.ruling_id}`: {ruling.authority.described}"
        f" ({ruling.source}, {ruling.recorded_at[:10]})\n"
        f"{_META_PREFIX}{_encode_meta(ruling)}{_META_SUFFIX}\n"
        f"**Governs:** {governs}"
        + (f"\n\n**Settles:**{settles}" if settles else "")
        + f"\n\n{_TEXT_BEGIN}\n{ruling.text}\n{_TEXT_END}\n"
    )


def render_rulings_block(rulings: Iterable[StandingRuling]) -> str:
    """The block for *rulings* (at least one), in order."""
    items = tuple(rulings)
    if not items:
        raise ValueError("a rulings block carries at least one ruling")
    rendered = "\n".join(_render_ruling(ruling) for ruling in items)
    return (
        f"{RULINGS_BLOCK_BEGIN}\n"
        "> [!IMPORTANT]\n"
        "> **Standing rulings: binding on every agent and reviewer working this issue.**"
        " Where a ruling conflicts with the rest of this issue, the ruling wins."
        " issue-orchestrator manages this block; change a ruling through its ruling"
        " command, not by editing here.\n\n"
        f"{rendered}"
        f"{RULINGS_BLOCK_END}"
    )


def _split_block(body: str) -> tuple[str, str | None, str]:
    """(before, block contents or None, after), the body's line endings normalized."""
    text = body.replace("\r\n", "\n")
    begins, ends = text.count(RULINGS_BLOCK_BEGIN), text.count(RULINGS_BLOCK_END)
    if begins == 0 and ends == 0:
        if _MARKER_STEM in text:
            raise RulingsBlockError("the issue body carries standing-ruling markers outside a rulings block")
        return text, None, ""
    if begins != 1 or ends != 1:
        raise RulingsBlockError(
            f"the issue body carries {begins} rulings-block begin and {ends} end markers; exactly one of each"
        )
    head, _, rest = text.partition(RULINGS_BLOCK_BEGIN)
    inner, _, tail = rest.partition(RULINGS_BLOCK_END)
    if RULINGS_BLOCK_END in head:
        raise RulingsBlockError("the rulings block ends before it begins")
    if _MARKER_STEM in head or _MARKER_STEM in tail:
        raise RulingsBlockError("the issue body carries standing-ruling markers outside its rulings block")
    return head, inner, tail


def parse_rulings_block(body: str | None) -> tuple[StandingRuling, ...]:
    """The rulings an issue body carries, in order; () when it has no block.

    A malformed block raises :class:`RulingsBlockError` rather than guessing:
    a ruling silently dropped is exactly the failure this record exists to
    prevent.
    """
    _, inner, _ = _split_block(body or "")
    if inner is None:
        return ()
    rulings, remaining = _parse_rulings(inner)
    _require_intact(inner, remaining, rulings)
    return rulings


def _parse_rulings(inner: str) -> tuple[tuple[StandingRuling, ...], str]:
    """Each ruling in the block, in order, and what follows the last one."""
    rulings: list[StandingRuling] = []
    remaining = inner
    while _META_PREFIX in remaining:
        _, _, after_prefix = remaining.partition(_META_PREFIX)
        encoded, found, after_meta = after_prefix.partition(_META_SUFFIX)
        if not found:
            raise RulingsBlockError("a ruling's metadata marker is not closed")
        meta = _decode_meta(encoded.strip())
        before_text, found_begin, after_begin = after_meta.partition(_TEXT_BEGIN)
        if not found_begin or _META_PREFIX in before_text:
            raise RulingsBlockError(f"ruling {meta.get('id')!r} has no text")
        text, found_end, remaining = after_begin.partition(_TEXT_END)
        if not found_end or _META_PREFIX in text or _TEXT_BEGIN in text:
            raise RulingsBlockError(f"ruling {meta.get('id')!r}'s text is not closed")
        try:
            rulings.append(StandingRuling.from_dict({**meta, "text": text.strip()}))
        except ValueError as error:
            raise RulingsBlockError(f"ruling {meta.get('id')!r} is invalid: {error}") from None
    return tuple(rulings), remaining


def _require_intact(inner: str, remaining: str, rulings: tuple[StandingRuling, ...]) -> None:
    """Every marker, heading and id in the block belongs to exactly one parsed ruling."""
    if not rulings:
        raise RulingsBlockError("the rulings block carries no ruling (a block is removed with its last ruling)")
    for marker in (_META_PREFIX, _TEXT_BEGIN, _TEXT_END):
        if inner.count(marker) != len(rulings):
            raise RulingsBlockError(f"the rulings block carries a stray {marker!r} marker")
    if remaining.strip():
        raise RulingsBlockError("the rulings block carries content after its last ruling")
    outside_text = re.sub(re.escape(_TEXT_BEGIN) + r".*?" + re.escape(_TEXT_END), "", inner, flags=re.DOTALL)
    headings = outside_text.count(_RULING_HEADING)
    if headings != len(rulings):
        raise RulingsBlockError(f"the rulings block shows {headings} ruling headings for {len(rulings)} rulings")
    ids = [ruling.ruling_id for ruling in rulings]
    if len(set(ids)) != len(ids):
        raise RulingsBlockError(f"the rulings block repeats a ruling id: {ids}")


def with_rulings_block(body: str | None, rulings: Iterable[StandingRuling]) -> str:
    """*body* with its rulings block replaced by one for *rulings*.

    The block goes at the TOP of the body (where it already was, or first),
    because that is what an agent reads first; no rulings removes it. The rest
    of the body is kept as written.
    """
    head, inner, tail = _split_block(body or "")
    items = tuple(rulings)
    rest = f"{head}{tail}" if inner is not None else head
    rest = rest.lstrip("\n")
    updated = rest if not items else (
        f"{render_rulings_block(items)}\n\n{rest}" if rest else render_rulings_block(items)
    )
    if len(updated) > GITHUB_BODY_MAX_CHARS:
        raise RulingsBlockError(
            f"the issue body would be {len(updated)} characters with its rulings,"
            f" over GitHub's {GITHUB_BODY_MAX_CHARS}"
        )
    return updated


# -- review ---------------------------------------------------------------------


def rulings_covering(rulings: Iterable[StandingRuling], paths: Iterable[str]) -> tuple[StandingRuling, ...]:
    """The rulings a diff touching *paths* is governed by."""
    touched = tuple(paths)
    return tuple(ruling for ruling in rulings if ruling.scope.covers(touched))


def unattested_rulings(
    rulings: Iterable[StandingRuling], paths: Iterable[str], upheld: Iterable[str]
) -> tuple[StandingRuling, ...]:
    """The rulings covering the diff that an approval did not attest as upheld."""
    attested = frozenset(upheld)
    return tuple(ruling for ruling in rulings_covering(rulings, paths) if ruling.ruling_id not in attested)


def parse_upheld_rulings(value: Any) -> tuple[str, ...]:
    """A review's ``upheld_rulings`` attestation (untrusted): ids, bounded.

    Absent is no attestation. Anything but a list of ruling ids raises.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > MAX_UPHELD_RULINGS:
        raise ValueError(f"upheld_rulings is a list of at most {MAX_UPHELD_RULINGS} ruling ids")
    ids = tuple(value)
    for item in ids:
        if not isinstance(item, str) or not _RULING_ID.match(item):
            raise ValueError(f"upheld_rulings names a ruling id, got {item!r}")
    return ids


def upheld_rulings_of(payload: Any) -> tuple[str, ...]:
    """A review-exchange verdict's attestation: its ``decision.upheld_rulings``.

    Untrusted agent JSON. A malformed attestation attests nothing, so the
    review rule fails CLOSED: the approval is refused if a ruling needed it.
    """
    decision = payload.get("decision") if isinstance(payload, Mapping) else None
    raw = decision.get("upheld_rulings") if isinstance(decision, Mapping) else None
    try:
        return parse_upheld_rulings(raw)
    except ValueError:
        return ()


def refused_approval_feedback(refused: Iterable[StandingRuling], *, summary: str | None) -> str:
    """The implementation-required feedback an approval the rulings refused becomes."""
    items = tuple(refused)
    if not items:
        raise ValueError("an approval is refused for at least one ruling")
    listed = "\n".join(
        f"- Ruling `{ruling.ruling_id}` ({ruling.authority.described}, {ruling.source}): {ruling.summary}"
        for ruling in items
    )
    said = f"\n\nThe reviewer's own summary was: {summary.strip()}" if summary and summary.strip() else ""
    return (
        "Implementation-required: this change touches what a standing ruling on the issue"
        " governs, and the approval did not attest that the diff upholds it. The ruling binds"
        " this PR; make the PR implement it (and never keep, restore or extend what it retires)."
        f"\n\n{listed}\n\nThe full text of each ruling is at the top of the issue body.{said}"
        f"\n\n{REFUSED_APPROVAL_MARKER}"
    )


# -- prompts --------------------------------------------------------------------


class RulingsAudience(StrEnum):
    """How a prompt frames the rulings, by what its session does with them."""

    CODER = "coder"
    #: A rework of a published PR (a conflict, a failing check, review feedback):
    #: the rulings are its brief, whatever triggered it.
    REWORK_BRIEF = "rework_brief"
    REVIEWER = "reviewer"
    TECH_LEAD = "tech_lead"


def audience_for(kind: SessionKind) -> RulingsAudience:
    """The framing for a session of *kind* (its capability row decides).

    A rework always carries the rulings as its BRIEF: porchpin#379's conflict
    rework extended the retired design because its brief was only "resolve the
    conflict". Whether a ruling is already implemented is the reviewer's call,
    on the reworked diff, never an assumption the rework may make.
    """
    capabilities = kind.capabilities
    role = capabilities.sandbox_role
    if role is SandboxRole.REVIEWER:
        return RulingsAudience.REVIEWER
    if role is SandboxRole.TECH_LEAD:
        return RulingsAudience.TECH_LEAD
    if role is SandboxRole.CODER:
        return RulingsAudience.REWORK_BRIEF if capabilities.pushes_to_an_open_pr else RulingsAudience.CODER
    raise ValueError(f"a {kind.value} run has no agent prompt to carry rulings")


_PREAMBLE: Mapping[RulingsAudience, str] = {
    RulingsAudience.CODER: (
        "Build to these rulings. Each one overrides the issue's own Outcome, Scope or plan,"
        " any earlier design, review feedback and your own judgement wherever they conflict."
        " Do not reopen a ruling: if one cannot be satisfied, stop and ask with"
        " `coding-done needs_human` instead of working around it."
    ),
    RulingsAudience.REVIEWER: (
        "Check the diff against EACH ruling before your verdict. A diff that contradicts a"
        " ruling (including one that keeps, restores or extends what the ruling retires) is"
        " implementation-required: request changes and name the ruling id. To approve, attest"
        " every ruling you checked and found upheld: `reviewer-done approved ..."
        " --upholds-ruling <id>`, once per ruling (in a review exchange, list the ids in the"
        " `--decision-json` object's `upheld_rulings`). The orchestrator refuses an approval that leaves a"
        " ruling covering the changed files unattested and sends the PR back with that ruling"
        " as implementation-required feedback."
    ),
    RulingsAudience.TECH_LEAD: (
        "These are the maintainer's settled decisions for this issue. Never propose or take an"
        " action that contradicts one, and judge the issue's work against them: a ruling"
        " changes only when the maintainer changes it."
    ),
}


#: Around every binding section of rulings a prompt carries (#8347). Ruling
#: text, sources and scope entries may not contain the marker stem, so no ruling
#: can forge or truncate one; :func:`without_binding_sections` drops what they
#: enclose when a later prompt embeds an earlier one (a validation retry), so
#: only the rulings read for THAT launch bind it.
BINDING_BEGIN = "<!-- io:standing-ruling:binding:begin -->"
BINDING_END = "<!-- io:standing-ruling:binding:end -->"


def _binding(section: str) -> str:
    return f"{BINDING_BEGIN}\n{section}\n{BINDING_END}"


def without_binding_sections(prompt: str) -> str:
    """*prompt* without the binding sections of rulings it embeds.

    A validation retry embeds the original launch's prompt as its task; the
    rulings that prompt carried were read then. A ruling retired since must not
    bind the retry, and one recorded since must, so the retry drops the old
    sections and binds itself with the rulings read now. A begin marker without
    its end leaves the rest untouched (nothing a ruling wrote can cause it).
    """
    out = prompt
    while (start := out.find(BINDING_BEGIN)) != -1:
        end = out.find(BINDING_END, start)
        if end == -1:
            break
        tail = out[end + len(BINDING_END):]
        # The separator a launch prompt puts between its rulings and its task.
        tail = tail.removeprefix(RULINGS_SEPARATOR)
        out = out[:start] + tail
    return out


#: How a rework prompt opens the rulings it must implement (the brief).
REWORK_BRIEF_OPENING = "YOUR BRIEF FOR THIS REWORK"
#: How every prompt section of standing rulings begins.
RULINGS_PROMPT_HEADING = "## BINDING: standing rulings on issue #"


def _rework_brief(rulings: tuple[StandingRuling, ...]) -> str:
    ids = ", ".join(f"`{ruling.ruling_id}`" for ruling in rulings)
    return (
        f"{REWORK_BRIEF_OPENING}: this PR must implement the ruling(s) {ids}, whatever"
        " triggered this rework (a merge conflict, a failing check, review feedback). Check the"
        " PR against each ruling first and make it conform; resolve the trigger IN the ruled"
        " design. Keeping, restoring or extending anything a ruling retires (including while"
        " resolving a conflict) contradicts it, and the review will refuse it."
        f"\n\n{_PREAMBLE[RulingsAudience.CODER]}"
    )


def rulings_prompt(
    issue_number: int,
    rulings: Iterable[StandingRuling],
    audience: RulingsAudience,
) -> str | None:
    """The binding section for a prompt about issue *issue_number*; None without rulings."""
    items = tuple(rulings)
    if not items:
        return None
    preamble = _rework_brief(items) if audience is RulingsAudience.REWORK_BRIEF else _PREAMBLE[audience]
    rendered = "\n\n".join(ruling_in_full(ruling) for ruling in items)
    return _binding(
        f"{RULINGS_PROMPT_HEADING}{issue_number}\n\n"
        f"{preamble}\n\n{rendered}\n\n(End of the standing rulings on issue #{issue_number}.)"
    )


def ruling_in_full(ruling: StandingRuling) -> str:
    """One ruling as a prompt shows it: heading, authority, scope and full text."""
    scope = ruling.scope
    governs = ", ".join(f"`{pattern}`" for pattern in scope.files) or "the whole issue"
    settles = "".join(f"\n  - {claim}" for claim in scope.claims)
    return (
        f"### Ruling `{ruling.ruling_id}` ({ruling.authority.described}; {ruling.source};"
        f" recorded {ruling.recorded_at})\n"
        f"Governs: {governs}" + (f"\nSettles:{settles}" if settles else "") + f"\n\n{ruling.text}"
    )


#: How a tech lead's section of the rulings on the work it covers begins (#8347).
COVERED_RULINGS_HEADING = "## BINDING: standing rulings on the work this run covers"

_COVERED_PREAMBLE = (
    "These are the maintainer's settled decisions for the issues whose work this run"
    " reviews or may act on (a batch review's PRs, a health review's problem issues and"
    " the blocked items it triages), read for this launch: they supersede any ruling text"
    " in this run's data files."
    " Judge each PR against its issue's rulings: one that contradicts a ruling (including"
    " one that keeps, restores or extends what the ruling retires) is a finding to report"
    " and act on, never work to pass as sound. Never propose or take an action that"
    " contradicts a ruling: a ruling changes only when the maintainer changes it."
)


def covered_rulings_prompt(
    covered: Mapping[int, tuple[int, ...]],
    rulings: Mapping[int, tuple[StandingRuling, ...]],
) -> str | None:
    """The binding section for a tech-lead run over other issues' work (#8347).

    *covered* maps each issue the run covers to its PRs in the run (none for an
    issue it covers without a PR); *rulings* holds each one's rulings as its
    body records them. None when no covered issue has a ruling.
    """
    sections = []
    for issue_number in sorted(covered):
        items = rulings[issue_number]
        if not items:
            continue
        prs = covered[issue_number]
        of_prs = f" (PR {', '.join(f'#{pr}' for pr in sorted(prs))})" if prs else ""
        rendered = "\n\n".join(ruling_in_full(ruling) for ruling in items)
        sections.append(
            f"{RULINGS_PROMPT_HEADING}{issue_number}{of_prs}\n\n{rendered}"
            f"\n\n(End of the standing rulings on issue #{issue_number}.)"
        )
    if not sections:
        return None
    return _binding("\n\n".join((COVERED_RULINGS_HEADING, _COVERED_PREAMBLE, *sections)))
