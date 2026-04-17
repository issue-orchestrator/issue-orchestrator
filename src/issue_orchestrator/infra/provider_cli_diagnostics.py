"""Diagnostics for provider CLI executable resolution."""

from __future__ import annotations

import glob
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def provider_cli_display(provider_name: str, executable: str) -> str:
    """Return a display label that distinguishes provider name from executable."""
    if executable == provider_name:
        return provider_name
    return f"{provider_name} via {executable}"


def provider_cli_missing_detail(provider_name: str, executable: str) -> str:
    """Return an actionable detail string for a missing provider CLI executable."""
    label = provider_name
    if executable != provider_name:
        label = f"{provider_name} (expected executable: {executable})"

    parts = [label, f"executable '{executable}' not found on PATH"]
    nvm_bin = os.environ.get("NVM_BIN")
    if nvm_bin:
        parts.append(f"NVM_BIN={nvm_bin}")

    node_bins = _node_bins_on_path()
    if node_bins and (not nvm_bin or node_bins != [nvm_bin]):
        parts.append("PATH node bins=" + ", ".join(node_bins[:3]))

    outside_path = _find_executable_outside_path(executable)
    if outside_path:
        parts.append("found outside PATH: " + ", ".join(str(path) for path in outside_path[:3]))

    logger.warning(
        "Provider CLI executable is missing from PATH: provider=%s executable=%s "
        "nvm_bin=%s path=%s found_outside_path=%s",
        provider_name,
        executable,
        nvm_bin,
        os.environ.get("PATH", ""),
        [str(path) for path in outside_path],
    )
    return "; ".join(parts)


def _node_bins_on_path() -> list[str]:
    return [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if "/.nvm/versions/node/" in entry
    ]


def _find_executable_outside_path(executable: str) -> list[Path]:
    current = shutil.which(executable)
    current_path = Path(current).resolve() if current else None
    paths: list[Path] = []
    home = Path.home()
    patterns = [
        str(home / ".nvm" / "versions" / "node" / "*" / "bin" / executable),
        str(home / ".npm-global" / "bin" / executable),
        str(home / ".local" / "bin" / executable),
        f"/opt/homebrew/bin/{executable}",
        f"/usr/local/bin/{executable}",
    ]
    for pattern in patterns:
        for raw_path in glob.glob(pattern):
            path = Path(raw_path)
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if current_path and resolved == current_path:
                continue
            if path.is_file() and os.access(path, os.X_OK):
                paths.append(path)
    return sorted(dict.fromkeys(paths))
