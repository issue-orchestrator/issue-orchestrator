"""Project attested coder validation into pair and run views for an exchange."""

import json
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path

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
        require_validation: bool,
    ) -> str | None:
        """Mirror authenticated validation bytes and enforce optional HEAD freshness."""
        try:
            evidence = self.intake.completion_evidence()
        except (CompletionIntakeError, OSError) as exc:
            self._clear()
            return f"completion receipt unavailable or validation failed: {exc}"
        self._replace_bytes(evidence.validation_bytes)
        if not require_validation:
            return None
        return self.current_validation_error()

    def replace_from_initial(self, source: Path | None) -> None:
        """Mirror the caller's current validation source at exchange start.

        A missing source clears any prior pair record. That is
        intentional: an exchange without current validation evidence
        must not inherit the last exchange's passing record.
        """
        self._replace_from(source)

    def current_validation_error(self) -> str | None:
        return _validation_record_error(
            self.record_path,
            current_head_sha=self.head_reader(self.coder_worktree_path),
        )

    def _replace_from(self, source: Path | None) -> None:
        if source is None or not source.exists():
            self._clear()
            return
        self._replace_bytes(source.read_bytes())

    def _replace_bytes(self, payload: bytes) -> None:
        self.pair_dir.mkdir(parents=True, exist_ok=True)
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
    require_validation: bool,
) -> str | None:
    """Compatibility entry point; the candidate filename is never authority."""
    return pair_validation.completion_error(
        require_validation=require_validation,
    )
