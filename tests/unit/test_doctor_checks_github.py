"""Unit tests for GitHub doctor checks."""

from issue_orchestrator.adapters.github.tokens import TokenValidationResult
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.doctor.checks import github as github_checks


def test_check_github_auth_respects_repo_scoped_sources(monkeypatch):
    cfg = Config()
    cfg.repo = "BruceBGordon/tixmeup"
    cfg.github_token_env = "TIXMEUP_GITHUB_TOKEN"

    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens.describe_github_token_sources",
        lambda **_kw: [],
    )
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.http_client.validate_github_token",
        lambda **_kw: TokenValidationResult(
            valid=False,
            error="GitHub token not configured for repo-specific auth. Checked env:TIXMEUP_GITHUB_TOKEN.",
        ),
    )

    checks = github_checks.check_github_auth(cfg)

    assert checks[0].status == "error"
    assert "repo-configured sources" in checks[0].detail
    assert "env:TIXMEUP_GITHUB_TOKEN" in checks[0].detail
    assert checks[1].status == "error"
    assert "repo-specific auth" in checks[1].detail


def test_check_github_auth_reports_repo_access(monkeypatch):
    cfg = Config()
    cfg.repo = "BruceBGordon/tixmeup"
    cfg.github_keyring_service = "tixmeup-github"
    cfg.github_keyring_username = "bruce"

    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.tokens.describe_github_token_sources",
        lambda **_kw: ["Keyring (tixmeup-github/bruce): ghp_...1234"],
    )
    monkeypatch.setattr(
        "issue_orchestrator.adapters.github.http_client.validate_github_token",
        lambda **_kw: TokenValidationResult(valid=True, username="octocat"),
    )

    checks = github_checks.check_github_auth(cfg)

    assert checks[0].status == "ok"
    assert "tixmeup-github/bruce" in checks[0].detail
    assert checks[1].status == "ok"
    assert checks[1].detail == "Authenticated as: octocat with access to BruceBGordon/tixmeup"
