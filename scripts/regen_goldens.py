#!/usr/bin/env python3
"""Regenerate the expected blocks of timeline golden fixtures.

Reads each fixture's `records:` block, runs `project_timeline()` and the
`produce_external_records()` fan-out pipeline, and rewrites the
`internal_timeline:` and (if present) `external_timeline:` blocks with
the canonical projection output.

Comments outside the regenerated blocks are preserved verbatim
(scenario / description / records / inline records comments). Comments
inside the regenerated blocks are dropped — those blocks are
machine-managed; explanatory text belongs in `description:` or as
records-block comments.

Usage:

    python scripts/regen_goldens.py [path ...]

With no arguments, regenerates every `*.yaml` under
`tests/fixtures/timeline/golden/`. Each path argument may be a fixture
file or a directory.

Workflow:

    1. Make a behavior change (touch a projection helper, narrative
       enricher, view registry, etc.).
    2. Run `scripts/regen_goldens.py` with no arguments.
    3. `git diff` shows exactly which scenarios changed and how.
    4. Commit the source change and the regenerated fixtures together.

Tracked fields: only the per-event keys the goldens currently assert
(`event`, `phase`, `step`, `status`, `level`, `parent_key`,
`logical_phase`, `narrative`, `summary`, `detail`, `artifacts`).
Optional fields that are None / empty are omitted, matching the
existing fixture style.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from issue_orchestrator.events.fan_out_pipeline import produce_external_records  # noqa: E402
from issue_orchestrator.ports.timeline_store import TimelineRecord  # noqa: E402
from issue_orchestrator.timeline import TimelineEvent, project_timeline  # noqa: E402


# Order matters: the regen produces fields in this order so diffs
# stay stable across runs. Drop any that come back None / empty.
TRACKED_FIELDS = (
    "event",
    "phase",
    "step",
    "status",
    "level",
    "logical_phase",
    "narrative",
    "summary",
    "detail",
    "parent_key",
    "artifacts",
)

DEFAULT_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "timeline" / "golden"


def regen_fixture(path: Path) -> bool:
    """Regenerate one fixture in place. Returns True if the file changed."""
    original = path.read_text()
    fixture = yaml.safe_load(original)

    if "records" not in fixture or "internal_timeline" not in fixture:
        # Not a goldens fixture in the format we manage; leave alone.
        return False

    issue_number = fixture["issue_number"]
    records_data: list[dict[str, Any]] = fixture["records"]

    # ----- Internal timeline: project the input records directly. -----
    internal_records = [_record_from_entry(entry, i) for i, entry in enumerate(records_data)]
    internal_events = project_timeline(internal_records, issue_number=issue_number)
    new_internal_block = _format_block(internal_events)

    new_text = _replace_block(original, "internal_timeline:", new_internal_block)

    # ----- External timeline: fan out + project + filter to view. -----
    if "external_timeline" in fixture:
        view = fixture.get("external_view", "user")
        fanned: list[TimelineRecord] = []
        for i, entry in enumerate(records_data):
            fanned.extend(
                produce_external_records(
                    internal_event_name=entry["event"],
                    enriched_data=entry.get("data", {}),
                    base_event_id=f"i-{i:04d}",
                    timestamp_iso=entry["timestamp"],
                )
            )
        external_events_all = project_timeline(fanned, issue_number=issue_number)
        external_events = [e for e in external_events_all if e.views and view in e.views]
        new_external_block = _format_block(external_events)
        new_text = _replace_block(new_text, "external_timeline:", new_external_block)

    if new_text != original:
        path.write_text(new_text)
        return True
    return False


def _record_from_entry(entry: dict[str, Any], index: int) -> TimelineRecord:
    return TimelineRecord(
        event_id=f"i-{index:04d}",
        timestamp=entry["timestamp"],
        event=entry["event"],
        data=entry.get("data", {}),
        source_event=entry.get("source_event", entry["event"]),
    )


def _format_block(events: list[TimelineEvent]) -> list[str]:
    """Format a list of TimelineEvents as fixture lines (without the
    `internal_timeline:` / `external_timeline:` header line itself).

    Each event becomes either a single-line flow-style mapping (when no
    list-valued fields are present) or a block mapping (when artifacts
    are present, since flow-style nested lists are unreadable).
    """
    lines: list[str] = []
    for ev in events:
        d = ev.to_dict()
        ordered = [(k, d.get(k)) for k in TRACKED_FIELDS if _included(d.get(k))]
        artifacts = next((v for k, v in ordered if k == "artifacts"), None)
        scalar_fields = [(k, v) for k, v in ordered if k != "artifacts"]
        if artifacts:
            # Block style with artifacts as a sub-list.
            lines.append("  - " + _format_first_field(scalar_fields[0]))
            for k, v in scalar_fields[1:]:
                lines.append("    " + _format_field(k, v))
            lines.append("    artifacts:")
            for art in artifacts:
                lines.append(
                    f"      - {{ type: {art['type']}, label: {_yaml_scalar(art['label'])}, "
                    f"value: {_yaml_scalar(art['value'])} }}"
                )
        else:
            inner = ", ".join(_format_field(k, v) for k, v in scalar_fields)
            lines.append(f"  - {{ {inner} }}")
    return lines


def _included(value: Any) -> bool:
    if value is None:
        return False
    if value == "":
        return False
    if isinstance(value, list) and not value:
        return False
    return True


def _format_field(key: str, value: Any) -> str:
    return f"{key}: {_yaml_scalar(value)}"


def _format_first_field(field: tuple[str, Any]) -> str:
    """Format a field as the leading entry of a block-style item."""
    k, v = field
    return f"{k}: {_yaml_scalar(v)}"


def _yaml_scalar(value: Any) -> str:
    """Format a scalar for inclusion in a flow-style line.

    Quotes strings consistently. Numbers, bools, None are rendered as
    their YAML literal forms. Strings that contain special characters
    (`:`, leading whitespace, `[`, `{`, etc.) are double-quoted; simple
    identifier-like strings are emitted unquoted to match the existing
    fixture style.
    """
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if _needs_quoting(value):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return value
    # Fallback: rely on yaml.safe_dump to do something reasonable.
    return yaml.safe_dump(value, default_flow_style=True).strip()


def _needs_quoting(s: str) -> bool:
    if not s:
        return True
    if s.lower() in {"true", "false", "null", "yes", "no", "on", "off"}:
        return True
    if s[0] in " \t-?:[]{}#&*!|>'\"%@`," or s[-1] in " \t":
        return True
    # Any non-identifier character → quote. Conservative.
    for ch in s:
        if ch in ":#'\"\n\\":
            return True
    # Looks like a number? quote
    try:
        float(s)
        return True
    except ValueError:
        pass
    return False


def _replace_block(text: str, key: str, new_block_lines: list[str]) -> str:
    """Replace the YAML block under top-level `key:` with `new_block_lines`.

    The block extends from the line after `key:` (which must appear at
    column 0) up to (but not including) the next top-level key or EOF.
    Top-level keys are detected as lines starting at column 0 with a
    `<word>:` form.
    """
    lines = text.splitlines()
    # Locate the header line.
    header_idx = next(
        (i for i, line in enumerate(lines) if line.rstrip() == key),
        None,
    )
    if header_idx is None:
        raise ValueError(f"Could not find top-level key {key!r} in fixture")

    # Find the end of the block: the next top-level key, or EOF.
    end_idx = len(lines)
    for j in range(header_idx + 1, len(lines)):
        line = lines[j]
        if line and not line.startswith((" ", "\t", "#")) and ":" in line:
            end_idx = j
            break

    # Reconstruct.
    out = lines[: header_idx + 1] + new_block_lines + [""] + lines[end_idx:]
    # Re-add trailing newline if the original had one.
    result = "\n".join(out)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _iter_paths(args: list[str]) -> list[Path]:
    if not args:
        return sorted(DEFAULT_FIXTURE_DIR.glob("*.yaml"))
    out: list[Path] = []
    for arg in args:
        p = Path(arg)
        if p.is_dir():
            out.extend(sorted(p.glob("*.yaml")))
        else:
            out.append(p)
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "paths",
        nargs="*",
        help="Fixture file or directory. Defaults to all goldens.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any fixture is out of sync (for CI).",
    )
    ns = parser.parse_args(argv)

    paths = _iter_paths(ns.paths)
    if not paths:
        print("No fixture files found.", file=sys.stderr)
        return 1

    changed: list[Path] = []
    for path in paths:
        try:
            if regen_fixture(path):
                changed.append(path)
        except Exception as exc:
            print(f"FAIL {path}: {exc}", file=sys.stderr)
            return 2

    if ns.check:
        if changed:
            print("Goldens out of sync:", file=sys.stderr)
            for p in changed:
                print(f"  {p}", file=sys.stderr)
            print(
                "Run `python scripts/regen_goldens.py` to update.",
                file=sys.stderr,
            )
            return 1
        print(f"All {len(paths)} fixtures in sync.")
        return 0

    if changed:
        print(f"Updated {len(changed)} fixture(s):")
        for p in changed:
            print(f"  {p}")
    else:
        print(f"All {len(paths)} fixtures already in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
