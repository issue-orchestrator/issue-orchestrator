"""Project attested coder validation into pair and run views for an exchange."""

import json
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..domain.completion_intake import CompletionIntakeError
from ..ports.completion_intake import CompletionExchangeIntake
from ..infra.atomic_io import atomic_write_bytes as _atomic_write_bytes


@dataclass
class PairValidationMirror:
    """Own the pair-scoped validation record's freshness contract.

    The persistent pair owns pair-scoped validation evidence, but validation is
    only valid for the coder worktree's current HEAD. This mirror is the
    single owner for invalidating stale pair records, copying the
    current validation owner's record into pair scope, and asserting
    that a required validation record both passed and matches HEAD.
    """

    pair_dir: Path
    record_path: Path
    coder_worktree_path: Path
    intake: CompletionExchangeIntake
    head_reader: Callable[[Path], str | None]
    run_record_path: Path | None = None

    def completion_error(
        self,
        *,
        run_validation_record_path: Path,
        require_validation: bool,
    ) -> str | None:
        """Read attested intent, refresh its view, and enforce optional HEAD freshness."""
        try:
            record = self.intake.completion_record()
        except (CompletionIntakeError, OSError) as exc:
            return f"completion receipt unavailable or validation failed: {exc}"
        validation_source_error = self.refresh_from_completion(
            record.to_dict(),
            run_validation_record_path=run_validation_record_path,
        )
        if not require_validation:
            return None
        # All source errors below are non-empty. Preserve the first failure and
        # inspect HEAD only after the source was copied successfully.
        return validation_source_error or self.current_validation_error()

    def replace_from_initial(self, source: Path | None) -> None:
        """Mirror the caller's current validation source at exchange start.

        A missing source clears any prior pair record. That is
        intentional: an exchange without current validation evidence
        must not inherit the last exchange's passing record.
        """
        self._replace_from(source)

    def refresh_from_completion(
        self,
        payload: dict[str, Any],
        *,
        run_validation_record_path: Path,
    ) -> str | None:
        """Mirror validation evidence produced by this coder turn."""
        source, error = self._completion_validation_source(
            payload,
            run_validation_record_path=run_validation_record_path,
        )
        if error is not None:
            self._clear()
            return error
        self._replace_from(source)
        return None

    def current_validation_error(self) -> str | None:
        return _validation_record_error(
            self.record_path,
            current_head_sha=self.head_reader(self.coder_worktree_path),
        )

    def _completion_validation_source(
        self,
        payload: dict[str, Any],
        *,
        run_validation_record_path: Path,
    ) -> tuple[Path | None, str | None]:
        raw_path = payload.get("validation_record_path")
        if raw_path is not None:
            if not isinstance(raw_path, str) or not raw_path.strip():
                return (
                    None,
                    "completion validation_record_path must be a non-empty string",
                )
            return self._validated_worktree_path(raw_path)
        if run_validation_record_path.exists():
            return run_validation_record_path, None
        return None, None

    def _validated_worktree_path(self, raw_path: str) -> tuple[Path | None, str | None]:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.coder_worktree_path / candidate
        try:
            resolved = candidate.resolve()
            worktree = self.coder_worktree_path.resolve()
            if resolved != self.record_path.resolve():
                resolved.relative_to(worktree)
        except (OSError, ValueError):
            return None, (
                "completion validation_record_path must stay under the coder worktree"
            )
        if not resolved.exists():
            return None, f"completion validation_record_path does not exist: {resolved}"
        if not resolved.is_file():
            return None, f"completion validation_record_path is not a file: {resolved}"
        return resolved, None

    def _replace_from(self, source: Path | None) -> None:
        if source is None or not source.exists():
            self._clear()
            return
        self.pair_dir.mkdir(parents=True, exist_ok=True)
        payload = source.read_bytes()
        _atomic_write_bytes(self.record_path, payload)
        if self.run_record_path is not None:
            _atomic_write_bytes(self.run_record_path, payload)

    def _clear(self) -> None:
        self.record_path.unlink(missing_ok=True)
        if self.run_record_path is not None:
            self.run_record_path.unlink(missing_ok=True)


def _validation_record_error(
    record_path: Path,
    *,
    current_head_sha: str | None,
) -> str | None:
    if not record_path.exists():
        return "validation-record.json missing"
    try:
        data = json.loads(record_path.read_text())
    except json.JSONDecodeError:
        return "validation-record.json is not valid JSON"
    if not isinstance(data, dict):
        return "validation-record.json must be a JSON object"
    if data.get("passed") is not True:
        return "validation-record.json did not pass"
    if current_head_sha is None:
        return "cannot determine current HEAD for validation-record.json"
    record_head_sha = data.get("head_sha")
    if not isinstance(record_head_sha, str) or not record_head_sha:
        return "validation-record.json missing head_sha"
    if record_head_sha != current_head_sha:
        return (
            "validation-record.json head "
            f"{record_head_sha[:12]} does not match current HEAD "
            f"{current_head_sha[:12]}"
        )
    return None


def validate_coder_completion(
    *,
    completion_path: Path,
    pair_validation: PairValidationMirror,
    run_validation_record_path: Path,
    require_validation: bool,
) -> str | None:
    """Compatibility entry point; the candidate filename is never authority."""
    return pair_validation.completion_error(
        run_validation_record_path=run_validation_record_path,
        require_validation=require_validation,
    )
