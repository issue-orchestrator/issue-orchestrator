"""Protected destinations shared by completion policy and exact publication."""


def is_protected_publication_branch(branch: str) -> bool:
    return branch in ("main", "master")


def require_short_publication_branch(branch: str) -> None:
    if (
        type(branch) is not str
        or not branch
        or branch == "@"
        or branch.startswith(("-", "refs/"))
        or branch.endswith((".", "/"))
        or any(
            char.isspace() or ord(char) < 32 or char in "~^:?*[\\"
            for char in branch
        )
        or ".." in branch
        or "@{" in branch
        or any(
            not part or part.startswith(".") or part.endswith(".lock")
            for part in branch.split("/")
        )
    ):
        raise ValueError("publication requires valid short branch names")
