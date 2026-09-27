"""An exam run's scorecard survives a failing cleanup (round 12 F1)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.e2e.exam.recording import run_recorded


class CleanupFailed(RuntimeError):
    pass


def _record_to(tmp_path: Path):
    def record(result: str) -> Path:
        path = tmp_path / "scorecard.json"
        path.write_text(result, encoding="utf-8")
        return path

    return record


def test_the_scorecard_is_written_before_a_failing_cleanup_raises(tmp_path: Path) -> None:
    attempted: list[str] = []

    async def run_case() -> str:
        return '{"passed": false}'

    def cleanup() -> None:
        attempted.append("cleanup")
        raise CleanupFailed("GitHub hiccup")

    with pytest.raises(CleanupFailed):
        asyncio.run(run_recorded(run_case, record=_record_to(tmp_path), cleanup=cleanup))

    assert (tmp_path / "scorecard.json").read_text(encoding="utf-8") == '{"passed": false}'
    assert attempted == ["cleanup"]


def test_a_failed_run_still_cleans_up_and_neither_failure_hides_the_other(tmp_path: Path) -> None:
    async def run_case() -> str:
        raise RuntimeError("engine checkout failed")

    def cleanup() -> None:
        raise CleanupFailed("GitHub hiccup")

    with pytest.raises(BaseExceptionGroup) as caught:
        asyncio.run(run_recorded(run_case, record=_record_to(tmp_path), cleanup=cleanup))

    assert [str(e) for e in caught.value.exceptions] == ["engine checkout failed", "GitHub hiccup"]
    assert not (tmp_path / "scorecard.json").exists()


def test_a_failed_run_with_clean_cleanup_raises_the_run_failure(tmp_path: Path) -> None:
    cleaned: list[str] = []

    async def run_case() -> str:
        raise RuntimeError("engine checkout failed")

    with pytest.raises(RuntimeError, match="engine checkout failed"):
        asyncio.run(run_recorded(run_case, record=_record_to(tmp_path), cleanup=lambda: cleaned.append("ok")))
    assert cleaned == ["ok"]


def test_a_clean_run_returns_its_result_and_artifact(tmp_path: Path) -> None:
    async def run_case() -> str:
        return "ok"

    result, path = asyncio.run(run_recorded(run_case, record=_record_to(tmp_path), cleanup=lambda: None))
    assert result == "ok" and path.read_text(encoding="utf-8") == "ok"
