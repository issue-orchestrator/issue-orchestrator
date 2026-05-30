"""Tests for SessionKey domain model.

These tests verify:
- SessionKey equality based on issue + task
- SessionKey hashing for use as dict keys and set members
- stable_id() format
- Compatibility with different IssueKey implementations
"""

import pytest

from issue_orchestrator.domain import (
    SessionKey,
    TaskKind,
    FakeIssueKey,
    GitHubIssueKey,
)


class TestTaskKind:
    """Tests for the TaskKind enum."""

    def test_task_kind_values(self):
        """TaskKind has expected values."""
        assert TaskKind.CODE.value == "code"
        assert TaskKind.REVIEW.value == "review"
        assert TaskKind.REWORK.value == "rework"
        assert TaskKind.TRIAGE.value == "triage"
        assert TaskKind.RETROSPECTIVE_REVIEW.value == "retrospective-review"

    def test_task_kind_all_members(self):
        """TaskKind has exactly the expected members."""
        assert set(TaskKind) == {
            TaskKind.CODE,
            TaskKind.REVIEW,
            TaskKind.REWORK,
            TaskKind.TRIAGE,
            TaskKind.RETROSPECTIVE_REVIEW,
        }


class TestSessionKeyEquality:
    """Tests for SessionKey equality semantics."""

    def test_same_issue_same_task_are_equal(self):
        """Two keys with same issue and task are equal."""
        issue = FakeIssueKey("M1-011")
        key1 = SessionKey(issue=issue, task=TaskKind.CODE)
        key2 = SessionKey(issue=issue, task=TaskKind.CODE)
        assert key1 == key2

    def test_same_issue_different_task_not_equal(self):
        """Two keys with same issue but different task are not equal."""
        issue = FakeIssueKey("M1-011")
        key1 = SessionKey(issue=issue, task=TaskKind.CODE)
        key2 = SessionKey(issue=issue, task=TaskKind.REVIEW)
        assert key1 != key2

    def test_different_issue_same_task_not_equal(self):
        """Two keys with different issue but same task are not equal."""
        issue1 = FakeIssueKey("M1-011")
        issue2 = FakeIssueKey("M1-012")
        key1 = SessionKey(issue=issue1, task=TaskKind.CODE)
        key2 = SessionKey(issue=issue2, task=TaskKind.CODE)
        assert key1 != key2

    def test_equality_uses_stable_id_not_object_identity(self):
        """Equality is based on stable_id, not object identity."""
        # Two different FakeIssueKey instances with same stable_id
        issue1 = FakeIssueKey("M1-011")
        issue2 = FakeIssueKey("M1-011")
        assert issue1 is not issue2  # Different objects

        key1 = SessionKey(issue=issue1, task=TaskKind.CODE)
        key2 = SessionKey(issue=issue2, task=TaskKind.CODE)
        assert key1 == key2  # But equal keys

    def test_equality_considers_scope(self):
        """Two keys with same stable_id but different scope are not equal."""
        issue1 = FakeIssueKey("M1-011", test_scope="repo-a")
        issue2 = FakeIssueKey("M1-011", test_scope="repo-b")

        key1 = SessionKey(issue=issue1, task=TaskKind.CODE)
        key2 = SessionKey(issue=issue2, task=TaskKind.CODE)
        assert key1 != key2

    def test_not_equal_to_non_session_key(self):
        """SessionKey is not equal to non-SessionKey objects."""
        issue = FakeIssueKey("M1-011")
        key = SessionKey(issue=issue, task=TaskKind.CODE)

        assert key != "code:M1-011"
        assert key != 123
        assert key != None
        assert key != issue


