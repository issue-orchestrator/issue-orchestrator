"""The productized improver tournament: snapshots, keys, arms, graders (#8001)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.domain.test_improver_tournament import SEALED
from tests.unit.improver_support import build_improver_data, example

from issue_orchestrator.contracts.improver_run import (
    ImproverAgentChoice,
    ImproverProvider,
)
from issue_orchestrator.contracts.improver_tournament import (
    AnswerKeyItem,
    TournamentArm,
)
from issue_orchestrator.entrypoints.improver_run import HeatPlan
from issue_orchestrator.entrypoints.improver_staging import load_staged_evidence
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.improver_answer_keys import (
    AnswerKeyError,
    FileAnswerKeyStore,
)
from issue_orchestrator.execution.improver_snapshots import (
    FrozenSnapshotStore,
    SnapshotUnavailable,
)
from issue_orchestrator.execution.improver_tournament import (
    ArmOutput,
    ArmSpec,
    Grader,
    TournamentHarness,
)
from issue_orchestrator.ports.improver import HeatSpace, ImproverAgentResult

T0 = datetime(2026, 10, 4, 7, 36, tzinfo=UTC)


def _legacy_inputs(root: Path) -> Path:
    """A staged bundle as io wrote it before #8001's operator hand actions."""
    data = build_improver_data(root)
    (data / "interventions.json").write_text(json.dumps({
        "window_from": "2026-09-27T18:00:00Z", "window_to": "2026-09-28T18:00:00Z", "complete": False,
        "derived_from": ["charter ledger: proposal approvals and declines"],
        "not_derivable": ["needs-human or other labels removed by a human on GitHub"],
        "interventions": [
            {"at": "2026-09-28T16:00:00Z", "kind": "proposal_approved", "subject": "#900", "detail": "kill (d1)"},
            {"at": "2026-09-28T16:30:00Z", "kind": "operator_pause", "subject": "engine", "detail": "cli: operator"},
        ],
    }))
    return data


def _engine_files(root: Path) -> tuple[Path, Path]:
    state = root / "state"
    (state / "logs").mkdir(parents=True)
    with sqlite3.connect(state / "timeline.sqlite") as conn:
        conn.execute("CREATE TABLE timeline (event TEXT)")
        conn.execute("INSERT INTO timeline VALUES ('review.skipped')")
    (state / "logs" / "orchestrator.log").write_text("2026-10-04 02:00:01 INFO auth_expired\n")
    clone = root / "clone"
    clone.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    (clone / "README.md").write_text("the audited repository")
    when = (T0 - timedelta(days=1)).isoformat()
    env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when, "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "README.md"], cwd=clone, check=True, env=env)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"], cwd=clone, check=True, env=env)
    return state, clone


@pytest.fixture
def stores(tmp_path: Path) -> tuple[FrozenSnapshotStore, FileAnswerKeyStore, Path]:
    root = tmp_path / "io-improver"
    snapshots = FrozenSnapshotStore(root, LocalCommandRunner())
    state, clone = _engine_files(tmp_path)
    snapshots.import_("20261004", improver_data=_legacy_inputs(tmp_path / "src"), taken_at=T0,
                      origin="test", state_dir=state, clone=clone)
    keys = FileAnswerKeyStore(root)
    keys.seed_sealed("20261004", SEALED, sealed_at=T0, added_by="coordinator")
    return snapshots, keys, root


# -- frozen snapshots ----------------------------------------------------------


