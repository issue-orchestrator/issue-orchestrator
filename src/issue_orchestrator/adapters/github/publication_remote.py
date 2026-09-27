"""Strict uncached publication reads over the shared GitHub HTTP boundary."""

import json
from typing import Any
from urllib.parse import urlsplit

from ...domain.exact_git import ExactPushDestination

from ...domain.publication_remote import (
    PrCreateRejection,
    PublicationPrCreateRejected,
    PublicationPullRequest,
    PublicationPrState,
    PublicationRemoteError,
    attributed_publication_body,
)
from ...domain.validated_head_publication import PublishValidatedHeadCommand
from ...domain.validated_work_capture import (
    ValidatedWorkRemoteFacts,
    ValidatedWorkRemoteRequest,
)
from ...domain.validated_work import require_sha
from ...ports.repository_host import RepositoryHostError
from .errors import GitHubHttpError
from .http_client import GitHubHttpClient

# GitHub answers an unprocessable PR create with 422 and, for the refusals the
# publication owner acts on, a stable human message in ``errors[].message``.
_UNPROCESSABLE = 422
_NO_COMMITS = "no commits between"
_ALREADY_EXISTS = "a pull request already exists"


def _pull_request(raw: dict[str, Any]) -> PublicationPullRequest:
    try:
        state = (
            PublicationPrState.MERGED
            if raw.get("merged") or raw.get("merged_at")
            else PublicationPrState(raw["state"])
        )
        return PublicationPullRequest(
            number=raw["number"],
            url=raw["html_url"],
            head_repo=raw["head"]["repo"]["full_name"],
            base_repo=raw["base"]["repo"]["full_name"],
            branch=raw["head"]["ref"],
            base_branch=raw["base"]["ref"],
            head_sha=raw["head"]["sha"],
            state=state,
            body=raw["body"] or "",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PublicationRemoteError("Incomplete publication PR identity") from exc


def _create_rejection(exc: GitHubHttpError) -> PublicationPrCreateRejected | None:
    """Classify a definite PR-create refusal; anything else stays a remote error.

    Only a 422 is a definite refusal. Its subtype is read from the validation
    messages GitHub returns; an unrecognized or unreadable 422 body is still a
    refusal of this exact request, never a transient read failure.
    """
    if exc.status_code != _UNPROCESSABLE:
        return None
    messages: list[str] = []
    try:
        payload = json.loads(exc.response_text or "")
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        errors = payload.get("errors")
        for item in errors if isinstance(errors, list) else ():
            if isinstance(item, dict) and isinstance(item.get("message"), str):
                messages.append(item["message"])
        if isinstance(payload.get("message"), str):
            messages.append(payload["message"])
    folded = " ".join(messages).casefold()
    rejection = (
        PrCreateRejection.NO_COMMITS
        if _NO_COMMITS in folded
        else PrCreateRejection.ALREADY_EXISTS
        if _ALREADY_EXISTS in folded
        else PrCreateRejection.INVALID
    )
    detail = "; ".join(messages) or str(exc)
    return PublicationPrCreateRejected(rejection, f"PR create refused: {detail}")


def _branch_head(client: GitHubHttpClient, branch_name: str) -> str | None:
    raw = client.read_publication_branch(branch_name)
    if raw is None:
        return None
    if raw["ref"] != f"refs/heads/{branch_name}" or raw["object"]["type"] != "commit":
        raise ValueError("Unexpected branch ref identity")
    sha = raw["object"]["sha"]
    require_sha(sha)
    return sha


def _branch_pull_requests(
    client: GitHubHttpClient, branch_name: str
) -> tuple[PublicationPullRequest, ...]:
    return tuple(_pull_request(raw) for raw in client.read_publication_prs(branch_name))


class GitHubValidatedWorkCaptureObserver:
    """Capture one complete uncached branch/PR snapshot without publication intent."""

    def __init__(self, client: GitHubHttpClient, *, repo_slug: str) -> None:
        if client.config.repo != repo_slug:
            raise ValueError("HTTP client repository must match capture repository")
        self._client = client
        self._repo_slug = repo_slug

    def observe(self, request: ValidatedWorkRemoteRequest) -> ValidatedWorkRemoteFacts:
        if request.repo_slug != self._repo_slug:
            raise PublicationRemoteError("Capture repository does not match configured remote")
        try:
            return ValidatedWorkRemoteFacts(
                _branch_head(self._client, request.branch_name),
                _branch_pull_requests(self._client, request.branch_name),
            )
        except (RepositoryHostError, KeyError, TypeError, ValueError) as exc:
            raise PublicationRemoteError(str(exc)) from exc


class GitHubPublicationRemote:
    def __init__(self, client: GitHubHttpClient, *, repo_slug: str) -> None:
        if client.config.repo != repo_slug:
            raise ValueError("HTTP client repository must match publication repository")
        self._client = client
        self._repo_slug = repo_slug

    def _require_repository(self, command: PublishValidatedHeadCommand) -> None:
        if command.repo_slug != self._repo_slug:
            raise PublicationRemoteError(
                "Publication repository does not match configured remote"
            )

    def accepts_push_destination(
        self, command: PublishValidatedHeadCommand, destination: ExactPushDestination
    ) -> bool:
        self._require_repository(command)
        endpoint = destination.endpoint
        if endpoint.startswith("git@") and ":" in endpoint and "://" not in endpoint:
            authority, path = endpoint.split(":", 1)
            endpoint = f"ssh://{authority}/{path}"
        try:
            parsed = urlsplit(endpoint)
            api = urlsplit(self._client.config.base_url)
            expected_host = (
                "github.com" if api.hostname == "api.github.com" else api.hostname
            )
            expected_port = (api.port or 443) if parsed.scheme == "https" else 22
            if (
                parsed.scheme not in {"https", "ssh"}
                or parsed.hostname != expected_host
                or (parsed.port or (443 if parsed.scheme == "https" else 22))
                != expected_port
                or parsed.query
                or parsed.fragment
                or parsed.password is not None
                or (parsed.scheme == "ssh" and parsed.username != "git")
                or (parsed.scheme == "https" and parsed.username is not None)
            ):
                return False
            path = parsed.path.removeprefix("/").removesuffix(".git")
            return path.casefold() == self._repo_slug.casefold()
        except ValueError:
            return False

    def read_branch(self, command: PublishValidatedHeadCommand) -> str | None:
        self._require_repository(command)
        try:
            return _branch_head(self._client, command.branch_name)
        except (RepositoryHostError, KeyError, TypeError, ValueError) as exc:
            raise PublicationRemoteError(str(exc)) from exc

    def read_pr(
        self, command: PublishValidatedHeadCommand, number: int
    ) -> PublicationPullRequest | None:
        self._require_repository(command)
        try:
            raw = self._client.read_publication_pr(number)
            if raw is None:
                return None
            pr = _pull_request(raw)
            if pr.number != number:
                raise PublicationRemoteError(
                    "PR response number does not match request"
                )
            return pr
        except (RepositoryHostError, ValueError, TypeError) as exc:
            raise PublicationRemoteError(str(exc)) from exc

    def list_prs(
        self, command: PublishValidatedHeadCommand
    ) -> tuple[PublicationPullRequest, ...]:
        self._require_repository(command)
        try:
            return _branch_pull_requests(self._client, command.branch_name)
        except (RepositoryHostError, ValueError, TypeError) as exc:
            raise PublicationRemoteError(str(exc)) from exc

    def create_pr(self, command: PublishValidatedHeadCommand) -> PublicationPullRequest:
        self._require_repository(command)
        try:
            raw = self._client.create_pr(
                title=command.content.title,
                body=attributed_publication_body(command.content.body, command.issue_number, command.branch_name),
                head=command.branch_name,
                base=command.pr_base_branch,
                draft=command.content.draft,
            )
            if raw is None:
                raise PublicationRemoteError("PR create response was lost")
            return _pull_request(raw)
        except GitHubHttpError as exc:
            rejected = _create_rejection(exc)
            if rejected is not None:
                raise rejected from exc
            raise PublicationRemoteError(str(exc)) from exc
        except (RepositoryHostError, ValueError, TypeError) as exc:
            raise PublicationRemoteError(str(exc)) from exc
