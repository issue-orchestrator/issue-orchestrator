"""Tests for adapter boundary guardrail."""

import tempfile
from pathlib import Path

from issue_orchestrator.validation.adapter_boundary_guardrail import (
    AdapterBoundaryResult,
    BoundaryViolation,
    check_adapter_boundaries,
    _check_file,
    _get_module_package,
    _should_check_file,
)


class TestGetModulePackage:
    """Test module package detection."""

    def test_control_module(self):
        """Test detection of control module."""
        path = Path("/some/path/src/issue_orchestrator/control/foo.py")
        result = _get_module_package(path)
        assert result == "issue_orchestrator.control"

    def test_adapters_module(self):
        """Test detection of adapters module."""
        path = Path("/some/path/src/issue_orchestrator/adapters/github.py")
        result = _get_module_package(path)
        assert result == "issue_orchestrator.adapters"

    def test_execution_module(self):
        """Test detection of execution module."""
        path = Path("/some/path/src/issue_orchestrator/execution/manager.py")
        result = _get_module_package(path)
        assert result == "issue_orchestrator.execution"

    def test_entrypoints_module(self):
        """Test detection of entrypoints module."""
        path = Path("/some/path/src/issue_orchestrator/entrypoints/bootstrap.py")
        result = _get_module_package(path)
        assert result == "issue_orchestrator.entrypoints"


class TestShouldCheckFile:
    """Test file checking heuristic."""

    def test_check_control_files(self):
        """Control files should be checked."""
        assert _should_check_file("issue_orchestrator.control.foo")

    def test_check_entrypoints_files(self):
        """Entrypoints files should be checked."""
        assert _should_check_file("issue_orchestrator.entrypoints.bootstrap")

    def test_check_observation_files(self):
        """Observation files should be checked."""
        assert _should_check_file("issue_orchestrator.observation.foo")

    def test_check_domain_files(self):
        """Domain files should be checked."""
        assert _should_check_file("issue_orchestrator.domain.foo")

    def test_skip_execution_files(self):
        """Execution files should not be checked."""
        assert not _should_check_file("issue_orchestrator.execution.foo")

    def test_skip_adapters_files(self):
        """Adapters files should not be checked."""
        assert not _should_check_file("issue_orchestrator.adapters.foo")

    def test_skip_ports_files(self):
        """Ports files should not be checked."""
        assert not _should_check_file("issue_orchestrator.ports.foo")

    def test_skip_empty_package(self):
        """Empty package should not be checked."""
        assert not _should_check_file("")


class TestCheckFileAdapterInternalImport:
    """Test detection of adapter internal imports."""

    def test_import_github_http_client_violation(self):
        """Importing GitHubHttpClient should be detected."""
        code = """
from issue_orchestrator.adapters.github.http_client import GitHubHttpClient

client = GitHubHttpClient()
"""
        path = Path("/src/issue_orchestrator/control/foo.py")
        violations = _check_file(path, code)
        assert len(violations) >= 1
        assert any(v.violation_type == "import" for v in violations)

    def test_import_cache_violation(self):
        """Importing GitHubCache should be detected."""
        code = """
from issue_orchestrator.adapters.github.cache import GitHubCache

cache = GitHubCache()
"""
        path = Path("/src/issue_orchestrator/control/foo.py")
        violations = _check_file(path, code)
        assert len(violations) >= 1
        assert any(v.violation_type == "import" for v in violations)

    def test_import_from_port_no_violation(self):
        """Importing from ports should not violate."""
        code = """
from issue_orchestrator.ports.repository_host import RepositoryHost

def use_repo(repo: RepositoryHost):
    pass
"""
        path = Path("/src/issue_orchestrator/control/foo.py")
        violations = _check_file(path, code)
        # Should have no violations (no internal imports)
        adapter_import_violations = [
            v for v in violations if v.violation_type == "import"
        ]
        assert len(adapter_import_violations) == 0

    def test_import_normal_class_no_violation(self):
        """Importing normal classes should not violate."""
        code = """
from issue_orchestrator.domain.issue_key import IssueKey

key = IssueKey()
"""
        path = Path("/src/issue_orchestrator/control/foo.py")
        violations = _check_file(path, code)
        adapter_import_violations = [
            v for v in violations if v.violation_type == "import"
        ]
        assert len(adapter_import_violations) == 0


