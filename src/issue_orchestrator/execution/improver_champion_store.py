"""Where the improver's champion lives (#8001): ``<io-improver>/champion/``.

* ``state.json``: the champion and its promotions (:class:`ChampionState`);
* ``prompts/<sha256>.md``: every prompt a champion or challenger ran with,
  by digest, never changed;
* ``challenges/<id>.json``: each challenger's trial, written once.

The champion changes only through :meth:`promote`, which re-checks, under
the store's lock, that the challenge won against the champion that is
current NOW; the caller has checked the maintainer's approval (#7906).
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ..contracts.improver_tournament import require_slug
from ..contracts.improver_variant import ChallengeRecord, ChallengeRequest, ChampionState, ImproverVariant, Promotion
from ..domain.improver_champion import ChangeNotApplicable, live_limits, prompt_digest

CHAMPION_DIRNAME = "champion"


class ChampionUnavailable(RuntimeError):
    """No champion yet, or a write the store refuses."""


class FileChampionStore:
    def __init__(self, root: Path) -> None:
        self._root = root / CHAMPION_DIRNAME

    # -- reads ---------------------------------------------------------------

    def state(self) -> ChampionState:
        path = self._root / "state.json"
        if not path.is_file():
            raise ChampionUnavailable("no improver champion yet: seed one (improver_tournament champion seed)")
        return ChampionState.model_validate_json(path.read_text(encoding="utf-8"))

    def seeded(self) -> bool:
        return (self._root / "state.json").is_file()

    def prompt(self, digest: str) -> str:
        path = self._root / "prompts" / f"{digest}.md"
        if not path.is_file():
            raise ChampionUnavailable(f"no stored prompt {digest[:12]}")
        text = path.read_text(encoding="utf-8")
        if prompt_digest(text) != digest:
            raise ChampionUnavailable(f"stored prompt {digest[:12]} does not match its digest")
        return text

    def has_challenge(self, challenge_id: str) -> bool:
        return self._challenge_path(challenge_id).is_file()

    def challenge(self, challenge_id: str) -> ChallengeRecord:
        path = self._challenge_path(challenge_id)
        if not path.is_file():
            raise ChampionUnavailable(f"no challenge {challenge_id!r}")
        return ChallengeRecord.model_validate_json(path.read_text(encoding="utf-8"))

    # -- writes --------------------------------------------------------------

    def store_prompt(self, text: str) -> str:
        """Keep ``text`` by its digest (idempotent); the digest."""
        digest = prompt_digest(text)
        path = self._root / "prompts" / f"{digest}.md"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(path, text)
        return digest

    def seed(self, variant: ImproverVariant, prompt: str, *, at: datetime, by: str) -> ChampionState:
        """The first champion, once (one a live run can run)."""
        try:
            live_limits(variant)
        except ChangeNotApplicable as error:
            raise ChampionUnavailable(f"that champion cannot run live: {error}") from error
        if self.store_prompt(prompt) != variant.prompt_sha256:
            raise ChampionUnavailable("the seeded prompt does not match the variant's digest")
        with self._locked():
            if self.seeded():
                raise ChampionUnavailable("the champion is seeded already; it changes only by promotion")
            state = ChampionState(champion=variant, seeded_at=at, seeded_by=by)
            _write_atomic(self._root / "state.json", state.model_dump_json(indent=2) + "\n")
        return state

    def fix_challenge_request(self, request: ChallengeRequest) -> None:
        """Record what the challenge tries, once; a later call must ask for exactly the same."""
        path = self._root / "challenges" / f"{require_slug(request.challenge_id, 'a challenge id')}.request.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if path.exists():
                fixed = ChallengeRequest.model_validate_json(path.read_text(encoding="utf-8"))
                if fixed != request:
                    changed = sorted(k for k, v in request.model_dump().items() if fixed.model_dump()[k] != v)
                    raise ChampionUnavailable(
                        f"challenge {request.challenge_id} was asked for with other {changed}; a retry asks for"
                        " exactly what the first did"
                    )
                return
            _write_atomic(path, request.model_dump_json(indent=2) + "\n")

    def save_challenge(self, record: ChallengeRecord) -> None:
        """Record a challenge, once (a trial is evidence; it is never rewritten)."""
        path = self._challenge_path(record.challenge_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if path.exists():
                raise ChampionUnavailable(f"challenge {record.challenge_id!r} is recorded already")
            _write_atomic(path, record.model_dump_json(indent=2) + "\n")

    def promote(self, challenge: ChallengeRecord, *, at: datetime, approved_by: str) -> ChampionState:
        """Make the challenger the champion, if it won against the champion
        that is current at this moment."""
        if challenge.outcome != "won":
            raise ChampionUnavailable(f"challenge {challenge.challenge_id} did not win")
        self.prompt(challenge.challenger.prompt_sha256)  # its prompt is stored, and intact
        with self._locked():
            state = self.state()
            if state.champion != challenge.champion:
                raise ChampionUnavailable(
                    f"challenge {challenge.challenge_id} was tried against {challenge.champion.id};"
                    f" the champion is now {state.champion.id}"
                )
            promotion = Promotion(
                at=at, previous=state.champion, champion=challenge.challenger,
                challenge_id=challenge.challenge_id, issue=challenge.issue, approved_by=approved_by,
            )
            updated = state.model_copy(update={"champion": challenge.challenger,
                                                "promotions": (*state.promotions, promotion)})
            _write_atomic(self._root / "state.json", updated.model_dump_json(indent=2) + "\n")
        return updated

    def _challenge_path(self, challenge_id: str) -> Path:
        return self._root / "challenges" / f"{require_slug(challenge_id, 'a challenge id')}.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._root.mkdir(parents=True, exist_ok=True)
        with open(self._root / ".lock", "w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


def _write_atomic(path: Path, text: str) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(text)
    os.replace(temporary, path)


__all__ = ["CHAMPION_DIRNAME", "ChampionUnavailable", "FileChampionStore"]
