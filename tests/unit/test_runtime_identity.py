"""Tests for running orchestrator identity resolution."""

from issue_orchestrator.infra import runtime_identity


def test_resolve_runtime_identity_combines_package_version_and_source_sha(
    monkeypatch,
) -> None:
    sha = "abcdef1234567890abcdef1234567890abcdef12"
    monkeypatch.setattr(runtime_identity, "__version__", "1.2.3")
    monkeypatch.setattr(runtime_identity, "resolve_cc_commit_sha", lambda: sha)

    identity = runtime_identity.resolve_runtime_identity()

    assert identity.package_version == "1.2.3"
    assert identity.source_commit_sha == sha
    assert identity.source_commit_short == sha[:7]


def test_resolve_runtime_identity_uses_canonical_unknown_package_version(
    monkeypatch,
) -> None:
    monkeypatch.setattr(runtime_identity, "__version__", "0+unknown")
    monkeypatch.setattr(runtime_identity, "resolve_cc_commit_sha", lambda: None)

    identity = runtime_identity.resolve_runtime_identity()

    assert identity.package_version == "0+unknown"
    assert identity.source_commit_sha is None
    assert identity.source_commit_short is None