class TestCheckFilePrivateAttributeAccess:
    """Test detection of private attribute access on adapters."""

    def test_access_github_private_attribute(self):
        """Accessing github._http_client should be detected."""
        code = """
def setup(github):
    client = github._http_client
    return client
"""
        path = Path("/src/issue_orchestrator/entrypoints/bootstrap.py")
        violations = _check_file(path, code)
        assert len(violations) >= 1
        attribute_violations = [
            v for v in violations if v.violation_type == "attribute_access"
        ]
        assert len(attribute_violations) >= 1

    def test_access_repository_host_private_attribute(self):
        """Accessing repository_host._http_client should be detected."""
        code = """
def setup(repository_host):
    client = repository_host._http_client
    return client
"""
        path = Path("/src/issue_orchestrator/control/foo.py")
        violations = _check_file(path, code)
        attribute_violations = [
            v for v in violations if v.violation_type == "attribute_access"
        ]
        assert len(attribute_violations) >= 1

    def test_access_public_method_no_violation(self):
        """Accessing public methods should not violate."""
        code = """
def setup(github):
    issues = github.get_issue_labels(42)
    return issues
"""
        path = Path("/src/issue_orchestrator/control/foo.py")
        violations = _check_file(path, code)
        attribute_violations = [
            v for v in violations if v.violation_type == "attribute_access"
        ]
        assert len(attribute_violations) == 0


class TestCheckAdapterBoundaries:
    """Test full boundary checking."""

    def test_check_directory_with_violations(self):
        """Check directory with violations should report failures."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create issue_orchestrator/control/test.py with violation
            control_dir = tmppath / "issue_orchestrator" / "control"
            control_dir.mkdir(parents=True, exist_ok=True)

            control_file = control_dir / "test.py"
            control_file.write_text("""
from issue_orchestrator.adapters.github.http_client import GitHubHttpClient

def setup():
    client = GitHubHttpClient()
    return client
""")

            result = check_adapter_boundaries(tmppath)
            assert result.status == "fail"
            assert len(result.violations) >= 1

    def test_check_directory_no_violations(self):
        """Check directory with no violations should pass."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create issue_orchestrator/control/test.py with no violation
            control_dir = tmppath / "issue_orchestrator" / "control"
            control_dir.mkdir(parents=True, exist_ok=True)

            control_file = control_dir / "test.py"
            control_file.write_text("""
from issue_orchestrator.ports.repository_host import RepositoryHost

def setup(repo: RepositoryHost):
    labels = repo.get_issue_labels(42)
    return labels
""")

            result = check_adapter_boundaries(tmppath)
            assert result.status == "ok"
            assert len(result.violations) == 0

    def test_check_skip_adapter_files(self):
        """Check should skip files in adapters/execution directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create issue_orchestrator/adapters/test.py with "violation"
            # (but it should be skipped because it's in adapters/)
            adapters_dir = tmppath / "issue_orchestrator" / "adapters"
            adapters_dir.mkdir(parents=True, exist_ok=True)

            adapters_file = adapters_dir / "test.py"
            adapters_file.write_text("""
from issue_orchestrator.adapters.github.http_client import GitHubHttpClient

def setup():
    client = GitHubHttpClient()
    return client
""")

            result = check_adapter_boundaries(tmppath)
            # Should not fail because adapters/ files are not checked
            assert result.status == "ok"

    def test_check_nonexistent_directory(self):
        """Check nonexistent directory should return error."""
        result = check_adapter_boundaries(Path("/nonexistent/path"))
        assert result.status == "error"
        assert result.reason is not None and "not found" in result.reason


class TestBoundaryViolation:
    """Test BoundaryViolation dataclass."""

    def test_violation_has_required_fields(self):
        """Violation should have all required fields."""
        violation = BoundaryViolation(
            file_path="foo.py",
            line_number=42,
            violation_type="import",
            message="Test message",
            code_snippet="some code",
        )
        assert violation.file_path == "foo.py"
        assert violation.line_number == 42
        assert violation.violation_type == "import"
        assert violation.message == "Test message"
        assert violation.code_snippet == "some code"


class TestAdapterBoundaryResult:
    """Test AdapterBoundaryResult dataclass."""

    def test_result_ok_status(self):
        """Result with ok status should have no violations."""
        result = AdapterBoundaryResult(status="ok", violations=[])
        assert result.status == "ok"
        assert len(result.violations) == 0

    def test_result_fail_status(self):
        """Result with fail status should have violations."""
        violation = BoundaryViolation(
            file_path="foo.py",
            line_number=42,
            violation_type="import",
            message="Test",
            code_snippet="code",
        )
        result = AdapterBoundaryResult(
            status="fail",
            violations=[violation],
            reason="Found violations",
        )
        assert result.status == "fail"
        assert len(result.violations) == 1
        assert result.reason == "Found violations"
