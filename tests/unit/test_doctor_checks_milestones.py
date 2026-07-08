"""Unit tests for milestone doctor checks."""

from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.doctor.checks import milestones as milestone_checks


def test_check_milestone_order_skips_when_empty():
    cfg = Config()
    cfg.milestone_order = []

    checks = milestone_checks.check_milestone_order(cfg)

    assert checks == []


def test_check_milestone_order_errors_without_repo(monkeypatch):
    cfg = Config()
    cfg.milestone_order = ["M1"]
    cfg.repo = None

    def _raise_repo_error():
        raise milestone_checks.GitRepoError("missing")

    monkeypatch.setattr(milestone_checks, "get_repo_from_git", _raise_repo_error)

    checks = milestone_checks.check_milestone_order(cfg)

    assert checks[0].status == "error"
    assert "milestones.order" in checks[0].detail


def test_check_milestone_order_errors_when_missing(monkeypatch):
    cfg = Config()
    cfg.milestone_order = ["M1", "M2"]
    cfg.repo = "owner/repo"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())

    class _Client:
        def __init__(self, _config):
            pass

        def list_milestones(self, state="open"):
            assert state == "open"
            return [{"title": "M1", "number": 1}]

        def close(self):
            pass

    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _Client)

    checks = milestone_checks.check_milestone_order(cfg)

    assert checks[0].status == "error"
    assert "M2" in checks[0].detail


def test_check_milestone_order_ok_when_all_found(monkeypatch):
    cfg = Config()
    cfg.milestone_order = ["M1"]
    cfg.repo = "owner/repo"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())

    class _Client:
        def __init__(self, _config):
            pass

        def list_milestones(self, state="open"):
            return [{"title": "M1", "number": 1}]

        def close(self):
            pass

    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _Client)

    checks = milestone_checks.check_milestone_order(cfg)

    assert checks[0].status == "ok"
