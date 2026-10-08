"""When two improver findings are about one defect, whatever their kinds (#8700).

The effect keys (:mod:`..control.improver_effects`) identify a stall finding
by what it asks for about which anomalies, and a design finding by its kind
and slug. Two heats never pick the same slug, and a design finding and a
stall finding never share a key, so one defect could be filed twice: as a
design finding of one heat and a design finding of another (#8693/#8696), or
as a design finding and a stall finding (#8694/#8691).

A finding's :class:`DefectProfile` is where the defect lives and what it was
seen on:

* its **code sites** (:class:`CodeSite`, one function of one staged
  module, named in full): a stall finding's ``root_cause.owner`` and a
  design finding's ``owner``, each resolved in the staged engine source
  (an owner naming no one function there, e.g. a bare method name two
  classes define, is no site); and the function enclosing each line of the
  engine source a design finding cites;
* its **evidence**: the items (``#N``) it is about, the staged records it
  cites, and, for a design finding, its citations other than engine source.

Two findings, at least one of them a design finding, are the SAME DEFECT
(:func:`same_defect`) exactly when they share a code site AND overlap in
evidence (:meth:`DefectProfile.overlaps`: two design findings must cite one
thing). Neither is enough alone: one function can hold two defects, and
one item can show two. Two stall findings are never related here: a stall
finding's identity is its effect key, and two stall findings asking for
different things (an exam case and a fix) are two deliverables.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from ..contracts.improver_findings import DesignFinding, Finding, ToolCitation
from .improver_citations import LINE_SLACK, normalized

#: Where a staged engine source file sits in the run directory.
ENGINE_SOURCE_PREFIX = "improver-data/engine-source/"
#: How a stall finding cites an engine source file in ``stall_evidence``.
ENGINE_SOURCE_REF = "engine-source:"

_SYMBOL = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_ITEM = re.compile(r"(?<![\w#])#(\d+)\b")


class EngineSource(Protocol):
    """The staged engine source, read as Python (the execution side reads
    the run directory)."""

    def enclosing_function(self, path: str, line: int, quote: str) -> CodeSite | None:
        """The innermost function or method enclosing the line of run-dir
        file ``path`` that holds ``quote`` (within the citation slack of
        ``line``), named in full; None when that line is in no function, or
        the file is no staged Python source."""
        ...

    def resolve(self, written: CodeSite) -> CodeSite | None:
        """The one staged definition ``written`` names, in full: exactly one
        staged Python module whose dotted path ends with ``written.module``,
        defining exactly one class, function or module-level name whose
        qualified name ends with ``written.symbol``; None otherwise."""
        ...


@dataclass(frozen=True)
class CodeSite:
    """A function (or class, or name) in a module of the engine source: its
    module's dotted path and its qualified name.

    As an owner is WRITTEN, either may be partial
    (``control/completion_handler.py:_update_issue_machine``); resolved in
    the staged source (:meth:`EngineSource.resolve`) or read from it, both
    are complete (``issue_orchestrator.control.completion_handler``,
    ``CompletionHandler._update_issue_machine``), and two complete sites are
    one exactly when they are equal. A class is not the same site as one of
    its methods: one class can hold two defects."""

    module: tuple[str, ...]
    symbol: tuple[str, ...]


def module_path(path: str) -> tuple[str, ...] | None:
    """A Python file's dotted module path, from a run-dir citation path, a
    ``stall_evidence`` reference or a path in the source tree; None for a
    file that is not Python."""
    for prefix in (ENGINE_SOURCE_PREFIX, ENGINE_SOURCE_REF):
        path = path.removeprefix(prefix)
    if not path.endswith(".py"):
        return None
    parts = tuple(p for p in path.removesuffix(".py").split("/") if p)
    parts = parts[1:] if parts[:1] == ("src",) else parts
    return parts or None


def code_site(owner: str) -> CodeSite | None:
    """The site an owner names as WRITTEN (possibly partial), from
    ``<module>:<function>`` as the prompt asks a root cause's owner to be
    written (``control/completion_handler.py:_update_issue_machine`` or
    ``issue_orchestrator.control.completion_handler:CompletionHandler._update_issue_machine``);
    None when it is not written that way, which relates the finding to none."""
    module, sep, symbol = owner.strip().partition(":")
    symbol = symbol.strip().removesuffix("()")
    if not sep or not _SYMBOL.match(symbol):
        return None
    module = module.strip()
    dotted = (
        module_path(module)
        if module.endswith(".py")
        else tuple(module.removeprefix(ENGINE_SOURCE_REF).split("."))
    )
    if not dotted or not all(_SYMBOL.match(part) for part in dotted):
        return None
    return CodeSite(module=dotted, symbol=tuple(symbol.split(".")))


@dataclass(frozen=True)
class _Cited:
    """A non-code citation: a file line (``path``, ``line``) or a toolbox
    answer (``path`` None, ``line`` its call number; one run's heats share
    one toolbox, so a call number names one answer). One file line is
    compared loosely, since agents count lines inexactly and quote more or
    less of a line; one answer is one call (r4 F3: two answers can hold the
    same phrase)."""

    path: str | None
    line: int
    quote: str

    def same_as(self, other: _Cited) -> bool:
        if self.path != other.path:
            return False
        near = self.line == other.line if self.path is None else abs(self.line - other.line) <= 2 * LINE_SLACK
        return near and (self.quote in other.quote or other.quote in self.quote)


@dataclass(frozen=True)
class DefectProfile:
    """Where one finding's defect lives and what it was seen on."""

    finding_id: str
    is_design: bool
    sites: tuple[CodeSite, ...]
    #: The items (``#N``: issues and PRs) it is about.
    items: frozenset[int]
    #: Staged record ids a stall finding cites (decisions, case files, runs).
    records: frozenset[str]
    #: A design finding's citations other than engine source.
    cited: tuple[_Cited, ...]
    #: A design finding's quotes, normalized, for the records they name.
    quotes: tuple[str, ...]

    def shares_site(self, other: DefectProfile) -> bool:
        return bool(set(self.sites) & set(other.sites))

    def overlaps(self, other: DefectProfile) -> bool:
        """Evidence of one incident. Two design findings must cite one thing
        (r2 F2: two defects of one function can show on one item); a design
        finding and a stall finding share no citation form, so an item they
        are both about, or a staged record the stall finding cites and the
        design finding quotes, is their overlap."""
        if any(a.same_as(b) for a in self.cited for b in other.cited):
            return True
        if self.is_design and other.is_design:
            return False
        if self.items & other.items:
            return True
        return any(_names(quote, record) for record in self.records for quote in other.quotes) or any(
            _names(quote, record) for record in other.records for quote in self.quotes
        )