def test_an_old_staged_bundle_is_frozen_upgraded_and_loads_with_todays_contracts(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snapshots, _, root = stores

    snapshot = snapshots.get("20261004")

    assert snapshot.has_toolbox and snapshot.taken_at == T0 and snapshot.audited_repo == "porchpin/porchpin"
    assert snapshot.upgrades and "interventions.json" in snapshot.upgrades[0]
    data = root / "snapshots" / "20261004" / "improver-data"
    interventions = json.loads((data / "interventions.json").read_text())
    assert [(i["source"], i["attribution"]) for i in interventions["interventions"]] == [
        ("charter_ledger", "operator_surface"), ("pause_journal", "operator_surface"),
    ]
    assert interventions["github"]["read"] is False
    load_staged_evidence(data)
    toolbox = root / "snapshots" / "20261004" / "toolbox"
    assert (toolbox / "state" / "timeline.sqlite").is_file() and (toolbox / "logs" / "orchestrator.log").is_file()
    assert (toolbox / "repo" / ".git").is_dir()


def test_a_snapshot_is_never_changed_or_half_made(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snapshots, _, root = stores
    with pytest.raises(SnapshotUnavailable, match="never changed"):
        snapshots.import_("20261004", improver_data=_legacy_inputs(tmp_path / "again"), taken_at=T0, origin="x")
    state, _ = _engine_files(tmp_path / "half")
    with pytest.raises(SnapshotUnavailable, match="needs both"):
        snapshots.import_("half", improver_data=_legacy_inputs(tmp_path / "h"), taken_at=T0, origin="x", state_dir=state)
    broken = _legacy_inputs(tmp_path / "broken")
    (broken / "audit.json").write_text("{}")
    with pytest.raises(SnapshotUnavailable, match="do not load"):
        snapshots.import_("broken", improver_data=broken, taken_at=T0, origin="x")
    assert snapshots.ids() == ("20261004",) and not (root / "snapshots" / "broken").exists()


def test_a_snapshot_never_links_outside_itself(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A link out of a snapshot would show later evidence: a clone whose
    .git is a live repository's, or a staged file pointing at a live one."""
    snapshots, _, root = stores
    live = tmp_path / "live"
    live.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=live, check=True)
    data = _legacy_inputs(tmp_path / "b")
    (data / "later.md").symlink_to(live / "README")
    with pytest.raises(SnapshotUnavailable, match="later.md links outside the snapshot"):
        snapshots.import_("leaky", improver_data=data, taken_at=T0, origin="x")
    escaping = _legacy_inputs(tmp_path / "c")
    (escaping / "up.md").symlink_to("../../outside.md")
    with pytest.raises(SnapshotUnavailable, match="up.md links outside"):
        snapshots.import_("escaping", improver_data=escaping, taken_at=T0, origin="x")
    assert snapshots.ids() == ("20261004",)
    assert not any((root / "snapshots" / name).exists() for name in ("leaky", "escaping"))
    # A link that stays inside (a bundle's CLAUDE.md -> AGENTS.md) is fine.
    inside = _legacy_inputs(tmp_path / "d")
    (inside / "AGENTS.md").write_text("agents")
    (inside / "CLAUDE.md").symlink_to("AGENTS.md")
    snapshots.import_("inside", improver_data=inside, taken_at=T0, origin="x")


def test_a_clone_that_borrows_objects_is_frozen_owning_them(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """``git clone --shared`` borrows the live repository's objects; a
    snapshot must own them, so it keeps working whatever the live one does."""
    snapshots, _, root = stores
    live = tmp_path / "live"
    live.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    before = {"GIT_AUTHOR_DATE": (T0 - timedelta(hours=1)).isoformat(),
              "GIT_COMMITTER_DATE": (T0 - timedelta(hours=1)).isoformat(), "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q"], cwd=live, check=True)
    (live / "f").write_text("one")
    subprocess.run([*git, "add", "f"], cwd=live, check=True)
    subprocess.run([*git, "commit", "-qm", "one"], cwd=live, check=True, env=before)
    shared = tmp_path / "shared"
    subprocess.run(["git", "clone", "-q", "--shared", str(live), str(shared)], check=True)
    assert (shared / ".git" / "objects" / "info" / "alternates").exists()
    state, _ = _engine_files(tmp_path / "e")

    snapshots.import_("shared", improver_data=_legacy_inputs(tmp_path / "a"), taken_at=T0, origin="x",
                      state_dir=state, clone=shared)

    frozen = root / "snapshots" / "shared" / "toolbox" / "repo"
    assert not (frozen / ".git" / "objects" / "info" / "alternates").exists()
    shutil.rmtree(live)
    shown = subprocess.run(["git", "-C", str(frozen), "show", "HEAD:f"], capture_output=True, text=True, check=True)
    assert shown.stdout == "one"


def test_a_clone_showing_commits_after_the_snapshot_is_refused(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A clone taken later than the staged inputs would show the arms what
    was done since; and what no ref reaches (a dropped commit) is not kept."""
    snapshots, _, root = stores
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    def commit(message: str, when: datetime, *, ref: str | None = None) -> str:
        env = {"GIT_AUTHOR_DATE": when.isoformat(), "GIT_COMMITTER_DATE": when.isoformat(), "PATH": "/usr/bin:/bin"}
        (repo / "f").write_text(message)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "add", "f"], cwd=repo, check=True, env=env)
        subprocess.run([*git, "commit", "-qm", message], cwd=repo, check=True, env=env)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    commit("before", T0 - timedelta(hours=1))
    dropped = commit("after, then dropped", T0 + timedelta(days=2))
    subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=repo, check=True)
    state, _ = _engine_files(tmp_path / "e")

    snapshots.import_("in-time", improver_data=_legacy_inputs(tmp_path / "a"), taken_at=T0, origin="x",
                      state_dir=state, clone=repo)
    frozen = root / "snapshots" / "in-time" / "toolbox" / "repo"
    shown = subprocess.run(["git", "-C", str(frozen), "cat-file", "-e", dropped], capture_output=True)
    assert shown.returncode != 0, "a commit no ref reaches was kept in the snapshot"

    later = commit("after", T0 + timedelta(days=3))
    with pytest.raises(SnapshotUnavailable, match=r"reaches 1 commit\(s\) made after the snapshot.*" + later[:12]):
        snapshots.import_("late", improver_data=_legacy_inputs(tmp_path / "b"), taken_at=T0, origin="x",
                          state_dir=state, clone=repo)
    assert "late" not in snapshots.ids()


def test_a_frozen_clone_shows_its_commit_and_nothing_written_since(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snapshots, _, root = stores
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_DATE": (T0 - timedelta(hours=1)).isoformat(),
           "GIT_COMMITTER_DATE": (T0 - timedelta(hours=1)).isoformat(), "PATH": "/usr/bin:/bin"}
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "tracked.md").write_text("as committed")
    (repo / ".gitignore").write_text("ignored.log\n")
    subprocess.run([*git, "add", "."], cwd=repo, check=True, env=env)
    subprocess.run([*git, "commit", "-qm", "c"], cwd=repo, check=True, env=env)
    (repo / "tracked.md").write_text("edited after the snapshot")
    (repo / "untracked.md").write_text("written after the snapshot")
    (repo / "ignored.log").write_text("ignored, later")
    state, _ = _engine_files(tmp_path / "e")

    snapshots.import_("worktree", improver_data=_legacy_inputs(tmp_path / "a"), taken_at=T0, origin="x",
                      state_dir=state, clone=repo)

    frozen = root / "snapshots" / "worktree" / "toolbox" / "repo"
    assert (frozen / "tracked.md").read_text() == "as committed"
    assert sorted(p.name for p in frozen.iterdir()) == [".git", ".gitignore", "tracked.md"]
    status = subprocess.run(["git", "-C", str(frozen), "status", "--porcelain", "--ignored"],
                            capture_output=True, text=True, check=True)
    assert status.stdout == ""


def test_a_committed_link_out_restored_by_the_rebuild_is_refused(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The checkout is rebuilt from the commit, so a link the commit holds
    (but the working tree had deleted) comes back: it is checked again."""
    snapshots, _, root = stores
    repo = tmp_path / "repo"
    repo.mkdir()
    when = (T0 - timedelta(hours=1)).isoformat()
    env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when, "PATH": "/usr/bin:/bin"}
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "live.md").symlink_to(tmp_path / "outside.md")
    subprocess.run([*git, "add", "live.md"], cwd=repo, check=True, env=env)
    subprocess.run([*git, "commit", "-qm", "c"], cwd=repo, check=True, env=env)
    (repo / "live.md").unlink()
    state, _ = _engine_files(tmp_path / "e")

    with pytest.raises(SnapshotUnavailable, match="live.md links outside the snapshot"):
        snapshots.import_("relinked", improver_data=_legacy_inputs(tmp_path / "a"), taken_at=T0, origin="x",
                          state_dir=state, clone=repo)
    assert not (root / "snapshots" / "relinked").exists()


def test_no_arm_runs_before_its_snapshot_has_a_key(tmp_path: Path) -> None:
    """A key is sealed before any result is seen: arms run without one could
    be read, and the key written to fit them."""
    root = tmp_path / "io-improver"
    snapshots = FrozenSnapshotStore(root, LocalCommandRunner())
    snapshots.import_("keyless", improver_data=_legacy_inputs(tmp_path / "src"), taken_at=T0, origin="x")
    agents = Agents({"m": "{}"}, {})
    harness = _harness(root, (snapshots, FileAnswerKeyStore(root), root), agents)
    arm = TournamentArm(name="A", provider="claude", model="m", mode="scripted")

    with pytest.raises(AnswerKeyError, match="no answer key"):
        harness.run_arms("t8", "keyless", [_spec(arm)])
    assert agents.spaces == [] and agents.timeouts == {}
    assert not harness.directory("t8").exists()


def test_a_snapshot_is_published_whole_or_not_at_all(stores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    import issue_orchestrator.execution.improver_snapshots as module

    snapshots, _, root = stores
    real_rename = module.os.rename

    def failing(source: str, target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "rename", failing)
    with pytest.raises(OSError, match="disk full"):
        snapshots.import_("torn", improver_data=_legacy_inputs(tmp_path / "a"), taken_at=T0, origin="x")
    assert not (root / "snapshots" / "torn").exists() and "torn" not in snapshots.ids()
    assert list((root / "snapshots" / ".staging").iterdir()) == []

    # An import killed mid-way leaves only a staging directory: never listed,
    # and the id is still free.
    abandoned = root / "snapshots" / ".staging" / "torn-killed"
    (abandoned / "improver-data").mkdir(parents=True)
    (abandoned / "snapshot.json").write_text("{")
    monkeypatch.setattr(module.os, "rename", real_rename)
    assert "torn" not in snapshots.ids()
    snapshots.import_("torn", improver_data=_legacy_inputs(tmp_path / "b"), taken_at=T0, origin="x")
    assert snapshots.get("torn").id == "torn" and snapshots.ids() == ("20261004", "torn")


def test_a_frozen_clone_takes_nothing_from_its_sources_git_directory(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A clone of io's own checkout holds io's improver store (its answer
    keys) in .git; a symlinked .git reads a live repository. The frozen
    clone is a new repository with only the refs' history."""
    snapshots, _, root = stores
    _, live = _engine_files(tmp_path / "live")
    keys = live / ".git" / "io-improver" / "keys"
    keys.mkdir(parents=True)
    (keys / "20261004.json").write_text("THE ANSWER KEY")
    (live / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\ntouch HOOK-RAN\n")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").symlink_to(live / ".git")
    state, _ = _engine_files(tmp_path / "e")

    snapshots.import_("rebuilt", improver_data=_legacy_inputs(tmp_path / "a"), taken_at=T0, origin="x",
                      state_dir=state, clone=linked)

    frozen = root / "snapshots" / "rebuilt" / "toolbox" / "repo"
    assert (frozen / ".git").is_dir() and not (frozen / ".git").is_symlink()
    everything = [str(p.relative_to(frozen)) for p in frozen.rglob("*")]
    assert not any("io-improver" in p for p in everything)
    assert not any(p.read_bytes().find(b"THE ANSWER KEY") >= 0 for p in frozen.rglob("*") if p.is_file())
    assert not (frozen / ".git" / "hooks" / "post-checkout").exists()
    assert (frozen / "README.md").read_text() == "the audited repository"


def test_a_frozen_store_holds_the_rows_its_write_ahead_log_had(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The toolbox reads a store copy immutable (no log): a row committed
    to the log but not yet checkpointed must be in the frozen copy itself."""
    snapshots, _, root = stores
    state, clone = _engine_files(tmp_path / "e")
    writer = sqlite3.connect(state / "events.sqlite")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE events (name TEXT)")
    writer.execute("INSERT INTO events VALUES ('only-in-the-log')")
    writer.commit()
    try:
        assert (state / "events.sqlite-wal").stat().st_size > 0
        snapshots.import_("wal", improver_data=_legacy_inputs(tmp_path / "a"), taken_at=T0, origin="x",
                          state_dir=state, clone=clone)
    finally:
        writer.close()

    frozen = root / "snapshots" / "wal" / "toolbox" / "state" / "events.sqlite"
    with closing(sqlite3.connect(f"{frozen.as_uri()}?mode=ro&immutable=1", uri=True)) as conn:
        assert conn.execute("SELECT name FROM events").fetchall() == [("only-in-the-log",)]


def test_a_bundle_reached_through_a_link_is_refused_and_left_untouched(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Copied, a link would stay a reference: later edits to the bundle
    would change the frozen evidence (and the upgrade would edit the source)."""
    snapshots, _, root = stores
    real = _legacy_inputs(tmp_path / "real")
    before = (real / "interventions.json").read_text()
    link = tmp_path / "bundle-link"
    link.symlink_to(real)

    with pytest.raises(SnapshotUnavailable, match="is a symlink"):
        snapshots.import_("linked-bundle", improver_data=link, taken_at=T0, origin="x")
    assert (real / "interventions.json").read_text() == before
    assert "linked-bundle" not in snapshots.ids() and not (root / "snapshots" / "linked-bundle").exists()


def test_a_scripted_only_snapshot_has_no_toolbox_to_copy(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snapshots, _, _ = stores
    snapshots.import_("bundle-only", improver_data=_legacy_inputs(tmp_path / "b"), taken_at=T0, origin="x")
    run = tmp_path / "run"
    run.mkdir()

    snapshots.copy_inputs("bundle-only", run)
    with pytest.raises(SnapshotUnavailable, match="scripted arms only"):
        snapshots.copy_toolbox("bundle-only", run)


def test_a_clone_with_no_commit_is_refused(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snapshots, _, _ = stores
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=empty, check=True)
    state, _ = _engine_files(tmp_path / "e")
    with pytest.raises(SnapshotUnavailable, match="has no commit to freeze"):
        snapshots.import_("empty", improver_data=_legacy_inputs(tmp_path / "a"), taken_at=T0, origin="x",
                          state_dir=state, clone=empty)


# -- answer keys ---------------------------------------------------------------


def _hindsight(item_id: str = "H-7999", *, by: str = "coordinator", confirmed: bool = True) -> AnswerKeyItem:
    return AnswerKeyItem(
        id=item_id, weight=2, title="Review admitted onto a PR whose rework is live", description="d",
        category="stall", source="hindsight", status="confirmed" if confirmed else "candidate",
        links=("issue-orchestrator/issue-orchestrator#7999",), filed_at=T0.date(), added_at=T0, added_by=by,
    )


def test_hindsight_items_join_the_sealed_key_and_only_confirmed_ones_score(stores) -> None:  # type: ignore[no-untyped-def]
    _, keys, _ = stores

    keys.add(_hindsight("H-7999"), snapshot_id="20261004")
    key = keys.add(_hindsight("H-8137", confirmed=False), snapshot_id="20261004")

    assert [i.id for i in key.scored] == ["1", "2", "9", "H-7999"] and key.max_score == 10
    assert keys.confirm("20261004", "H-8137", by="operator").max_score == 12


@pytest.mark.parametrize("by", ["improver", "Improver", " the improver "])
def test_the_improver_never_writes_its_own_key(stores, by: str) -> None:  # type: ignore[no-untyped-def]
    _, keys, _ = stores
    with pytest.raises(AnswerKeyError, match="never writes its own"):
        keys.add(_hindsight(by=by), snapshot_id="20261004")
    with pytest.raises(AnswerKeyError, match="never writes its own"):
        keys.confirm("20261004", "1", by=by)


def test_a_key_is_seeded_once_and_an_item_added_once(stores) -> None:  # type: ignore[no-untyped-def]
    _, keys, _ = stores
    with pytest.raises(AnswerKeyError, match="already has a key"):
        keys.seed_sealed("20261004", SEALED, sealed_at=T0, added_by="coordinator")
    keys.add(_hindsight(), snapshot_id="20261004")
    with pytest.raises(AnswerKeyError, match="exists"):
        keys.add(_hindsight(), snapshot_id="20261004")
    with pytest.raises(AnswerKeyError, match="only hindsight"):
        keys.add(_hindsight().model_copy(update={"id": "H-x", "source": "sealed_key"}), snapshot_id="20261004")


def test_two_seeds_at_once_publish_exactly_one_key(tmp_path: Path) -> None:
    """A sealed key is never replaced, even by a seed racing it."""
    keys = FileAnswerKeyStore(tmp_path / "io-improver")
    other = SEALED.replace("#364 ruling", "#365 ruling")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def seed(markdown: str, name: str) -> None:
        barrier.wait()
        try:
            keys.seed_sealed("race", markdown, sealed_at=T0, added_by=name)
            outcomes.append(name)
        except AnswerKeyError:
            outcomes.append("refused")

    threads = [threading.Thread(target=seed, args=(SEALED, "one")), threading.Thread(target=seed, args=(other, "two"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) in (["one", "refused"], ["refused", "two"])
    winner = next(o for o in outcomes if o != "refused")
    assert all(i.added_by == winner for i in keys.get("race").items)


def test_hindsight_items_added_at_once_are_all_kept(stores) -> None:  # type: ignore[no-untyped-def]
    _, keys, _ = stores
    threads = [threading.Thread(target=keys.add, args=(_hindsight(f"H-{n}"),), kwargs={"snapshot_id": "20261004"})
               for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert {i.id for i in keys.get("20261004").items} >= {f"H-{n}" for n in range(8)}


def test_no_improver_run_module_can_reach_the_answer_keys() -> None:
    """The improver never writes its own key: nothing an improver run is
    built from imports the key store (only the tournament's key commands do)."""
    src = Path(__file__).resolve().parents[3] / "src" / "issue_orchestrator"
    run_path = [
        "entrypoints/improver_run.py", "entrypoints/improver_staging.py", "entrypoints/cli_tools/improver.py",
        "execution/improver_agents.py", "execution/claude_improver_agent.py", "execution/codex_improver_agent.py",
        "execution/improver_investigation.py", "execution/improver_toolbox.py", "execution/improver_effect_applier.py",
    ]
    for module in run_path:
        assert "improver_answer_keys" not in (src / module).read_text(), module


# -- the harness -----------------------------------------------------------------


class Agents:
    """Fake agents: arms answer by arm, graders grade every output."""

    def __init__(self, arm_answers: dict[str, str | None], grader_answers: dict[str, object]) -> None:
        self.arm_answers = arm_answers
        self.grader_answers = grader_answers
        self.spaces: list[tuple[str, HeatSpace, object]] = []
        self.timeouts: dict[str, list[int]] = {}
        self._lock = threading.Lock()

    def agent_for(self, choice: ImproverAgentChoice, minutes: int):  # type: ignore[no-untyped-def]
        agents = self
        self.timeouts.setdefault(choice.model, []).append(minutes)

        class Agent:
            def __init__(self) -> None:
                self.choice = choice

            def run(self, *, prompt: str, space: HeatSpace, toolbox: object) -> ImproverAgentResult:
                with agents._lock:
                    agents.spaces.append((prompt, space, toolbox))
                if "Improver tournament grader" in prompt:
                    answer = agents.grader_answers[choice.provider.value]
                    labels = sorted(p.stem for p in (space.run_dir / "anon").glob("*.json"))
                    text = answer(labels) if callable(answer) else answer
                    return ImproverAgentResult(text, "graded")  # type: ignore[arg-type]
                arm = choice.model
                return ImproverAgentResult(agents.arm_answers[arm], "done" if agents.arm_answers[arm] else "timed out")

        return Agent()


ADDENDUM = "<<AUDITED_REPO>> budget <<BUDGET_MINUTES>> minutes <<STAGED_AT>>"


def _spec(arm: TournamentArm, *, prompt: str = "THE IMPROVER PROMPT", heats: HeatPlan = HeatPlan(2, 2),
          budget: int = 30, timeout: int = 40) -> ArmSpec:
    return ArmSpec(arm=arm, prompt=prompt, empowered_addendum=ADDENDUM, heats=heats, budget_minutes=budget,
                   agent_timeout_minutes=timeout)


def _harness(root: Path, stores, agents: Agents) -> TournamentHarness:  # type: ignore[no-untyped-def]
    snapshots, keys, _ = stores
    return TournamentHarness(
        root=root, snapshots=snapshots, keys=keys, agent_for=agents.agent_for,
        grader_prompt=(Path(__file__).resolve().parents[3] / "examples" / "prompts" / "improver-grader.md").read_text(),
        clock=lambda: T0 + timedelta(days=3),
    )


def test_arms_run_on_the_frozen_snapshot_and_are_graded_blind_by_every_grader(stores) -> None:  # type: ignore[no-untyped-def]
    _, _, root = stores
    good, poor = json.dumps(example("exam_case")), "not even json"
    agents = Agents({"good-model": good, "poor-model": poor, "dead-model": None}, {})

    def grade_from_text(labels: list[str]) -> str:
        out = {}
        for label in labels:
            text = (root / "tournaments").glob(f"*/anon/{label}.json")
            body = next(text).read_text()
            grade = "full" if "refused-validation" in body else "miss"
            out[label] = {"items": {i: {"grade": grade, "why": "q"} for i in ("1", "2", "9")},
                          "unsupported": 0, "unsupported_ids": [], "extras": []}
        return json.dumps(out)

    agents.grader_answers = {"claude": grade_from_text, "codex": grade_from_text}
    harness = _harness(root, stores, agents)
    arms = [
        TournamentArm(name="G", provider="claude", model="good-model", mode="empowered"),
        TournamentArm(name="P", provider="codex", model="poor-model", mode="scripted"),
        TournamentArm(name="X", provider="codex", model="dead-model", mode="scripted"),
    ]

    outputs = harness.run_arms("t1", "20261004", [_spec(arm) for arm in arms])
    result = harness.grade("t1", "20261004", outputs, seed=11)

    assert {(o.arm, o.heat, o.text is not None) for o in outputs} == {
        # P's answers broke io's rules (rejected): like X's none, they are not graded.
        ("G", 1, True), ("G", 2, True), ("P", 1, False), ("P", 2, False), ("X", 1, False), ("X", 2, False),
    }
    # Each grader graded every output three times (the default passes), side by side.
    assert sorted(g.grading for g in result.graders) == [f"{g}#{n}" for g in ("claude", "codex") for n in (1, 2, 3)]
    assert all(g.accepted for g in result.graders) and result.passes == 3
    assert {a.arm: a.mean for a in result.arms} == {"G": 8.0, "P": 0.0, "X": 0.0}
    # The fake graders agree exactly: no noise, so G is told apart and P and X (both 0) are not.
    assert result.grading_sd == 0.0 and result.ranking == (("G",), ("P", "X"))
    assert result.distinguishable == (("G", "P"), ("G", "X"))
    assert result.cost.arm_heats == {"claude": 2, "codex": 4}
    assert result.cost.grader_calls == {"claude": 3, "codex": 3}
    directory = harness.directory("t1").resolve()
    # Graders read only the anonymized outputs and the key; never the
    # sealed mapping, the arms' run dirs or the key store.
    grader_spaces = [s for p, s, _ in agents.spaces if "Improver tournament grader" in p]
    assert {s.evidence for s in grader_spaces} == {(directory / "anon", directory / "key")}
    mapping = json.loads((directory / "sealed" / "mapping.json").read_text())
    assert mapping["seed"] == 11 and {v["arm"] for v in mapping["labels"].values()} == {"G"}
    assert not any(str(directory) in p.read_text() for p in (directory / "anon").glob("*.json"))
    # Arms read their frozen inputs (and the empowered one its toolbox), never the key.
    arm_spaces = [(s, t) for p, s, t in agents.spaces if "THE IMPROVER PROMPT" in p]
    assert all(not any("keys" in str(e) or "sealed" in str(e) for e in s.evidence) for s, _ in arm_spaces)
    assert any(any(e.name == "toolbox" for e in s.evidence) and t is not None for s, t in arm_spaces)
    assert json.loads((directory / "result.json").read_text())["ranking"] == [["G"], ["P", "X"]]


def _complete(labels: list[str]) -> str:
    return json.dumps({label: {"items": {i: {"grade": "half", "why": "q"} for i in ("1", "2", "9")},
                               "unsupported": 0} for label in labels})


def test_a_tournament_one_grader_could_not_grade_has_no_result(stores) -> None:  # type: ignore[no-untyped-def]
    """Cross-model means every grader: one model's judgment never ranks the arms alone."""
    _, _, root = stores

    def partial(labels: list[str]) -> str:
        return json.dumps({labels[0]: {}})

    harness = _harness(root, stores, Agents({}, {"claude": _complete, "codex": partial}))

    with pytest.raises(RuntimeError, match=r"not every grading was complete.*codex#1: rejected"):
        harness.grade("t2", "20261004", [ArmOutput("A", 1, "{}"), ArmOutput("B", 1, "{}")], seed=1)
    assert not (harness.directory("t2") / "result.json").exists()
    # What each grading answered stays for a person to read.
    assert (harness.directory("t2") / "graders" / "codex" / "p3" / "grades.json").is_file()


def test_arms_are_ranked_on_their_exact_means_not_the_rounded_ones(stores) -> None:  # type: ignore[no-untyped-def]
    """Noise-free, 1.0004 and 0.9996 are told apart; rounded, both show 1.0."""
    from issue_orchestrator.domain.improver_tournament import pool
    from issue_orchestrator.execution.improver_tournament import _result

    key = stores[1].get("20261004")
    gradings = {"g#1": {"S10": 1.0004, "S11": 0.9996}}
    arm_of = {"S10": "A", "S11": "B"}
    result = _result("t", "20261004", key, (), 1, pool(gradings, arm_of, ungraded={}), gradings, arm_of, {}, {})

    assert result.ranking == (("A",), ("B",))
    assert [a.mean for a in result.arms] == [1.0, 1.0]


def test_a_challenger_and_its_champion_run_together_each_as_specified(stores) -> None:  # type: ignore[no-untyped-def]
    _, _, root = stores
    agents = Agents({"m": json.dumps(example("exam_case"))}, {})
    harness = _harness(root, stores, agents)
    arm = TournamentArm(name="champion", provider="claude", model="m", mode="empowered")

    outputs = harness.run_arms("t5", "20261004", [
        _spec(arm, prompt="CHAMPION PROMPT", heats=HeatPlan(1, 1), budget=30, timeout=40),
        _spec(arm.model_copy(update={"name": "challenger"}), prompt="CHALLENGER PROMPT", heats=HeatPlan(3, 2),
              budget=45, timeout=55),
    ])

    seen = sorted(
        ("CHALLENGER" if "CHALLENGER PROMPT" in p else "CHAMPION" if "CHAMPION PROMPT" in p else "?", space.heat,
         "budget 45 minutes" in p)
        for p, space, _ in agents.spaces
    )
    assert seen == [("CHALLENGER", 1, True), ("CHALLENGER", 2, True), ("CHALLENGER", 3, True), ("CHAMPION", 1, False)]
    assert [(o.arm, o.heat) for o in outputs] == [("champion", 1), ("challenger", 1), ("challenger", 2), ("challenger", 3)]
    assert agents.timeouts["m"] == [40, 55]
    with pytest.raises(ValueError, match="arm names repeat"):
        harness.run_arms("t6", "20261004", [_spec(arm), _spec(arm)])


def test_an_empowered_arm_is_stopped_only_after_its_budget() -> None:
    arm = TournamentArm(name="e", provider="claude", model="m", mode="empowered")
    with pytest.raises(ValueError, match="60-minute budget must be below its 60-minute agent timeout"):
        _spec(arm, budget=60, timeout=60)
    _spec(arm.model_copy(update={"mode": "scripted"}), budget=60, timeout=60)


@pytest.mark.parametrize(
    ("graders", "why"),
    [
        ([Grader("only", ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE))], "at least two providers"),
        ([Grader("a", ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE)),
          Grader("b", ImproverAgentChoice(provider=ImproverProvider.CLAUDE, model="sonnet"))], "at least two providers"),
        ([Grader("g", ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE)),
          Grader("g", ImproverAgentChoice.for_provider(ImproverProvider.CODEX))], "grader names repeat"),
    ],
)
def test_a_tournament_is_graded_by_at_least_two_providers_under_distinct_names(stores, graders, why) -> None:  # type: ignore[no-untyped-def]
    _, _, root = stores
    harness = _harness(root, stores, Agents({}, {"claude": _complete, "codex": _complete}))

    with pytest.raises(ValueError, match=why):
        harness.grade("t7", "20261004", [ArmOutput("A", 1, "{}")], graders=graders, seed=1)
    assert not harness.directory("t7").exists()


@pytest.mark.parametrize("name", ["../../../snapshots/20261004/improver-data", "a/b", "..", ""])
def test_no_name_reaches_outside_its_directory(stores, name: str) -> None:  # type: ignore[no-untyped-def]
    """A grader, snapshot or tournament name is one path component: a name
    like ../../snapshots/x would write into a frozen snapshot."""
    snapshots, keys, root = stores
    before = sorted(str(p) for p in (root / "snapshots").rglob("*"))
    harness = _harness(root, stores, Agents({}, {}))

    with pytest.raises(ValueError, match="one path component"):
        Grader(name, ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE))
    with pytest.raises(ValueError, match="one path component"):
        harness.directory(name)
    with pytest.raises(ValueError, match="one path component"):
        keys.path(name)
    with pytest.raises(ValueError, match="one path component"):
        snapshots.get(name)
    with pytest.raises(ValueError, match="one path component"):
        snapshots.import_(name, improver_data=root, taken_at=T0, origin="x")
    assert sorted(str(p) for p in (root / "snapshots").rglob("*")) == before


def _full_credit(labels: list[str]) -> str:
    return json.dumps({label: {"items": {i: {"grade": "full", "why": "q"} for i in ("1", "2", "9")},
                               "unsupported": 0} for label in labels})


def test_an_answer_io_rejected_scores_nothing_however_it_reads(stores) -> None:  # type: ignore[no-untyped-def]
    """A rejected answer files nothing, so it is worth nothing: graders that
    would give anything full credit never see it."""
    _, _, root = stores
    agents = Agents({"valid": json.dumps(example("exam_case")), "invalid": "item 1: the ruling never reaches review"},
                    {"claude": _full_credit, "codex": _full_credit})
    harness = _harness(root, stores, agents)
    arms = [TournamentArm(name="V", provider="claude", model="valid", mode="scripted"),
            TournamentArm(name="R", provider="codex", model="invalid", mode="scripted")]

    result = harness.grade("t9", "20261004", harness.run_arms("t9", "20261004", [_spec(a) for a in arms]), seed=3)

    assert {a.arm: a.mean for a in result.arms} == {"V": 8.0, "R": 0.0}
    assert result.ranking == (("V",), ("R",))


def test_a_failed_grading_is_retried_on_the_same_anonymized_outputs(stores) -> None:  # type: ignore[no-untyped-def]
    _, _, root = stores

    def partial(labels: list[str]) -> str:
        return json.dumps({labels[0]: {}})

    agents = Agents({}, {"claude": _complete, "codex": partial})
    harness = _harness(root, stores, agents)
    outputs = [ArmOutput("A", 1, "{}"), ArmOutput("B", 1, "{}")]
    with pytest.raises(RuntimeError, match="grade it again to retry"):
        harness.grade("t10", "20261004", outputs, seed=4)
    first = {p.name: p.read_text() for p in (harness.directory("t10") / "anon").iterdir()}

    with pytest.raises(RuntimeError, match=r"same outputs with the same seed.*sealed/mapping.json"):
        harness.grade("t10", "20261004", outputs, seed=5)
    with pytest.raises(RuntimeError, match=r"changed: \['anon/"):
        harness.grade("t10", "20261004", [ArmOutput("A", 1, "{}"), ArmOutput("B", 1, '{"other": 1}')], seed=4)
    agents.grader_answers["codex"] = _complete
    result = harness.grade("t10", "20261004", outputs, seed=4)

    directory = harness.directory("t10")
    assert len(result.graders) == 6 and all(g.accepted for g in result.graders)
    every = {f"{g}#{n}": (4.0,) for g in ("claude", "codex") for n in (1, 2, 3)}
    assert {a.arm: a.scores for a in result.arms} == {"A": every, "B": every}
    assert {p.name: p.read_text() for p in (directory / "anon").iterdir()} == first
    # The failed attempt's answers are kept for a person to read.
    kept = directory / "graders-attempt-1" / "codex" / "p1" / "grades.json"
    assert kept.read_text() == partial(sorted(Path(name).stem for name in first))
    assert (directory / "graders" / "codex" / "p3" / "grades.json").is_file()
    with pytest.raises(RuntimeError, match="already has a result"):
        harness.grade("t10", "20261004", outputs, seed=4)


def test_a_path_that_names_where_an_answer_was_written_is_hidden_from_graders(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from issue_orchestrator.entrypoints.cli_tools.improver_tournament import (
        read_recorded,
    )

    _, _, root = stores
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    old_run = "/Users/x/repo/.git/io-improver/tournaments/old/arms/A/runs/r1/heats/h1/improver-findings-h1.json"
    engine = "/Users/x/dev/worktree/porchpin/porchpin-7/.issue-orchestrator/sessions/s1"
    (recorded / "A1.txt").write_text(json.dumps({"note": f"see {old_run} and {recorded.resolve()}/A1.txt; {engine}"}))
    (recorded / "B1.txt").write_text("{}")
    harness = _harness(root, stores, Agents({}, {"claude": _complete, "codex": _complete}))

    harness.grade("t11", "20261004", read_recorded(recorded), seed=6)

    texts = " ".join(p.read_text() for p in (harness.directory("t11") / "anon").iterdir())
    assert "io-improver" not in texts and "arms/A" not in texts and str(recorded.resolve()) not in texts
    assert texts.count("<RUN>") == 2
    # The audited engine's paths are evidence, the same for every arm.
    assert engine in texts


def test_a_path_spelled_with_escaped_slashes_is_hidden_too(stores) -> None:  # type: ignore[no-untyped-def]
    _, _, root = stores
    escaped = "\\/Users\\/x\\/.git\\/io-improver\\/tournaments\\/old\\/arms\\/A\\/runs\\/r1\\/f.json"
    engine = "/Users/x/dev/worktree/porchpin/porchpin-7/.issue-orchestrator/sessions/s1"
    harness = _harness(root, stores, Agents({}, {"claude": _complete, "codex": _complete}))

    harness.grade("t12", "20261004", [ArmOutput("A", 1, '{"note": "' + escaped + '", "e": "' + engine + '"}'),
                                      ArmOutput("B", 1, "{}")], seed=7)

    texts = " ".join(p.read_text() for p in (harness.directory("t12") / "anon").iterdir())
    assert "io-improver" not in texts and "arms" not in texts and "<RUN>" in texts
    assert engine in texts


def test_a_grading_that_calls_a_missing_finding_unsupported_has_no_result(stores) -> None:  # type: ignore[no-untyped-def]
    _, _, root = stores

    def ghost(labels: list[str]) -> str:
        return json.dumps({label: {"items": {i: {"grade": "half", "why": "q"} for i in ("1", "2", "9")},
                                   "unsupported": 1, "unsupported_ids": ["ghost"]} for label in labels})

    harness = _harness(root, stores, Agents({}, {"claude": _complete, "codex": ghost}))
    with pytest.raises(RuntimeError, match="calls unsupported \\['ghost'\\]"):
        harness.grade("t13", "20261004", [ArmOutput("A", 1, json.dumps({"findings": [{"id": "f1"}]}))], seed=8)
    assert not (harness.directory("t13") / "result.json").exists()


def test_outputs_are_graded_only_on_the_snapshot_their_arms_ran_on(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snapshots, keys, root = stores
    snapshots.import_("other", improver_data=_legacy_inputs(tmp_path / "o"), taken_at=T0, origin="x")
    keys.seed_sealed("other", SEALED, sealed_at=T0, added_by="coordinator")
    agents = Agents({"valid": json.dumps(example("exam_case"))}, {"claude": _full_credit, "codex": _full_credit})
    harness = _harness(root, stores, agents)
    outputs = harness.run_arms("t14", "20261004", [_spec(TournamentArm(name="A", provider="claude", model="valid",
                                                                       mode="scripted"), heats=HeatPlan(1, 1))])
    arm_calls = len(agents.spaces)

    with pytest.raises(RuntimeError, match="the arms ran on snapshot 20261004, not other"):
        harness.grade("t14", "other", outputs, seed=1)
    with pytest.raises(RuntimeError, match="not these outputs"):
        harness.grade("t14", "20261004", [*outputs, ArmOutput("Z", 1, "{}")], seed=1)
    substituted = [ArmOutput(o.arm, o.heat, '{"findings": []}', o.hide) for o in outputs]
    with pytest.raises(RuntimeError, match="not these outputs"):
        harness.grade("t14", "20261004", substituted, seed=1)
    assert len(agents.spaces) == arm_calls
    assert not (harness.directory("t14") / "result.json").exists()
    with pytest.raises(RuntimeError, match="has already run its arms"):
        harness.run_arms("t14", "20261004", [_spec(TournamentArm(name="B", provider="claude", model="valid",
                                                                 mode="scripted"), heats=HeatPlan(1, 1))])
    assert len(agents.spaces) == arm_calls
    assert harness.grade("t14", "20261004", outputs, seed=1).snapshot_id == "20261004"


def test_arms_within_the_measured_grading_noise_are_reported_indistinguishable(stores) -> None:  # type: ignore[no-untyped-def]
    """Graders that disagree from pass to pass widen the noise band: a small
    difference in means inside it is "≈", not an order."""
    _, _, root = stores
    anon = root / "tournaments" / "t15" / "anon"
    passes: dict[str, int] = {}
    lock = threading.Lock()

    def noisy(labels: list[str]) -> str:
        with lock:
            n = passes["n"] = passes.get("n", 0) + 1
        out = {}
        for label in labels:
            strong = "strong" in (anon / f"{label}.json").read_text()
            weak_grade = "half" if n == 1 else "miss"  # one grading of six credits the weak arm
            grade = "full" if strong else weak_grade
            out[label] = {"items": {i: {"grade": grade if i == "1" else "miss", "why": "q"} for i in ("1", "2", "9")},
                          "unsupported": 0}
        return json.dumps(out)

    harness = _harness(root, stores, Agents({}, {"claude": noisy, "codex": noisy}))
    outputs = [ArmOutput("S", 1, '{"strong": 1}'), ArmOutput("S", 2, '{"strong": 2}'),
               ArmOutput("W", 1, '{"weak": 1}'), ArmOutput("W", 2, '{"weak": 2}'), ArmOutput("N", 1, None)]

    result = harness.grade("t15", "20261004", outputs, seed=2)

    means = {a.arm: a.mean for a in result.arms}
    assert (means["S"], means["W"], means["N"]) == (3.0, 0.25, 0.0)
    assert result.grading_sd is not None and result.grading_sd > 0
    # W differs from N, but by less than the noise its gradings showed.
    assert result.ranking == (("S",), ("W", "N"))
    assert ("W", "N") not in result.distinguishable and ("S", "W") in result.distinguishable


def test_a_tournament_never_touches_github(stores) -> None:  # type: ignore[no-untyped-def]
    from issue_orchestrator.execution.improver_tournament import _NoGitHub

    with pytest.raises(RuntimeError, match="never touches GitHub"):
        _NoGitHub().list_open_issue_labels_complete()


def test_the_graders_default_to_one_claude_and_one_codex() -> None:
    from issue_orchestrator.execution.improver_tournament import DEFAULT_GRADERS

    assert [(g.name, g.choice.provider.value) for g in DEFAULT_GRADERS] == [("claude", "claude"), ("codex", "codex")]
    assert isinstance(DEFAULT_GRADERS[0], Grader)
