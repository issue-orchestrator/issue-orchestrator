"""One owner for authenticated destination binding and supported Git history."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping
from urllib.parse import SplitResult, unquote, urlsplit

from ..adapters.git.git_cli import GIT_ENV_STRIP
from ..domain.exact_git import ExactPushAuthenticationError, ExactPushDestination
from ..ports.git import Git, GitError
from .git_push_operations import GitAuthEnvProvider, prepare_git_auth_env


def require_remote(remote: str) -> None:
    if (
        type(remote) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote) is None
    ):
        raise ValueError("remote must be a configured remote name")


def repository_scope(endpoint: str) -> tuple[str, str]:
    """Compare repository identity across legitimate SSH/HTTPS auth transitions."""
    if endpoint.startswith("/"):
        return "local", str(Path(endpoint).resolve())
    if "://" not in endpoint:
        if "::" in endpoint:
            raise ValueError("exact push does not support remote helpers")
        match = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", endpoint)
        if match is None:
            raise ValueError("unsupported exact push endpoint")
        host, path = match.groups()
    else:
        parsed = urlsplit(endpoint)
        if parsed.scheme == "file":
            return file_repository_scope(parsed)
        if (
            parsed.scheme not in {"https", "ssh"}
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("unsupported exact push endpoint")
        host, path = parsed.hostname, parsed.path
        # Nonstandard authorities must not collapse onto a different service.
        if (
            parsed.port is not None
            and parsed.port != {"ssh": 22, "https": 443}[parsed.scheme]
        ):
            host = f"{host}:{parsed.port}"
    path = path.removeprefix("/").removesuffix(".git")
    if (
        not path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or "%" in path
    ):
        raise ValueError("noncanonical exact push repository path")
    return host.lower(), path


def file_repository_scope(parsed: SplitResult) -> tuple[str, str]:
    """Git decodes file-URL escapes once; literal local paths stay literal."""
    if (
        parsed.netloc not in {"", "localhost"}
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
        or re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path)
    ):
        raise ValueError("noncanonical local file endpoint")
    path = unquote(parsed.path, errors="strict")
    if any(char in path for char in "\0\r\n"):
        raise ValueError("invalid decoded local file endpoint")
    return "local", str(Path(path).resolve())


@dataclass(frozen=True)
class ExactPushContext:
    destination: ExactPushDestination
    environment: Mapping[str, str] = field(repr=False)


class ExactPushContextOwner:
    """Freeze effective auth and endpoint, then recheck supported configuration.

    No hooks or repository configuration are overridden. Arbitrary hostile Git
    configuration mutation after the final check but before Git loads it remains
    outside this boundary; a frozen endpoint is not a configuration lock.
    """

    def __init__(self, git: Git, auth: GitAuthEnvProvider | None) -> None:
        self._git = git
        self._auth = auth

    def prepare(self, repository: Path, remote: str) -> ExactPushContext:
        require_remote(remote)
        ambient = {k: v for k, v in os.environ.items() if k not in GIT_ENV_STRIP}
        try:
            effective = prepare_git_auth_env(self._auth, remote=remote)
        except Exception as exc:
            raise ExactPushAuthenticationError(
                "unable to prepare exact push authentication"
            ) from exc
        env = ambient if effective is None else effective
        self.require_supported(repository, env)
        destination = self._destination(repository, remote, env)
        configured = self._destination(repository, remote, ambient)
        if repository_scope(destination.endpoint) != repository_scope(
            configured.endpoint
        ):
            raise ValueError(
                "authenticated push destination does not match configured repository scope"
            )
        return ExactPushContext(destination, MappingProxyType(dict(env)))

    def recheck(self, repository: Path, remote: str, context: ExactPushContext) -> None:
        env = dict(context.environment)
        self.require_supported(repository, env)
        if self._destination(repository, remote, env) != context.destination:
            raise ValueError("configured push destination changed before publication")

    def _destination(
        self, repository: Path, remote: str, env: dict[str, str]
    ) -> ExactPushDestination:
        urls = self._git.run(
            repository, ["remote", "get-url", "--push", "--all", remote], env=env
        ).stdout.splitlines()
        if len(urls) != 1 or not urls[0]:
            raise ValueError("exact push requires one configured push endpoint")
        endpoint = urls[0]
        if ":" not in endpoint or endpoint.startswith(("/", "./", "../")):
            endpoint = str((repository / endpoint).resolve())
        return ExactPushDestination(endpoint)

    def require_supported(self, repository: Path, env: dict[str, str]) -> None:
        if any(
            name in env
            for name in (*GIT_ENV_STRIP, "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE")
        ):
            raise ValueError(
                "exact push does not support repository or history environment overrides"
            )
        rewrites = self._git.run(
            repository,
            ["config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$"],
            env=env,
            check=False,
        )
        if rewrites.returncode == 0:
            raise ValueError("exact push does not support Git URL rewriting")
        if rewrites.returncode != 1:
            raise GitError(rewrites)
        shallow = self._git.run(
            repository, ["rev-parse", "--is-shallow-repository"], env=env
        ).stdout.strip()
        if shallow != "false":
            raise ValueError("exact push requires complete, non-shallow history")
        graft = self._git.run(
            repository, ["rev-parse", "--git-path", "info/grafts"], env=env
        ).stdout.strip()
        if (repository / graft).exists():
            raise ValueError("exact push does not support graft metadata")
        refs = self._git.run(
            repository,
            [
                "for-each-ref",
                "--format=%(refname)",
                "refs/replace/",
                env.get("GIT_REPLACE_REF_BASE", "refs/replace/"),
            ],
            env=env,
        )
        if refs.stdout.strip():
            raise ValueError("exact push does not support replacement objects")