def same_defect(a: DefectProfile, b: DefectProfile) -> bool:
    """Whether two findings, at least one a design finding, are one defect:
    one code site AND overlapping evidence."""
    if not (a.is_design or b.is_design) or a.finding_id == b.finding_id:
        return False
    return a.shares_site(b) and a.overlaps(b)


def resolved_owner(owner: str, source: EngineSource) -> CodeSite | None:
    """The one staged definition an owner names, or None."""
    written = code_site(owner)
    return None if written is None else source.resolve(written)


def stall_profile(finding: Finding, source: EngineSource) -> DefectProfile:
    site = resolved_owner(finding.root_cause.owner, source) if finding.root_cause is not None else None
    return DefectProfile(
        finding_id=finding.id,
        is_design=False,
        sites=() if site is None else (site,),
        items=_items(k.subject for k in finding.anomaly_keys),
        records=frozenset(e for e in finding.stall_evidence if not e.startswith(ENGINE_SOURCE_REF)),
        cited=(),
        quotes=(),
    )


def design_profile(design: DesignFinding, source: EngineSource) -> DefectProfile:
    declared = resolved_owner(design.owner, source) if design.owner is not None else None
    sites: list[CodeSite] = [] if declared is None else [declared]
    cited: list[_Cited] = []
    for citation in design.evidence:
        quote = normalized(citation.quote)
        if isinstance(citation, ToolCitation):
            cited.append(_Cited(path=None, line=citation.call, quote=quote))
            continue
        module = module_path(citation.path) if citation.path.startswith(ENGINE_SOURCE_PREFIX) else None
        if module is None:
            cited.append(_Cited(path=citation.path, line=citation.line, quote=quote))
            continue
        function = source.enclosing_function(citation.path, citation.line, citation.quote)
        if function is not None:
            sites.append(function)
    return DefectProfile(
        finding_id=design.id,
        is_design=True,
        sites=tuple(dict.fromkeys(sites)),
        items=_items(c.quote for c in design.evidence),
        records=frozenset(),
        cited=tuple(cited),
        quotes=tuple(normalized(c.quote) for c in design.evidence),
    )


def _names(quote: str, record: str) -> bool:
    """Whether ``quote`` names the record id whole (r6 F1: ``D1`` is not
    ``D10``)."""
    return re.search(rf"(?<![\w-]){re.escape(record)}(?![\w-])", quote) is not None


def _items(texts: Iterable[str]) -> frozenset[int]:
    return frozenset(int(n) for text in texts for n in _ITEM.findall(text))


__all__ = [
    "ENGINE_SOURCE_PREFIX",
    "CodeSite",
    "DefectProfile",
    "EngineSource",
    "code_site",
    "design_profile",
    "module_path",
    "resolved_owner",
    "same_defect",
    "stall_profile",
]