class TestSessionKeyHashing:
    """Tests for SessionKey hashing (dict key, set membership)."""

    def test_can_be_used_as_dict_key(self):
        """SessionKey can be used as a dictionary key."""
        issue = FakeIssueKey("M1-011")
        key = SessionKey(issue=issue, task=TaskKind.CODE)

        d = {key: "value"}
        assert d[key] == "value"

    def test_equal_keys_have_same_hash(self):
        """Equal SessionKeys have the same hash."""
        issue1 = FakeIssueKey("M1-011")
        issue2 = FakeIssueKey("M1-011")

        key1 = SessionKey(issue=issue1, task=TaskKind.CODE)
        key2 = SessionKey(issue=issue2, task=TaskKind.CODE)

        assert key1 == key2
        assert hash(key1) == hash(key2)

    def test_can_be_added_to_set(self):
        """SessionKey can be used in sets."""
        issue = FakeIssueKey("M1-011")
        key1 = SessionKey(issue=issue, task=TaskKind.CODE)
        key2 = SessionKey(issue=issue, task=TaskKind.CODE)

        s = {key1, key2}
        assert len(s) == 1  # Only one because they're equal

    def test_different_keys_in_set(self):
        """Different SessionKeys are separate in sets."""
        issue = FakeIssueKey("M1-011")
        key_code = SessionKey(issue=issue, task=TaskKind.CODE)
        key_review = SessionKey(issue=issue, task=TaskKind.REVIEW)

        s = {key_code, key_review}
        assert len(s) == 2

    def test_dict_lookup_with_equivalent_key(self):
        """Dict lookup works with an equivalent key (not same object)."""
        issue1 = FakeIssueKey("M1-011")
        issue2 = FakeIssueKey("M1-011")

        key1 = SessionKey(issue=issue1, task=TaskKind.CODE)
        key2 = SessionKey(issue=issue2, task=TaskKind.CODE)

        d = {key1: "found"}
        assert d[key2] == "found"  # Lookup with different but equal key


class TestSessionKeyStableId:
    """Tests for SessionKey.stable_id() method."""

    def test_stable_id_format(self):
        """stable_id returns {task}:{issue_stable_id}."""
        issue = FakeIssueKey("M1-011")
        key = SessionKey(issue=issue, task=TaskKind.CODE)
        assert key.stable_id() == "code:M1-011"

    def test_stable_id_for_each_task_kind(self):
        """stable_id works for all task kinds."""
        issue = FakeIssueKey("M1-011")

        assert SessionKey(issue=issue, task=TaskKind.CODE).stable_id() == "code:M1-011"
        assert SessionKey(issue=issue, task=TaskKind.REVIEW).stable_id() == "review:M1-011"
        assert SessionKey(issue=issue, task=TaskKind.REWORK).stable_id() == "rework:M1-011"
        assert SessionKey(issue=issue, task=TaskKind.TRIAGE).stable_id() == "triage:M1-011"
        assert (
            SessionKey(issue=issue, task=TaskKind.RETROSPECTIVE_REVIEW).stable_id()
            == "retrospective-review:M1-011"
        )


class TestSessionKeyStr:
    """Tests for SessionKey.__str__() method."""

    def test_str_includes_scope(self):
        """__str__ includes the full issue representation."""
        issue = FakeIssueKey("M1-011")
        key = SessionKey(issue=issue, task=TaskKind.CODE)
        result = str(key)
        assert "code" in result
        assert "M1-011" in result


class TestSessionKeyWithGitHubIssueKey:
    """Tests for SessionKey with GitHubIssueKey (real implementation)."""

    def test_with_github_issue_key(self):
        """SessionKey works with GitHubIssueKey."""
        issue = GitHubIssueKey(repo="owner/repo", external_id="M1-011")
        key = SessionKey(issue=issue, task=TaskKind.CODE)

        assert key.stable_id() == "code:M1-011"
        assert key.task == TaskKind.CODE

    def test_github_keys_different_repos_not_equal(self):
        """GitHubIssueKeys from different repos produce different SessionKeys."""
        issue1 = GitHubIssueKey(repo="owner/repo-a", external_id="M1-011")
        issue2 = GitHubIssueKey(repo="owner/repo-b", external_id="M1-011")

        key1 = SessionKey(issue=issue1, task=TaskKind.CODE)
        key2 = SessionKey(issue=issue2, task=TaskKind.CODE)

        assert key1 != key2  # Different scope (repo)


class TestSessionKeyImmutability:
    """Tests for SessionKey immutability (frozen dataclass)."""

    def test_cannot_modify_task(self):
        """SessionKey.task cannot be modified."""
        issue = FakeIssueKey("M1-011")
        key = SessionKey(issue=issue, task=TaskKind.CODE)

        with pytest.raises(AttributeError):
            key.task = TaskKind.REVIEW

    def test_cannot_modify_issue(self):
        """SessionKey.issue cannot be modified."""
        issue = FakeIssueKey("M1-011")
        key = SessionKey(issue=issue, task=TaskKind.CODE)

        with pytest.raises(AttributeError):
            key.issue = FakeIssueKey("M1-012")
