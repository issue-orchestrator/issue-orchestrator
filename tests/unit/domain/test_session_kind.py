"""SessionKind: the one authoritative kind, and every vocabulary derived from it (#7347)."""

import pytest

from issue_orchestrator.domain.session_kind import (
    HISTORICAL_AGENT_LABEL,
    SessionKind,
    SessionType,
)

TL = "agent:tech-lead"


class TestPersistedValues:
    def test_values_are_the_persisted_strings(self) -> None:
        """The ledger, run directory and queued-work payloads store these."""
        assert {kind: kind.value for kind in SessionKind} == {
            SessionKind.CODE: "code",
            SessionKind.REVIEW: "review",
            SessionKind.RETROSPECTIVE_REVIEW: "retrospective-review",
            SessionKind.REWORK: "rework",
            SessionKind.TECH_LEAD: "tech-lead",
            SessionKind.HISTORICAL: "historical",
        }


class TestDerivedVocabularies:
    @pytest.mark.parametrize(
        ("kind", "session_type", "terminal", "phase"),
        [
            (SessionKind.CODE, SessionType.ISSUE, "issue-7", "coding-2"),
            (SessionKind.REWORK, SessionType.REWORK, "rework-7", "coding-2"),
            (SessionKind.REVIEW, SessionType.REVIEW, "review-7", "review-2"),
            (
                SessionKind.RETROSPECTIVE_REVIEW,
                SessionType.RETROSPECTIVE_REVIEW,
                "retrospective-review-7",
                "retrospective-review-2",
            ),
            (SessionKind.TECH_LEAD, SessionType.TECH_LEAD, "tech-lead-7", "tech-lead-2"),
        ],
    )
    def test_each_kind_names_its_terminal_and_phase(
        self, kind: SessionKind, session_type: SessionType, terminal: str, phase: str
    ) -> None:
        """A tech-lead run is no longer ``issue-N`` / ``coding-1`` (#7347)."""
        assert kind.session_type is session_type
        assert kind.terminal_name(7) == terminal
        assert kind.phase_label(2) == phase

    def test_a_historical_import_has_no_terminal_or_phase(self) -> None:
        with pytest.raises(ValueError, match="no terminal"):
            _ = SessionKind.HISTORICAL.session_type
        with pytest.raises(ValueError, match="no launch phase"):
            SessionKind.HISTORICAL.phase_label(1)

    @pytest.mark.parametrize("attempt", [0, -1, True])
    def test_a_phase_attempt_must_be_a_positive_int(self, attempt: object) -> None:
        with pytest.raises(ValueError, match="positive int"):
            SessionKind.CODE.phase_label(attempt)  # type: ignore[arg-type]



class TestTheLaunchStamp:
    @pytest.mark.parametrize(
        ("agent", "tech_lead_agent", "expected"),
        [
            (TL, TL, SessionKind.TECH_LEAD),
            ("agent:web", TL, SessionKind.CODE),
            (None, TL, SessionKind.CODE),
            (TL, None, SessionKind.CODE),
            (TL, "", SessionKind.CODE),
        ],
    )
    def test_the_tech_lead_agent_launches_a_tech_lead_run(
        self, agent: str | None, tech_lead_agent: str | None, expected: SessionKind
    ) -> None:
        assert SessionKind.for_issue_launch(agent, tech_lead_agent) is expected

    def test_a_tech_lead_grant_for_any_other_agent_is_refused(self) -> None:
        with pytest.raises(ValueError, match="tech-lead scope would run as code"):
            SessionKind.for_issue_launch("agent:web", TL, granted_tech_lead_scope=True)
        assert (
            SessionKind.for_issue_launch(TL, TL, granted_tech_lead_scope=True)
            is SessionKind.TECH_LEAD
        )


class TestLegacyLedgerStamps:
    @pytest.mark.parametrize("kind", [k for k in SessionKind])
    def test_a_row_written_since_7347_is_its_kind(self, kind: SessionKind) -> None:
        label = HISTORICAL_AGENT_LABEL if kind is SessionKind.HISTORICAL else "agent:web"
        assert SessionKind.from_ledger_stamps(kind.value, kind.value, label) is kind

    def test_a_tech_lead_launched_under_the_old_code_stamp_is_a_tech_lead(self) -> None:
        """``completion_task`` was the durable role record before #7347."""
        assert (
            SessionKind.from_ledger_stamps("code", "tech-lead", TL) is SessionKind.TECH_LEAD
        )

    def test_a_historical_import_stamped_code_is_historical(self) -> None:
        assert (
            SessionKind.from_ledger_stamps("code", "code", HISTORICAL_AGENT_LABEL)
            is SessionKind.HISTORICAL
        )

    def test_a_row_that_never_recorded_a_role_keeps_its_slot_kind(self) -> None:
        assert SessionKind.from_ledger_stamps("rework", None, None) is SessionKind.REWORK

    @pytest.mark.parametrize(
        ("task", "completion_task", "agent"),
        [
            ("code", None, "agent:web"),  # half a role
            ("code", "code", None),  # half a role
            ("review", "code", "agent:web"),  # stamps disagree
            ("tech-lead", "code", TL),  # a new row can never disagree
            ("code", "rework", "agent:web"),
            ("mystery", "mystery", "agent:web"),
        ],
    )
    def test_ambiguous_or_corrupt_stamps_fail_fast(
        self, task: str, completion_task: str | None, agent: str | None
    ) -> None:
        with pytest.raises(ValueError):
            SessionKind.from_ledger_stamps(task, completion_task, agent)


class TestPhaseLabelPreFilter:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("issue-42", SessionKind.CODE),
            ("coding-2", SessionKind.CODE),
            ("rework-42", SessionKind.REWORK),
            ("tech-lead-1", SessionKind.TECH_LEAD),
            ("review-7", SessionKind.REVIEW),
            ("retrospective-review-42", SessionKind.RETROSPECTIVE_REVIEW),
        ],
    )
    def test_known_labels_classify(self, label: str, expected: SessionKind) -> None:
        assert SessionKind.from_phase_label(label) is expected

    def test_unknown_labels_fail_safe_as_none(self) -> None:
        assert SessionKind.from_phase_label("mystery-1") is None
        assert SessionKind.from_phase_label("") is None

    @pytest.mark.parametrize("kind", [k for k in SessionKind if k.runs_as_agent_session])
    def test_every_phase_label_a_kind_writes_reads_back_whether_it_commits(
        self, kind: SessionKind
    ) -> None:
        """The pre-filter only has to get "makes commits" right: rework shares
        ``coding-N`` by design and the ledger join supplies the real kind."""
        parsed = SessionKind.from_phase_label(kind.phase_label(1))
        assert parsed is not None
        assert parsed.capabilities.produces_commits is kind.capabilities.produces_commits


def test_every_kind_names_its_work_and_only_a_tech_lead_is_one() -> None:
    """Custody's card words and tech-lead identity come from the owner (#7347)."""
    assert {kind: kind.work_description for kind in SessionKind}[SessionKind.REWORK] == "rework session"
    assert all(kind.work_description for kind in SessionKind)
    assert {kind for kind in SessionKind if kind.is_tech_lead} == {SessionKind.TECH_LEAD}
