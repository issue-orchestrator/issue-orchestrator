"""The execenv image pins the whole HTCondor package family, not one package.

`htcondor` depends on `condor (= <exact>)`, and apt resolves that
dependency against condor's CANDIDATE version (the newest the repo
serves) without backtracking. Pinning only `htcondor` therefore broke
the image build the day htcondor.org published the next 24.0.x point
release. Every package of the family must be pinned to the one
`CONDOR_PACKAGE_VERSION` build arg.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "docker" / "execenv" / "Dockerfile"

# The condor source package's binaries that the install resolves:
# the metapackage, the package it depends on by exact version, and that
# package's unversioned same-source dependency.
CONDOR_FAMILY = ("htcondor", "condor", "condor-upgrade-checks")


def _dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_condor_version_arg_is_one_exact_debian_version() -> None:
    args = re.findall(r"^ARG CONDOR_PACKAGE_VERSION=(\S+)$", _dockerfile_text(), re.M)
    assert len(args) == 1, args
    assert re.fullmatch(r"\d+\.\d+\.\d+-\d+\+ubu\d+", args[0]), args[0]


def test_every_condor_family_package_is_pinned_to_the_build_arg() -> None:
    text = _dockerfile_text()
    for package in CONDOR_FAMILY:
        pinned = f'"{package}=${{CONDOR_PACKAGE_VERSION}}"'
        assert text.count(pinned) == 1, f"{package} must be pinned exactly once as {pinned}"

