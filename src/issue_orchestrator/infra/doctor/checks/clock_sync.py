"""Clock synchronization check for multi-machine claim coordination.

When claims are enabled (multi-orchestrator mode), machines must have
synchronized clocks for lease expiry to work correctly. NTP drift is
typically <1ms, but a misconfigured machine can be minutes off —
enough to break the 5-minute lease renewal buffer.

This check only runs when claims are enabled in the config.
"""

import platform
import re

from ..types import Check
from ...config import Config
from ....ports.command_runner import CommandRunner


def check_clock_sync(config: Config, runner: CommandRunner | None = None) -> list[Check]:
    """Check NTP clock synchronization status.

    Only relevant when claims are enabled (multi-machine coordination).
    """
    if not getattr(config, "claims", None) or not getattr(config.claims, "enabled", False):
        return []

    if runner is None:
        return [Check(
            name="Clock Sync",
            status="info",
            detail="Skipped (no CommandRunner provided)",
        )]

    system = platform.system()

    if system == "Darwin":
        return _check_macos_ntp(runner)
    elif system == "Linux":
        return _check_linux_ntp(runner)
    else:
        return [Check(
            name="Clock Sync",
            status="info",
            detail=f"Cannot check NTP on {system} — verify manually",
        )]


def _check_macos_ntp(runner: CommandRunner) -> list[Check]:
    """Check NTP status on macOS using sntp."""
    try:
        result = runner.run(
            ["sntp", "time.apple.com"],
            timeout_seconds=5,
        )
        output = result.stdout + result.stderr

        if result.returncode == 0:
            offset_s = _parse_sntp_offset(output)
            if offset_s is not None:
                if abs(offset_s) < 1.0:
                    return [Check(
                        name="Clock Sync",
                        status="ok",
                        detail=f"NTP offset: {offset_s:+.3f}s (< 1s)",
                    )]
                elif abs(offset_s) < 30.0:
                    return [Check(
                        name="Clock Sync",
                        status="warning",
                        detail=f"NTP offset: {offset_s:+.1f}s — consider running 'sudo sntp -sS time.apple.com'",
                    )]
                else:
                    return [Check(
                        name="Clock Sync",
                        status="error",
                        detail=f"NTP offset: {offset_s:+.0f}s — clock is dangerously out of sync for claim coordination",
                    )]

        return [Check(
            name="Clock Sync",
            status="info",
            detail="Could not determine NTP offset — verify clock is synced",
        )]
    except Exception:
        return [Check(
            name="Clock Sync",
            status="info",
            detail="sntp not available — verify clock is synced manually",
        )]


def _check_linux_ntp(runner: CommandRunner) -> list[Check]:
    """Check NTP status on Linux using timedatectl."""
    try:
        result = runner.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            timeout_seconds=5,
        )
        synced = result.stdout.strip().lower()

        if synced == "yes":
            return [Check(
                name="Clock Sync",
                status="ok",
                detail="NTP synchronized",
            )]
        elif synced == "no":
            return [Check(
                name="Clock Sync",
                status="warning",
                detail="NTP not synchronized — run 'timedatectl set-ntp true'",
            )]
        else:
            return [Check(
                name="Clock Sync",
                status="info",
                detail=f"Unexpected timedatectl output: {synced}",
            )]
    except Exception:
        # timedatectl not available (e.g., Docker container)
        try:
            result = runner.run(
                ["pgrep", "-x", "ntpd|chronyd"],
                timeout_seconds=5,
            )
            if result.returncode == 0:
                return [Check(
                    name="Clock Sync",
                    status="ok",
                    detail="NTP daemon running",
                )]
        except Exception:
            pass

        return [Check(
            name="Clock Sync",
            status="info",
            detail="Cannot check NTP — verify clock is synced manually",
        )]


def _parse_sntp_offset(output: str) -> float | None:
    """Parse the offset in seconds from sntp output.

    macOS sntp output looks like:
        +0.003412 +/- 0.029045 time.apple.com ...
    """
    match = re.search(r"([+-]?\d+\.\d+)\s+\+/-", output)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None
