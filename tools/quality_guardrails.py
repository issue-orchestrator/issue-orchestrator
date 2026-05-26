#!/usr/bin/env python3
"""Ratcheted quality guardrails for architecture-control drift.

Existing debt is recorded once; future PRs fail when they add new debt or make a
tracked metric worse.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml


BASELINE_VERSION = 1
EXIT_RATCHET_VIOLATION = 2
EXIT_STALE_BASELINE = 3
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_RUN = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class Metric:
    rule_id: str
    kind: str
    metric_id: str
    value: int
    path: str
    detail: str
    new_metric_min_value: int = 1

    @property
    def key(self) -> str:
        return f"{self.rule_id}:{self.metric_id}"

    def to_baseline_entry(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "metric_id": self.metric_id,
            "value": self.value,
            "path": self.path,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RatchetViolation:
    key: str
    rule_id: str
    kind: str
    path: str
    previous: int | None
    current: int
    detail: str

    def fmt(self) -> str:
        if self.previous is None:
            return (
                f"{self.path} [{self.rule_id}] new {self.kind}: "
                f"{self.current} ({self.detail})"
            )
        return (
            f"{self.path} [{self.rule_id}] {self.kind} increased: "
            f"{self.previous} -> {self.current} ({self.detail})"
        )


@dataclass(frozen=True)
class StaleBaselineEntry:
    key: str
    rule_id: str
    kind: str
    path: str
    value: int
    detail: str

    def fmt(self) -> str:
        return f"{self.path} [{self.rule_id}] stale {self.kind}: {self.value} ({self.detail})"


@dataclass(frozen=True)
class GuardrailResult:
    violations: list[RatchetViolation]
    stale_entries: list[StaleBaselineEntry]


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at top level")
    return data


def _load_baseline(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _write_baseline_data(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_baseline(path: Path, metrics: Sequence[Metric]) -> None:
    payload = {
        "version": BASELINE_VERSION,
        "metrics": {
            metric.key: metric.to_baseline_entry()
            for metric in sorted(metrics, key=lambda m: (m.rule_id, m.path, m.metric_id))
        },
    }
    _write_baseline_data(path, payload)


def _patterns(rule: Mapping[str, Any], key: str) -> list[str]:
    value = rule.get(key, []) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"rule {rule.get('id', '<unknown>')} field {key!r} must be a list of strings")
    return value


def _new_metric_min_value(rule: Mapping[str, Any]) -> int:
    value = int(rule.get("new_metric_min_value", 1))
    if value < 1:
        raise ValueError(f"rule {rule.get('id', '<unknown>')} new_metric_min_value must be >= 1")
    return value


def _is_excluded(rel_path: str, excludes: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in excludes)


def _iter_included_files(root: Path, rule: Mapping[str, Any]) -> Iterable[Path]:
    includes = _patterns(rule, "include")
    excludes = _patterns(rule, "exclude")
    seen: set[Path] = set()

    for pattern in includes:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            rel_path = path.relative_to(root).as_posix()
            if _is_excluded(rel_path, excludes):
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _collect_file_line_budget(root: Path, rule: Mapping[str, Any]) -> list[Metric]:
    rule_id = str(rule["id"])
    max_lines = int(rule["max_lines"])
    new_metric_min_value = _new_metric_min_value(rule)
    metrics: list[Metric] = []

    for path in _iter_included_files(root, rule):
        count = _line_count(path)
        if count <= max_lines:
            continue
        rel_path = path.relative_to(root).as_posix()
        metrics.append(
            Metric(
                rule_id=rule_id,
                kind="file_line_budget",
                metric_id=rel_path,
                value=count,
                path=rel_path,
                detail=f"{count} lines exceeds budget {max_lines}",
                new_metric_min_value=new_metric_min_value,
            )
        )

    return metrics


def _control_term_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_RUN.finditer(text):
        for part in _CAMEL_BOUNDARY.sub(" ", match.group(0)).split():
            tokens.append(part.lower())
    return tokens


def _control_phrase_tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN_RUN.finditer(text)]


def _token_matches_term(token: str, term: str) -> bool:
    return token in {term, f"{term}s", f"{term}es"} or (term.endswith("y") and token == f"{term[:-1]}ies")


def _contains_term_tokens(text_tokens: Sequence[str], term_tokens: Sequence[str]) -> bool:
    if not text_tokens or not term_tokens:
        return False
    if len(term_tokens) == 1:
        return any(_token_matches_term(token, term_tokens[0]) for token in text_tokens)
    width = len(term_tokens)
    return any(list(text_tokens[index : index + width]) == list(term_tokens) for index in range(len(text_tokens) - width + 1))


def _contains_any_term(text: str, terms: Sequence[str]) -> bool:
    text_tokens = _control_term_tokens(text)
    phrase_tokens = _control_phrase_tokens(text)
    for term in terms:
        term_tokens = _control_term_tokens(term)
        if len(term_tokens) == 1 and _contains_term_tokens(text_tokens, term_tokens):
            return True
        if len(term_tokens) > 1 and _contains_term_tokens(phrase_tokens, term_tokens):
            return True
    return False


def _python_policy_site_count(path: Path, terms: Sequence[str]) -> int:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path.as_posix())
    count = 0

    for node in ast.walk(tree):
        candidate: ast.AST | None = None
        if isinstance(node, (ast.If, ast.While, ast.IfExp)):
            candidate = node.test
        elif isinstance(node, ast.Match):
            candidate = node.subject

        if candidate is None:
            continue

        segment = ast.get_source_segment(source, candidate)
        if segment and _contains_any_term(segment, terms):
            count += 1

    return count


def _strip_js_comment(line: str) -> str:
    return line.split("//", 1)[0]


def _js_policy_site_count(path: Path, terms: Sequence[str]) -> int:
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = _strip_js_comment(line).strip()
        if not stripped:
            continue
        if not (
            stripped.startswith("if ")
            or stripped.startswith("if(")
            or stripped.startswith("switch ")
            or stripped.startswith("switch(")
            or stripped.startswith("case ")
            or " if (" in stripped
        ):
            continue
        if _contains_any_term(stripped, terms):
            count += 1
    return count


def _collect_control_policy_branch_sites(root: Path, rule: Mapping[str, Any]) -> list[Metric]:
    rule_id = str(rule["id"])
    terms = _patterns(rule, "terms")
    new_metric_min_value = _new_metric_min_value(rule)
    metrics: list[Metric] = []

    for path in _iter_included_files(root, rule):
        rel_path = path.relative_to(root).as_posix()
        try:
            if path.suffix == ".py":
                count = _python_policy_site_count(path, terms)
            elif path.suffix == ".js":
                count = _js_policy_site_count(path, terms)
            else:
                continue
        except SyntaxError as exc:
            raise ValueError(f"{rel_path}: syntax error while collecting policy sites: {exc}") from exc

        if count == 0:
            continue
        metrics.append(
            Metric(
                rule_id=rule_id,
                kind="control_policy_branch_sites",
                metric_id=rel_path,
                value=count,
                path=rel_path,
                detail=f"{count} branch site(s) mention lifecycle/control terms",
                new_metric_min_value=new_metric_min_value,
            )
        )

    return metrics


def collect_metrics(root: Path, config: Mapping[str, Any]) -> list[Metric]:
    rules = config.get("rules", []) or []
    if not isinstance(rules, list):
        raise ValueError("quality guardrail config field 'rules' must be a list")

    collectors = {
        "file_line_budget": _collect_file_line_budget,
        "control_policy_branch_sites": _collect_control_policy_branch_sites,
    }
    metrics: list[Metric] = []

    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("quality guardrail rules must be mappings")
        rule_id = rule.get("id")
        rule_type = rule.get("type")
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError("quality guardrail rule missing non-empty 'id'")
        if rule_type not in collectors:
            raise ValueError(f"quality guardrail rule {rule_id!r} has unsupported type {rule_type!r}")
        metrics.extend(collectors[rule_type](root, rule))

    return sorted(metrics, key=lambda m: (m.rule_id, m.path, m.metric_id))


def compare_to_baseline(metrics: Sequence[Metric], baseline: Mapping[str, Any]) -> list[RatchetViolation]:
    raw_entries = _baseline_entries(baseline)

    current_by_key = {metric.key: metric for metric in metrics}
    violations: list[RatchetViolation] = []

    for key, metric in sorted(current_by_key.items()):
        raw_previous = raw_entries.get(key)
        if raw_previous is None:
            if metric.value < metric.new_metric_min_value:
                continue
            violations.append(
                RatchetViolation(
                    key=key,
                    rule_id=metric.rule_id,
                    kind=metric.kind,
                    path=metric.path,
                    previous=None,
                    current=metric.value,
                    detail=metric.detail,
                )
            )
            continue
        previous = _baseline_entry_mapping(key, raw_previous)
        previous_value = _baseline_entry_int(key, previous, "value")
        if metric.value > previous_value:
            violations.append(
                RatchetViolation(
                    key=key,
                    rule_id=metric.rule_id,
                    kind=metric.kind,
                    path=metric.path,
                    previous=previous_value,
                    current=metric.value,
                    detail=metric.detail,
                )
            )

    return violations


def _baseline_entries(baseline: Mapping[str, Any]) -> dict[str, Any]:
    raw_entries = baseline.get("metrics", {}) or {}
    if not isinstance(raw_entries, dict):
        raise ValueError("baseline field 'metrics' must be a mapping")
    return raw_entries


def _baseline_entry_mapping(key: str, raw_entry: Any) -> Mapping[str, Any]:
    if not isinstance(raw_entry, dict):
        raise ValueError(f"baseline entry {key!r} must be a mapping")
    return raw_entry


def _baseline_entry_text(key: str, raw_entry: Mapping[str, Any], field: str) -> str:
    value = raw_entry.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"baseline entry {key!r} missing non-empty string field {field!r}")
    return value


def _baseline_entry_int(key: str, raw_entry: Mapping[str, Any], field: str) -> int:
    value = raw_entry.get(field)
    if not isinstance(value, int):
        raise ValueError(f"baseline entry {key!r} missing integer field {field!r}")
    return value


def find_stale_baseline_entries(
    metrics: Sequence[Metric],
    baseline: Mapping[str, Any],
) -> list[StaleBaselineEntry]:
    current_keys = {metric.key for metric in metrics}
    stale: list[StaleBaselineEntry] = []

    for key, raw_entry in sorted(_baseline_entries(baseline).items()):
        if key in current_keys:
            continue
        entry = _baseline_entry_mapping(key, raw_entry)
        stale.append(
            StaleBaselineEntry(
                key=key,
                rule_id=_baseline_entry_text(key, entry, "rule_id"),
                kind=_baseline_entry_text(key, entry, "kind"),
                path=_baseline_entry_text(key, entry, "path"),
                value=_baseline_entry_int(key, entry, "value"),
                detail=_baseline_entry_text(key, entry, "detail"),
            )
        )

    return stale


def accept_baseline_keys(
    baseline_path: Path,
    metrics: Sequence[Metric],
    keys: Sequence[str],
) -> dict[str, Any]:
    if not baseline_path.exists():
        raise ValueError(f"baseline not found: {baseline_path}")

    baseline = _load_baseline(baseline_path)
    raw_entries = baseline.setdefault("metrics", {})
    if not isinstance(raw_entries, dict):
        raise ValueError("baseline field 'metrics' must be a mapping")

    current_by_key = {metric.key: metric for metric in metrics}
    missing = [key for key in keys if key not in current_by_key]
    if missing:
        raise ValueError(f"cannot accept missing current metric(s): {', '.join(missing)}")

    baseline["version"] = BASELINE_VERSION
    for key in keys:
        raw_entries[key] = current_by_key[key].to_baseline_entry()

    _write_baseline_data(baseline_path, baseline)
    return baseline


def prune_baseline_keys(
    baseline_path: Path,
    metrics: Sequence[Metric],
    keys: Sequence[str],
) -> dict[str, Any]:
    if not baseline_path.exists():
        raise ValueError(f"baseline not found: {baseline_path}")

    baseline = _load_baseline(baseline_path)
    raw_entries = _baseline_entries(baseline)
    current_keys = {metric.key for metric in metrics}

    missing = [key for key in keys if key not in raw_entries]
    if missing:
        raise ValueError(f"cannot prune key(s) not present in baseline: {', '.join(missing)}")

    current = [key for key in keys if key in current_keys]
    if current:
        raise ValueError(f"cannot prune current metric key(s): {', '.join(current)}")

    baseline["version"] = BASELINE_VERSION
    for key in keys:
        del raw_entries[key]

    _write_baseline_data(baseline_path, baseline)
    return baseline


def _print_text_report(
    metrics: Sequence[Metric],
    violations: Sequence[RatchetViolation],
    stale_entries: Sequence[StaleBaselineEntry],
) -> None:
    print(f"Quality guardrails tracked metrics: {len(metrics)}")
    by_rule: dict[str, int] = {}
    for metric in metrics:
        by_rule[metric.rule_id] = by_rule.get(metric.rule_id, 0) + 1
    for rule_id, count in sorted(by_rule.items()):
        print(f"  {rule_id}: {count}")

    if stale_entries:
        print("\nQuality guardrails stale baseline entries:", file=sys.stderr)
        for entry in stale_entries:
            print(f"  {entry.fmt()}", file=sys.stderr)

    if violations:
        print("\nQuality guardrails ratchet violations:", file=sys.stderr)
        for violation in violations:
            print(f"  {violation.fmt()}", file=sys.stderr)


def _print_json_report(
    metrics: Sequence[Metric],
    violations: Sequence[RatchetViolation],
    stale_entries: Sequence[StaleBaselineEntry],
) -> None:
    payload = {
        "metrics": [metric.to_baseline_entry() for metric in metrics],
        "stale_entries": [
            {
                "key": entry.key,
                "rule_id": entry.rule_id,
                "kind": entry.kind,
                "path": entry.path,
                "value": entry.value,
                "detail": entry.detail,
            }
            for entry in stale_entries
        ],
        "violations": [
            {
                "key": violation.key,
                "rule_id": violation.rule_id,
                "kind": violation.kind,
                "path": violation.path,
                "previous": violation.previous,
                "current": violation.current,
                "detail": violation.detail,
            }
            for violation in violations
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def _operation_count(args: argparse.Namespace) -> int:
    return int(args.update_baseline) + int(bool(args.accept)) + int(bool(args.prune))


def _load_required_baseline(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"baseline not found: {path}")
    return _load_baseline(path)


def _compare_and_check_stale(
    metrics: Sequence[Metric],
    baseline: Mapping[str, Any],
    *,
    check_stale: bool,
) -> GuardrailResult:
    stale_entries = find_stale_baseline_entries(metrics, baseline) if check_stale else []
    return GuardrailResult(
        violations=compare_to_baseline(metrics, baseline),
        stale_entries=stale_entries,
    )


def _run_guardrail_operation(
    args: argparse.Namespace,
    baseline_path: Path,
    metrics: Sequence[Metric],
) -> GuardrailResult:
    if _operation_count(args) > 1:
        raise ValueError("--update-baseline, --accept, and --prune cannot be combined")

    if args.update_baseline:
        _write_baseline(baseline_path, metrics)
        return GuardrailResult(violations=[], stale_entries=[])

    if args.accept:
        baseline = accept_baseline_keys(baseline_path, metrics, args.accept)
        return _compare_and_check_stale(metrics, baseline, check_stale=args.check_stale)

    if args.prune:
        baseline = prune_baseline_keys(baseline_path, metrics, args.prune)
        return _compare_and_check_stale(metrics, baseline, check_stale=args.check_stale)

    if args.fail_on_new or args.check_stale:
        baseline = _load_required_baseline(baseline_path)
        return GuardrailResult(
            violations=compare_to_baseline(metrics, baseline) if args.fail_on_new else [],
            stale_entries=find_stale_baseline_entries(metrics, baseline) if args.check_stale else [],
        )

    return GuardrailResult(violations=[], stale_entries=[])


def _print_operation_messages(args: argparse.Namespace, root: Path, baseline_path: Path) -> None:
    if args.update_baseline:
        print(f"Quality guardrails baseline updated: {baseline_path.relative_to(root)}")
    if args.accept:
        accepted = ", ".join(args.accept)
        print(f"Quality guardrails accepted baseline key(s): {accepted}")
    if args.prune:
        pruned = ", ".join(args.prune)
        print(f"Quality guardrails pruned baseline key(s): {pruned}")


def _result_exit_code(result: GuardrailResult) -> int:
    if result.violations:
        return EXIT_RATCHET_VIOLATION
    if result.stale_entries:
        return EXIT_STALE_BASELINE
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root to scan")
    parser.add_argument("--config", default="tools/quality_guardrails.yml")
    parser.add_argument("--baseline", default="quality/guardrails-baseline.json")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument(
        "--accept",
        action="append",
        default=[],
        metavar="KEY",
        help="Update one baseline metric key from current results without rewriting all entries",
    )
    parser.add_argument(
        "--prune",
        action="append",
        default=[],
        metavar="KEY",
        help="Remove one stale baseline metric key without rewriting all entries",
    )
    parser.add_argument("--check-stale", action="store_true", help="Fail when the baseline contains stale entries")
    parser.add_argument("--fail-on-new", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    config_path = (root / args.config).resolve()
    baseline_path = (root / args.baseline).resolve()

    try:
        config = _load_yaml(config_path)
        metrics = collect_metrics(root, config)
        result = _run_guardrail_operation(args, baseline_path, metrics)

        if args.format == "json":
            _print_json_report(metrics, result.violations, result.stale_entries)
        else:
            _print_operation_messages(args, root, baseline_path)
            _print_text_report(metrics, result.violations, result.stale_entries)

        return _result_exit_code(result)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        print(f"Quality guardrails error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
