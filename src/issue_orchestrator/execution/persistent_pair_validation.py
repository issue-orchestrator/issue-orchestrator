"""Project attested coder validation into pair and run views for an exchange."""

import json
from dataclasses import dataclass, field
from collections.abc import Callable
from pathlib import Path

from ..domain.completion_intake import CompletionIntakeError
from ..domain.review_validation import ReviewValidationEvidence
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
    _authoritative_bytes: bytes | None = field(default=None, init=False, repr=False)

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
        return _validation_head_error(
            evidence.validation.head_sha,
            current_head_sha=self.head_reader(self.coder_worktree_path),
        )

    def replace_from_initial(self, evidence: ReviewValidationEvidence | None) -> None:
        """Project the caller's owner-held validation at exchange start.

        A missing source clears any prior pair record. That is
        intentional: an exchange without current validation evidence
        must not inherit the last exchange's passing record.
        """
        if evidence is None:
            self._clear()
            return
        self._replace_bytes(evidence.result_bytes)

    def current_validation_error(self) -> str | None:
        return _validation_bytes_error(
            self._authoritative_bytes,
            current_head_sha=self.head_reader(self.coder_worktree_path),
        )

    def _replace_bytes(self, payload: bytes) -> None:
        self._authoritative_bytes = payload
        self.pair_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(self.record_path, payload)
        if self.run_record_path is not None:
            _atomic_write_bytes(self.run_record_path, payload)

    def _clear(self) -> None:
        self._authoritative_bytes = None
        self.record_path.unlink(missing_ok=True)
        if self.run_record_path is not None:
            self.run_record_path.unlink(missing_ok=True)


def _validation_bytes_error(
    payload: bytes | None,
    *,
    current_head_sha: str | None,
) -> str | None:
    if payload is None:
        return "validation-record.json missing"
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "validation-record.json is not valid JSON"
    if not isinstance(data, dict):
        return "validation-record.json must be a JSON object"
    if data.get("passed") is not True:
        return "validation-record.json did not pass"
    record_head_sha = data.get("head_sha")
    if not isinstance(record_head_sha, str) or not record_head_sha:
        return "validation-record.json missing head_sha"
    return _validation_head_error(record_head_sha, current_head_sha=current_head_sha)


def _validation_head_error(
    record_head_sha: str,
    *,
    current_head_sha: str | None,
) -> str | None:
    if current_head_sha is None:
        return "cannot determine current HEAD for validation-record.json"
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
