"""A failed fresh label read must never look like an observation (#6957 R2 F4).

``GitHubFreshIssueReader`` used to swallow every read failure and return ``[]``.
That is indistinguishable from "this issue genuinely has no labels" — and an
empty label set SATISFIES the expectation every tech-lead mutation carries,
which forbids ``io:needs-reconcile`` and requires nothing. So a timeout, a rate
limit, or an auth failure let the control plane walk straight through an
explicit operator pause and create issues, comment cross-repo, settle
promotions, reset work, or kill sessions.

The round-one guardrail tests raised at the applier seam directly, so they never
exercised the production adapter's error-to-empty fallback. These do: the real
adapter is driven with a failing HTTP client, and the real applier is driven
with the real adapter.
"""

from unittest.mock import MagicMock, Mock

import pytest

from issue_orchestrator.adapters.github.errors import GitHubHttpError
from issue_orchestrator.adapters.github.fresh_issue_reader import GitHubFreshIssueReader
from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import (
    AddLabelAction,
    PromoteTechLeadFindingAction,
)
from issue_orchestrator.control.reconciliation import (
    ReconciliationRequired,
    build_expected_for_mutation,
)
from issue_orchestrator.ports.fresh_issue_reader import FreshIssueReadError

CASE_FILE = 65
MARKER = "<!-- issue-orchestrator:tech-lead-promotion:v1:abc -->"


def _reader(http_client) -> GitHubFreshIssueReader:
    """The REAL adapter over an injected client — no network, real error path."""
    return GitHubFreshIssueReader(repo="porchpin/porchpin", http_client=http_client)


class TestTheAdapterNeverFabricatesAnEmptyLabelSet:
    def test_a_github_error_raises_instead_of_returning_no_labels(self):
        client = Mock()
        client.get_issue_labels.side_effect = GitHubHttpError("rate limited")

        with pytest.raises(FreshIssueReadError):
            _reader(client).read_issue_labels(CASE_FILE)

    def test_an_unexpected_error_raises_too(self):
        """A transport/auth fault is just as unknown as an API error."""
        client = Mock()
        client.get_issue_labels.side_effect = TimeoutError("connect timeout")

        with pytest.raises(FreshIssueReadError):
            _reader(client).read_issue_labels(CASE_FILE)

    def test_an_issue_that_really_has_no_labels_still_reads_as_empty(self):
        """The distinction only matters if the honest empty answer survives."""
        client = Mock()
        client.get_issue_labels.return_value = []

        assert _reader(client).read_issue_labels(CASE_FILE) == []
        client.get_issue_labels.assert_called_once_with(CASE_FILE, use_cache=False)


class TestTheApplierFailsClosedOnTheRealAdapter:
    """The composed path: real adapter + real applier, no seam-level mocking."""

    @staticmethod
    def _applier(
        client,
        *,
        labels=None,
        repository_host=None,
        promotion_target=None,
        tech_lead_ops=None,
    ) -> ActionApplier:
        return ActionApplier(
            labels=labels or MagicMock(),
            sessions=MagicMock(),
            events=MagicMock(),
            repository_host=repository_host or MagicMock(),
            fresh_issue_reader=_reader(client),
            reconcile=True,
            tech_lead_ops=tech_lead_ops or MagicMock(),
            promotion_target=promotion_target or MagicMock(),
        )

    @staticmethod
    def _failing_client():
        client = Mock()
        client.get_issue_labels.side_effect = GitHubHttpError("rate limited")
        return client

    def _promotion(self):
        return PromoteTechLeadFindingAction(
            signature="sig",
            case_file_issue_number=CASE_FILE,
            target_repo="owner/upstream",
            title="[tech-lead:src] sig",
            body=f"body\n\n{MARKER}",
            labels=("agent:backend",),
            observation_count=2,
            idempotency_marker=MARKER,
            expected=build_expected_for_mutation(),
        )

    def test_an_unreadable_case_file_pauses_the_promotion(self):
        applier = self._applier(self._failing_client())

        with pytest.raises(ReconciliationRequired):
            applier.apply(self._promotion())

    def test_it_writes_nothing_anywhere(self):
        """Fail closed means zero writes, not a best-effort partial."""
        target = MagicMock()
        authority = MagicMock()
        repository_host = MagicMock()
        applier = self._applier(
            self._failing_client(),
            promotion_target=target,
            tech_lead_ops=authority,
            repository_host=repository_host,
        )

        with pytest.raises(ReconciliationRequired):
            applier.apply(self._promotion())

        assert target.method_calls == []
        assert authority.method_calls == []
        repository_host.add_comment.assert_not_called()
        repository_host.create_issue.assert_not_called()

    def test_the_same_failure_pauses_an_ordinary_label_write(self):
        """Not a tech-lead quirk: every expectation-carrying mutation is covered."""
        labels = MagicMock()
        applier = self._applier(self._failing_client(), labels=labels)

        with pytest.raises(ReconciliationRequired):
            applier.apply(
                AddLabelAction(
                    issue_number=CASE_FILE,
                    label="in-progress",
                    expected=build_expected_for_mutation(),
                )
            )

        labels.add_label.assert_not_called()

    def test_an_issue_that_really_has_no_labels_is_allowed_through(self):
        """The gate must block UNKNOWN, not merely "few labels"."""
        client = Mock()
        client.get_issue_labels.return_value = []
        labels = MagicMock()
        applier = self._applier(client, labels=labels)

        result = applier.apply(
            AddLabelAction(
                issue_number=CASE_FILE,
                label="in-progress",
                expected=build_expected_for_mutation(),
            )
        )

        assert result.success


