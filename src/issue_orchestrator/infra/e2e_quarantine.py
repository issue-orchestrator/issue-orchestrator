"""Quarantine file helpers for E2E test node IDs."""

from pathlib import Path


def load_quarantine_list(quarantine_path: Path) -> set[str]:
    """Load quarantined test nodeids from a file.

    File format: one nodeid per line, lines starting with # are comments.

    Args:
        quarantine_path: Path to quarantine file

    Returns:
        Set of quarantined nodeids
    """
    if not quarantine_path.exists():
        return set()

    quarantined = set()
    with open(quarantine_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                quarantined.add(line)

    return quarantined


def save_quarantine_list(quarantine_path: Path, nodeids: set[str]) -> None:
    """Save the quarantine list to a file.

    Creates the file and parent directories if they don't exist.
    Preserves header comment if present.

    Args:
        quarantine_path: Path to quarantine file
        nodeids: Set of nodeids to quarantine
    """
    header_lines = []
    if quarantine_path.exists():
        with open(quarantine_path) as f:
            for line in f:
                if line.startswith("#"):
                    header_lines.append(line.rstrip())
                else:
                    break

    quarantine_path.parent.mkdir(parents=True, exist_ok=True)

    with open(quarantine_path, "w") as f:
        if header_lines:
            for line in header_lines:
                f.write(line + "\n")
            f.write("\n")
        else:
            f.write("# Quarantined E2E tests\n")
            f.write("# Tests listed here are excluded from E2E failure counts\n")
            f.write("\n")

        for nodeid in sorted(nodeids):
            f.write(nodeid + "\n")
