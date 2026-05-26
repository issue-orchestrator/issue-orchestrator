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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml


BASELINE_VERSION = 1


@dataclass(frozen=True)
class Metric:
    rule_id: str
    kind: str
    metric_id: str
    value: int
    path: str
    detail: str

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


def _write_baseline(path: Path, metrics: Sequence[Metric]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": BASELINE_VERSION,
        "metrics": {
            metric.key: metric.to_baseline_entry()
            for metric in sorted(metrics, key=lambda m: (m.rule_id, m.path, m.metric_id))
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _patterns(rule: Mapping[str, Any], key: str) -> list[str]:
    value = rule.get(key, []) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"rule {rule.get('id', '<unknown>')} field {key!r} must be a list of strings")
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
            )
        )

    return metrics


def _contains_any_term(text: str, terms: Sequence[str]) -> bool:
    lower = text.lower()
    return any(term.lower() in lower for term in terms)


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
    raw_entries = baseline.get("metrics", {}) or {}
    if not isinstance(raw_entries, dict):
        raise ValueError("baseline field 'metrics' must be a mapping")

    current_by_key = {metric.key: metric for metric in metrics}
    violations: list[RatchetViolation] = []

    for key, metric in sorted(current_by_key.items()):
        raw_previous = raw_entries.get(key)
        if raw_previous is None:
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
        if not isinstance(raw_previous, dict):
            raise ValueError(f"baseline entry {key!r} must be a mapping")
        previous_value = int(raw_previous.get("value", 0))
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


def _print_text_report(metrics: Sequence[Metric], violations: Sequence[RatchetViolation]) -> None:
    print(f"Quality guardrails tracked metrics: {len(metrics)}")
    by_rule: dict[str, int] = {}
    for metric in metrics:
        by_rule[metric.rule_id] = by_rule.get(metric.rule_id, 0) + 1
    for rule_id, count in sorted(by_rule.items()):
        print(f"  {rule_id}: {count}")

    if violations:
        print("\nQuality guardrails ratchet violations:", file=sys.stderr)
        for violation in violations:
            print(f"  {violation.fmt()}", file=sys.stderr)


def _print_json_report(metrics: Sequence[Metric], violations: Sequence[RatchetViolation]) -> None:
    payload = {
        "metrics": [metric.to_baseline_entry() for metric in metrics],
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root to scan")
    parser.add_argument("--config", default="tools/quality_guardrails.yml")
    parser.add_argument("--baseline", default="quality/guardrails-baseline.json")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--fail-on-new", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    config_path = (root / args.config).resolve()
    baseline_path = (root / args.baseline).resolve()

    try:
        config = _load_yaml(config_path)
        metrics = collect_metrics(root, config)
        violations: list[RatchetViolation] = []

        if args.update_baseline:
            _write_baseline(baseline_path, metrics)
        elif args.fail_on_new:
            if not baseline_path.exists():
                print(f"Quality guardrails baseline not found: {baseline_path}", file=sys.stderr)
                return 1
            violations = compare_to_baseline(metrics, _load_baseline(baseline_path))

        if args.format == "json":
            _print_json_report(metrics, violations)
        else:
            if args.update_baseline:
                print(f"Quality guardrails baseline updated: {baseline_path.relative_to(root)}")
            _print_text_report(metrics, violations)

        return 2 if violations else 0
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        print(f"Quality guardrails error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
