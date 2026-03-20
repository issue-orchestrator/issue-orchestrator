"""Unit tests for clock sync doctor check."""

from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.infra.doctor.checks.clock_sync import (
    check_clock_sync,
    _parse_sntp_offset,
)
from issue_orchestrator.ports.command_runner import CommandResult


def make_config(claims_enabled: bool = False) -> MagicMock:
    config = MagicMock()
    config.claims.enabled = claims_enabled
    return config


def make_runner(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Create a mock CommandRunner returning a fixed result."""
    runner = MagicMock()
    runner.run.return_value = CommandResult(
        returncode=returncode, stdout=stdout, stderr=stderr,
    )
    return runner


class TestCheckClockSync:
    """Tests for check_clock_sync."""

    def test_skips_when_claims_disabled(self):
        """Returns no checks when claims are disabled."""
        config = make_config(claims_enabled=False)
        assert check_clock_sync(config) == []

    def test_skips_when_no_claims_config(self):
        """Returns no checks when claims config is absent."""
        config = MagicMock(spec=[])  # No 'claims' attribute
        assert check_clock_sync(config) == []

    def test_skips_when_no_runner(self):
        """Returns info check when no CommandRunner provided."""
        config = make_config(claims_enabled=True)
        checks = check_clock_sync(config, runner=None)
        assert len(checks) == 1
        assert checks[0].status == "info"

    @patch("platform.system", return_value="Darwin")
    def test_macos_ok_small_offset(self, _mock_sys):
        """Reports OK when NTP offset is small on macOS."""
        runner = make_runner(
            returncode=0,
            stdout="+0.003412 +/- 0.029045 time.apple.com",
        )
        config = make_config(claims_enabled=True)
        checks = check_clock_sync(config, runner)

        assert len(checks) == 1
        assert checks[0].status == "ok"
        assert "0.003" in checks[0].detail

    @patch("platform.system", return_value="Darwin")
    def test_macos_warning_moderate_offset(self, _mock_sys):
        """Reports warning when NTP offset is moderate on macOS."""
        runner = make_runner(
            returncode=0,
            stdout="+5.234 +/- 0.100 time.apple.com",
        )
        config = make_config(claims_enabled=True)
        checks = check_clock_sync(config, runner)

        assert len(checks) == 1
        assert checks[0].status == "warning"

    @patch("platform.system", return_value="Darwin")
    def test_macos_error_large_offset(self, _mock_sys):
        """Reports error when NTP offset is large on macOS."""
        runner = make_runner(
            returncode=0,
            stdout="+45.0 +/- 1.0 time.apple.com",
        )
        config = make_config(claims_enabled=True)
        checks = check_clock_sync(config, runner)

        assert len(checks) == 1
        assert checks[0].status == "error"
        assert "dangerously" in checks[0].detail

    @patch("platform.system", return_value="Linux")
    def test_linux_ntp_synced(self, _mock_sys):
        """Reports OK when timedatectl shows NTP synchronized."""
        runner = make_runner(returncode=0, stdout="yes\n")
        config = make_config(claims_enabled=True)
        checks = check_clock_sync(config, runner)

        assert len(checks) == 1
        assert checks[0].status == "ok"

    @patch("platform.system", return_value="Linux")
    def test_linux_ntp_not_synced(self, _mock_sys):
        """Reports warning when timedatectl shows NTP not synchronized."""
        runner = make_runner(returncode=0, stdout="no\n")
        config = make_config(claims_enabled=True)
        checks = check_clock_sync(config, runner)

        assert len(checks) == 1
        assert checks[0].status == "warning"
        assert "timedatectl" in checks[0].detail

    @patch("platform.system", return_value="Linux")
    def test_linux_timedatectl_missing_falls_back_to_pgrep(self, _mock_sys):
        """When timedatectl fails (nonzero exit), falls back to pgrep for ntpd."""
        call_count = [0]

        def mock_run(command, **_kwargs):
            call_count[0] += 1
            if command[0] == "timedatectl":
                # timedatectl not found / failed
                return CommandResult(returncode=127, stdout="", stderr="command not found")
            elif command[0] == "pgrep":
                # ntpd is running
                return CommandResult(returncode=0, stdout="1234\n", stderr="")
            return CommandResult(returncode=1, stdout="", stderr="")

        runner = MagicMock()
        runner.run.side_effect = mock_run
        config = make_config(claims_enabled=True)
        checks = check_clock_sync(config, runner)

        assert len(checks) == 1
        assert checks[0].status == "ok"
        assert "NTP daemon running" in checks[0].detail
        assert call_count[0] == 2  # timedatectl + pgrep

    @patch("platform.system", return_value="Linux")
    def test_linux_both_timedatectl_and_pgrep_fail(self, _mock_sys):
        """When both timedatectl and pgrep fail, returns info check."""
        runner = make_runner(returncode=1, stdout="", stderr="")
        config = make_config(claims_enabled=True)
        checks = check_clock_sync(config, runner)

        assert len(checks) == 1
        assert checks[0].status == "info"
        assert "Cannot check NTP" in checks[0].detail

    @patch("platform.system", return_value="Windows")
    def test_unsupported_platform(self, _mock_sys):
        """Returns info check on unsupported platforms."""
        runner = make_runner()
        config = make_config(claims_enabled=True)
        checks = check_clock_sync(config, runner)

        assert len(checks) == 1
        assert checks[0].status == "info"
        assert "Windows" in checks[0].detail


class TestParseSntpOffset:
    """Tests for _parse_sntp_offset helper."""

    def test_parses_positive_offset(self):
        assert _parse_sntp_offset("+0.003412 +/- 0.029045 time.apple.com") == pytest.approx(0.003412)

    def test_parses_negative_offset(self):
        assert _parse_sntp_offset("-1.234567 +/- 0.100 time.apple.com") == pytest.approx(-1.234567)

    def test_returns_none_for_garbage(self):
        assert _parse_sntp_offset("no valid output here") is None

    def test_returns_none_for_empty(self):
        assert _parse_sntp_offset("") is None