class TestTheSnapshotReadNeverFabricatesAFact:
    """The same contract, for the read that also carries the issue's state.

    ``read_issue_snapshot`` answers the question "may this nonterminal
    reconciliation outcome be written?", and the facts it returns are the pause
    label and whether the case file is still open. Every way that read can be
    incomplete must therefore be UNKNOWN, never a default — the first version
    filtered out any label it could not decode, which silently reported an
    undecodable ``io:needs-reconcile`` as absent and would have authorized a
    mutation straight through an operator pause (#7248 round 7 review F10).
    """

    def _payload(self, **overrides):
        payload = {
            "state": "open",
            "labels": [{"name": "io:needs-reconcile"}, {"name": "bug"}],
        }
        payload.update(overrides)
        return payload

    def test_a_valid_payload_yields_labels_and_state_from_an_uncached_read(self):
        client = Mock()
        client.get_issue.return_value = self._payload()

        snapshot = _reader(client).read_issue_snapshot(CASE_FILE)

        assert snapshot.number == CASE_FILE
        assert snapshot.state == "open"
        assert snapshot.labels == ("io:needs-reconcile", "bug")
        client.get_issue.assert_called_once_with(CASE_FILE, use_cache=False)

    def test_a_closed_issue_reports_closed(self):
        client = Mock()
        client.get_issue.return_value = self._payload(state="closed", labels=[])

        assert _reader(client).read_issue_snapshot(CASE_FILE).state == "closed"

    def test_a_github_error_raises_instead_of_returning_a_snapshot(self):
        client = Mock()
        client.get_issue.side_effect = GitHubHttpError("429 rate limited")

        with pytest.raises(FreshIssueReadError):
            _reader(client).read_issue_snapshot(CASE_FILE)

    def test_an_unexpected_error_raises_instead_of_returning_a_snapshot(self):
        client = Mock()
        client.get_issue.side_effect = RuntimeError("socket closed")

        with pytest.raises(FreshIssueReadError):
            _reader(client).read_issue_snapshot(CASE_FILE)

    @pytest.mark.parametrize(
        "payload",
        (
            None,
            "not an issue",
            {"labels": []},
            {"state": "open"},
            {"state": 7, "labels": []},
            {"state": "open", "labels": "bug"},
            {"state": "reopened", "labels": []},
        ),
        ids=(
            "absent",
            "not-an-object",
            "missing-state",
            "missing-labels",
            "non-string-state",
            "non-list-labels",
            "unknown-state",
        ),
    )
    def test_an_unreadable_payload_is_unknown(self, payload):
        client = Mock()
        client.get_issue.return_value = payload

        with pytest.raises(FreshIssueReadError):
            _reader(client).read_issue_snapshot(CASE_FILE)

    @pytest.mark.parametrize(
        "label",
        ({"colour": "red"}, {"name": None}, {"name": 7}, 12, None),
        ids=("no-name", "null-name", "numeric-name", "bare-number", "null"),
    )
    def test_a_label_this_adapter_cannot_decode_is_unknown(self, label):
        """Dropping it would report that label as ABSENT."""
        client = Mock()
        client.get_issue.return_value = self._payload(
            labels=[{"name": "bug"}, label]
        )

        with pytest.raises(FreshIssueReadError, match="cannot decode"):
            _reader(client).read_issue_snapshot(CASE_FILE)

    def test_a_malformed_label_cannot_authorize_a_guarded_transition(self):
        """The guard-level consequence, through the real gate and authority.

        The undecodable label here IS the pause label. Filtering it out made the
        payload look like a clean issue and granted the classification.
        """
        from issue_orchestrator.control.mutation_gate import ReconciliationGate
        from issue_orchestrator.control.tech_lead_case_file_lifecycle_reconciliation import (
            GatedCaseFileMutationAuthority,
        )

        client = Mock()
        client.get_issue.return_value = {
            "state": "open",
            "labels": [{"colour": "red"}],  # the pause label, undecodable
        }
        reader = _reader(client)
        authority = GatedCaseFileMutationAuthority(
            gate=ReconciliationGate(
                fresh_issue_reader=reader,
                reconcile=True,
                fresh_issue_snapshot_reader=reader,
            ),
            pause_label="io:needs-reconcile",
        )

        with pytest.raises(ReconciliationRequired):
            authority.require_classification(CASE_FILE)
