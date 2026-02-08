"""Invariant tests for queue cache mutation boundaries."""

from pathlib import Path


def test_cached_queue_issues_is_mutated_only_in_queue_abstractions():
    src_root = Path(__file__).parent.parent.parent / "src" / "issue_orchestrator"
    offenders: list[str] = []
    allowed_files = {
        src_root / "control" / "queue_cache.py",
        src_root / "control" / "queue_projection.py",
    }
    mutation_tokens = ("cached_queue_issues =", "cached_queue_issues.append(", "cached_queue_issues[")

    for path in src_root.rglob("*.py"):
        if path in allowed_files:
            continue
        text = path.read_text()
        for token in mutation_tokens:
            if token in text:
                offenders.append(f"{path.relative_to(src_root)}: {token}")

    assert offenders == [], (
        "Queue cache mutations must go through queue abstraction.\n"
        + "\n".join(offenders)
    )
