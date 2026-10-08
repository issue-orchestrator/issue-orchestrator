"""When two improver findings are about one defect, whatever their kinds (#8700).

The effect keys (:mod:`..control.improver_effects`) identify a stall finding
by what it asks for about which anomalies, and a design finding by its kind
and slug. Two heats never pick the same slug, and a design finding and a
stall finding never share a key, so one defect could be filed twice: as a
design finding of one heat and a design finding of another (#8693/#8696), or
as a design finding and a stall finding (#8694/#8691).

A finding's :class:`DefectProfile` is where the defect lives and what it was
seen on:

* its **code sites** (:class:`CodeSite`, a module and a function):
  a stall finding's ``root_cause.owner``; a design finding's ``owner``, and
  the function enclosing each line of the engine source it cites;
* its **evidence**: the items (``#N``) it is about, the staged records it
  cites, and, for a design finding, its citations other than engine source.

Two findings, at least one of them a design finding, are the SAME DEFECT
(:func:`same_defect`) exactly when they share a code site AND overlap in
evidence. Neither is enough alone: one function can hold two defects, and
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

    def enclosing_function(self, path: str, line: int, quote: str) -> tuple[str, ...] | None:
        """The qualified name (``("CompletionHandler", "_update_issue_machine")``)
        of the innermost function or method enclosing the line of run-dir
        file ``path`` that holds ``quote`` (within the citation slack of
        ``line``); None when that line is in no function, or the file is no
        staged Python source."""
        ...

    def defines(self, module: tuple[str, ...], symbol: tuple[str, ...]) -> bool:
        """Whether exactly one staged Python file is ``module`` (its dotted
        path, matched from the end) and it defines ``symbol`` (a class,
        function or module-level name, its qualified name matched from the
        end)."""
        ...


@dataclass(frozen=True)
class CodeSite:
    """A function (or class, or name) in a module of the engine source.

    Both parts are dotted paths as written, which may or may not be complete:
    ``control.completion_handler`` and
    ``issue_orchestrator.control.completion_handler`` are one module, and
    ``_update_issue_machine`` and ``CompletionHandler._update_issue_machine``
    one function. So two sites are one when, for each part, one path ends
    the other. A class is not the same site as one of its methods: one
    class can hold two defects."""

    module: tuple[str, ...]
    symbol: tuple[str, ...]

    def matches(self, other: CodeSite) -> bool:
        return _ends(self.module, other.module) and _ends(self.symbol, other.symbol)


def _ends(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    shorter, longer = sorted((a, b), key=len)
    return bool(shorter) and longer[len(longer) - len(shorter):] == shorter


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
    """``<module>:<function>`` as the prompt asks a root cause's owner to be
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
    """A non-code citation, compared loosely: agents count lines inexactly
    and quote more or less of one line."""

    path: str | None
    line: int
    quote: str

    def same_as(self, other: _Cited) -> bool:
        if self.path != other.path:
            return False
        if self.path is not None and abs(self.line - other.line) > 2 * LINE_SLACK:
            return False
        return self.quote in other.quote or other.quote in self.quote


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
        return any(a.matches(b) for a in self.sites for b in other.sites)

    def overlaps(self, other: DefectProfile) -> bool:
        if self.items & other.items:
            return True
        if any(a.same_as(b) for a in self.cited for b in other.cited):
            return True
        return any(record in quote for record in self.records for quote in other.quotes) or any(
            record in quote for record in other.records for quote in self.quotes
        )


def same_defect(a: DefectProfile, b: DefectProfile) -> bool:
    """Whether two findings, at least one a design finding, are one defect:
    one code site AND overlapping evidence."""
    if not (a.is_design or b.is_design) or a.finding_id == b.finding_id:
        return False
    return a.shares_site(b) and a.overlaps(b)


def stall_profile(finding: Finding) -> DefectProfile:
    site = code_site(finding.root_cause.owner) if finding.root_cause is not None else None
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
    declared = code_site(design.owner) if design.owner is not None else None
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
            sites.append(CodeSite(module=module, symbol=function))
    return DefectProfile(
        finding_id=design.id,
        is_design=True,
        sites=tuple(dict.fromkeys(sites)),
        items=_items(c.quote for c in design.evidence),
        records=frozenset(),
        cited=tuple(cited),
        quotes=tuple(normalized(c.quote) for c in design.evidence),
    )


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
    "same_defect",
    "stall_profile",
]
