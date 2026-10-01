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
from .errors import GitHubHttpError, GitHubRateLimitedError
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

    Only a 422 whose body is GitHub's structured validation failure is a
    definite refusal of this exact request. A 422 without that shape (abuse or
    spam throttling, a malformed body) proves nothing permanent and stays an
    untyped remote error, which the attempt budget bounds.
    """
    if exc.status_code != _UNPROCESSABLE or isinstance(exc, GitHubRateLimitedError):
        return None
    try:
        payload = json.loads(exc.response_text or "")
    except ValueError:
        return None
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list) or not errors or not all(
        isinstance(item, dict) and isinstance(item.get("code"), str) for item in errors
    ):
        return None
    messages = [item["message"] for item in errors if isinstance(item.get("message"), str)]
    folded = " ".join(messages).casefold()
    if _NO_COMMITS in folded:
        rejection = PrCreateRejection.NO_COMMITS
    elif _ALREADY_EXISTS in folded:
        rejection = PrCreateRejection.ALREADY_EXISTS
    elif all(isinstance(item.get("field"), str) and item.get("code") != "custom" for item in errors):
        # A field-level refusal (``base``/``head`` invalid or missing) names
        # what is wrong with this exact request. A ``custom`` message that is
        # neither recognized refusal (a throttle, "please wait ...") proves
        # nothing permanent and stays retryable.
        rejection = PrCreateRejection.INVALID
    else:
        return None
    detail = "; ".join(
        messages or [f"{item.get('field', '?')}: {item['code']}" for item in errors]
    )
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

    def merged_pull_requests(
        self, request: ValidatedWorkRemoteRequest
    ) -> tuple[PublicationPullRequest, ...]:
        if request.repo_slug != self._repo_slug:
            raise PublicationRemoteError("Capture repository does not match configured remote")
        try:
            closed = self._client.read_publication_prs(request.branch_name, state="closed")
            return tuple(
                pr for pr in (_pull_request(raw) for raw in closed)
                if pr.state is PublicationPrState.MERGED
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
