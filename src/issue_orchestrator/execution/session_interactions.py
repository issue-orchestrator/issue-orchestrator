"""Rule-based prompt-response helpers for running PTY-backed sessions."""

from __future__ import annotations

import logging
import re
import shlex
import threading
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from ..infra.terminal_viewport import DEFAULT_COLS, DEFAULT_ROWS, TerminalViewport

logger = logging.getLogger(__name__)

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_WHITESPACE_RE = re.compile(r"\s+")
_SHELL_COMMAND_SEPARATORS = frozenset({"&&", ";", "||"})
# Quiet period before answering a TUI prompt. Measured against codex-cli
# 0.156.1: an Enter sent on the frame that drew the prompt was dropped every
# time; 0.3 s after it, accepted every time.
_TUI_SETTLE_SECONDS = 0.5
# Both Claude Code and Codex show this while the agent is working, and never
# before startup prompts are done. Past it, a startup rule must stay silent.
_AGENT_WORKING_MARKERS = ("esc to interrupt",)
# Senders end every answer with a carriage return (Enter).
_DOWN_ARROW = "\x1b[B"


def normalize_terminal_text(text: str) -> str:
    """Collapse terminal control noise into a stable search buffer.

    Public because the review-exchange kill-evidence discriminator matches the
    same normalized shape (ANSI/OSC stripped, whitespace collapsed, casefolded)
    against recording tails; both surfaces must agree on what a marker looks
    like or the same TUI footer would match one and not the other.
    """
    if not text:
        return ""
    text = _OSC_ESCAPE_RE.sub(" ", text)
    text = _ANSI_ESCAPE_RE.sub(" ", text)
    text = text.replace("\r", " ").replace("\n", " ")
    text = _WHITESPACE_RE.sub(" ", text)
    return text.casefold().strip()


@dataclass(frozen=True)
class InteractionVariant:
    """One way a prompt is drawn, and the keys that answer THAT drawing."""

    required_substrings: tuple[str, ...]
    response: str

    def __post_init__(self) -> None:
        if not self.required_substrings:
            raise ValueError("InteractionVariant.required_substrings cannot be empty")


@dataclass(frozen=True)
class SessionInteractionRule:
    """One deterministic prompt-response rule."""

    name: str
    required_substrings: tuple[str, ...]
    response: str
    # Reserved for future cooldown/edge-trigger semantics; current rules are one-shot only.
    fire_once: bool = True
    # Other ways the SAME prompt is drawn (across versions, or with a
    # different option highlighted), each with the keys that answer it. Any
    # one complete set matches; the rule still fires once, so a startup wait
    # for "every rule fired" is not left waiting on a variant this version
    # never draws.
    alternatives: tuple["InteractionVariant", ...] = ()
    # Answer only after the screen has been quiet this long. A TUI can drop a
    # key that arrives while it is still drawing the prompt (codex 0.156 does);
    # any further output restarts the wait.
    settle_seconds: float = 0.0
    # Markers that mean startup is over (the agent is working). Seeing one
    # disarms the rule for good: later agent output that merely QUOTES the
    # prompt's text must never get an answer typed into the live session.
    expires_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.required_substrings or any(
            type(v) is not InteractionVariant for v in self.alternatives
        ):
            raise ValueError("SessionInteractionRule needs markers and typed variants")
        if not self.fire_once:
            raise ValueError("SessionInteractionRule only supports fire_once=True")
        if self.settle_seconds < 0:
            raise ValueError("SessionInteractionRule.settle_seconds cannot be negative")


@dataclass(frozen=True)
class _CompiledRule:
    rule: SessionInteractionRule
    # (markers, response) in precedence order: the first complete match wins.
    variants: tuple[tuple[tuple[str, ...], str], ...]
    expiry_markers: tuple[str, ...]

    def expired_by(self, buffer: str) -> bool:
        return any(marker in buffer for marker in self.expiry_markers)

    def response_for(self, buffer: str) -> str | None:
        """The keys that answer the prompt as drawn, or None if it is not drawn."""
        for markers, response in self.variants:
            if markers and all(m in buffer for m in markers):
                return response
        return None


def _compile_markers(substrings: Sequence[str]) -> tuple[str, ...]:
    return tuple(m for m in (normalize_terminal_text(item) for item in substrings) if m)


class _Timer(Protocol):
    def start(self) -> None: ...
    def cancel(self) -> None: ...


TimerFactory = Callable[[float, Callable[[], None]], _Timer]


def _thread_timer(seconds: float, callback: Callable[[], None]) -> _Timer:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    return timer


class SessionInteractionHandler:
    """Matches terminal output against rules and sends line-based responses."""

    def __init__(
        self,
        *,
        session_name: str,
        rules: Sequence[SessionInteractionRule],
        timer_factory: TimerFactory = _thread_timer,
    ) -> None:
        self._session_name = session_name
        # What is on the terminal NOW. Rules match this, never the history of
        # everything printed: a repainted prompt must not leave its old
        # highlight behind, and a chunk boundary must not split a glyph or a
        # word (#7299 review round 3).
        self._viewport = TerminalViewport(rows=DEFAULT_ROWS, cols=DEFAULT_COLS)
        self._sender: Callable[[str], bool] | None = None
        self._fired_rules: set[str] = set()
        self._timer_factory = timer_factory
        # Matched rules waiting for the screen to settle, keyed by rule name.
        self._settling: dict[str, _Timer] = {}
        # Rules that will never answer again: startup ended before they fired.
        self._expired: set[str] = set()
        self._lock = threading.RLock()
        self._rules = tuple(
            _CompiledRule(
                rule=rule,
                variants=(
                    *(
                        (_compile_markers(v.required_substrings), v.response)
                        for v in rule.alternatives
                    ),
                    (_compile_markers(rule.required_substrings), rule.response),
                ),
                expiry_markers=_compile_markers(rule.expires_on),
            )
            for rule in rules
        )

    def set_geometry(self, *, rows: int, cols: int) -> None:
        """Match the PTY the session was spawned with, before it prints.

        Cursor addressing only lands on the right rows at the real size.
        """
        with self._lock:
            self._viewport = TerminalViewport(rows=rows, cols=cols)

    def _screen(self) -> str:
        """The written rows of the current screen, as one normalized line."""
        return normalize_terminal_text(" ".join(self._viewport.render().written_rows))

    def bind_sender(self, sender: Callable[[str], bool]) -> None:
        """Attach a line-oriented sender once the PTY session exists."""
        self._sender = sender

    def disarm(self) -> None:
        """End the startup window: no unfired rule may answer from now on.

        Called once the caller has finished waiting for startup prompts and
        is about to drive the session itself. A pending (settling) answer is
        cancelled, not sent.
        """
        with self._lock:
            for compiled in self._rules:
                self._expire(compiled.rule.name)

    def _expire(self, name: str) -> None:
        if name in self._fired_rules:
            return
        self._expired.add(name)
        timer = self._settling.pop(name, None)
        if timer is not None:
            timer.cancel()

    @property
    def all_rules_fired(self) -> bool:
        """Whether every configured one-shot rule has fired."""
        return all(compiled.rule.name in self._fired_rules for compiled in self._rules)

    def on_output(self, data: bytes | str) -> None:
        """Apply PTY output to the screen, then fire (or start settling) rules."""
        if not data:
            return
        raw = data if isinstance(data, bytes) else data.encode("utf-8")
        with self._lock:
            # Any output, even a pure redraw, means the screen is not settled.
            for name in list(self._settling):
                self._restart_settle(self._compiled(name))
            self._viewport.feed(raw)
            self._scan_rules(self._screen())

    def _scan_rules(self, screen: str) -> None:
        """Expire rules whose startup is over, then answer (or settle) matches."""
        for compiled in self._rules:
            if compiled.expired_by(screen):
                self._expire(compiled.rule.name)
        for compiled in self._rules:
            rule = compiled.rule
            if rule.name in self._fired_rules | self._expired | self._settling.keys():
                continue
            response = compiled.response_for(screen)
            if response is None:
                continue
            if rule.settle_seconds > 0:
                self._restart_settle(compiled)
            else:
                self._respond(rule, response)

    def _compiled(self, name: str) -> _CompiledRule:
        return next(compiled for compiled in self._rules if compiled.rule.name == name)

    def _restart_settle(self, compiled: _CompiledRule) -> None:
        rule = compiled.rule
        previous = self._settling.get(rule.name)
        if previous is not None:
            previous.cancel()
        timer = self._timer_factory(rule.settle_seconds, lambda: self._settled(compiled, timer))
        self._settling[rule.name] = timer
        timer.start()

    def _settled(self, compiled: _CompiledRule, timer: _Timer) -> None:
        rule = compiled.rule
        with self._lock:
            if self._settling.get(rule.name) is not timer or rule.name in self._expired:
                return  # superseded by later output, or startup already ended
            del self._settling[rule.name]
            # Choose the keys from the SETTLED screen: an earlier, half-drawn
            # frame may not yet show which option is highlighted.
            response = compiled.response_for(self._screen())
            if response is not None:
                self._respond(rule, response)

    @property
    def answer_pending(self) -> bool:
        """Whether a matched prompt is still waiting for the screen to settle."""
        with self._lock:
            return bool(self._settling)

    def _respond(self, rule: SessionInteractionRule, response: str) -> None:
        sender = self._sender
        if sender is None:
            logger.warning(
                "[session-interactions] matched rule before sender was ready: session=%s rule=%s",
                self._session_name,
                rule.name,
            )
            return
        sent = sender(response)
        logger.info(
            "[session-interactions] rule fired: session=%s rule=%s sent=%s response=%s",
            self._session_name,
            rule.name,
            sent,
            f"{response!r}+<enter>" if response else "<enter>",
        )
        if sent and rule.fire_once:
            self._fired_rules.add(rule.name)


def builtin_session_interaction_rules(command: str) -> tuple[SessionInteractionRule, ...]:
    """Return built-in rules that apply to a specific session command.

    This intentionally targets the raw interactive Claude launch shape that the
    subprocess plugin receives from SessionLauncher, plus the interactive Codex
    launch shape used by persistent review exchange. It accepts leading shell
    environment assignments such as ``FOO=bar && claude ...``. It assumes those
    shell separators are whitespace-delimited, which matches the orchestrator's
    SessionLauncher command shape.
    """
    rules: list[SessionInteractionRule] = []
    if _looks_like_claude_command(command):
        rules.append(
            SessionInteractionRule(
                name="claude-trust-worktree",
                # Answer only a drawing that shows WHICH option is highlighted:
                # Claude Code 2.1.283 lists "No, exit" first and highlighted,
                # so a blind Enter quits the session (measured). An unknown
                # layout gets no answer: a stalled session is visible, a
                # silent exit is not.
                required_substrings=(
                    "Quick safety check",
                    "❯ Yes, I trust this folder",
                ),
                response="",
                alternatives=(
                    InteractionVariant(
                        ("Quick safety check", "❯ No, exit Yes, I trust this folder"),
                        _DOWN_ARROW,
                    ),
                    # Earlier versions numbered the list, "Yes" first.
                    InteractionVariant(
                        ("Quick safety check", "❯ 1. Yes, I trust this folder"), ""
                    ),
                ),
                settle_seconds=_TUI_SETTLE_SECONDS,
                expires_on=_AGENT_WORKING_MARKERS,
            ),
        )
    if _looks_like_interactive_codex_command(command):
        rules.append(
            SessionInteractionRule(
                name="codex-trust-worktree",
                required_substrings=(
                    "Do you trust the contents of this directory?",
                    "Yes, continue",
                    "No, quit",
                ),
                # Codex 0.156 draws this instead for a folder io registers as
                # untrusted on purpose. Its highlighted default, "1. Open
                # restricted", runs with the folder's config, hooks and exec
                # policies disabled, which is the posture io asks for. Left
                # unanswered it blocked every reviewer before its first
                # prompt (#7287).
                alternatives=(InteractionVariant(("Folder access", "Open restricted"), ""),),
                response="",
                # 0.156 drops a key that arrives while it is drawing the choice.
                settle_seconds=_TUI_SETTLE_SECONDS,
                expires_on=_AGENT_WORKING_MARKERS,
            ),
        )
    return tuple(rules)


def _looks_like_claude_command(command: str) -> bool:
    return _claude_command_tokens(command) is not None


def _looks_like_interactive_codex_command(command: str) -> bool:
    tokens = _codex_command_tokens(command)
    return tokens is not None and _is_codex_interactive_command_tokens(tokens)


def _claude_command_tokens(command: str) -> list[str] | None:
    """Extract the whitespace-delimited Claude command segment from a shell command."""
    return _matching_command_tokens(command, _is_claude_command_tokens)


def _codex_command_tokens(command: str) -> list[str] | None:
    """Extract the whitespace-delimited Codex command segment from a shell command."""
    return _matching_command_tokens(command, _is_codex_command_tokens)


def _matching_command_tokens(
    command: str,
    predicate: Callable[[Sequence[str] | None], bool],
) -> list[str] | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    current: list[str] = []
    for token in tokens:
        if token in _SHELL_COMMAND_SEPARATORS:
            command_tokens = _trim_command_prefix(current)
            if predicate(command_tokens):
                return command_tokens
            current = []
            continue
        current.append(token)

    command_tokens = _trim_command_prefix(current)
    if predicate(command_tokens):
        return command_tokens
    return None


def _trim_command_prefix(tokens: Sequence[str]) -> list[str] | None:
    """Strip the wrappers a launcher puts in front of the real executable.

    ``exec`` and bare ``FOO=bar`` assignments were handled from the start. The
    literal ``env`` COMMAND was not, and that is how the codex rules quietly
    stopped applying: the provider runs codex through an isolated home as

        env CODEX_HOME=<runtime> codex ...

    so ``tokens[0]`` is ``env``, the executable never matched ``codex``, and
    every codex rule — including the trust-worktree prompt — was silently
    inert. Nothing failed loudly, because a rule that never matches just never
    fires.
    """
    trimmed = list(tokens)
    while trimmed:
        head = trimmed[0]
        if head == "exec" or _looks_like_env_assignment(head):
            trimmed = trimmed[1:]
            continue
        # `env` and `/usr/bin/env`, whose own flags we never generate; a flag
        # we do not understand stops the trim rather than guessing past it.
        if head.rsplit("/", 1)[-1] == "env":
            trimmed = trimmed[1:]
            continue
        break
    return trimmed or None


def _is_claude_command_tokens(tokens: Sequence[str] | None) -> bool:
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1]
    return executable == "claude"


def _is_codex_command_tokens(tokens: Sequence[str] | None) -> bool:
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1]
    return executable == "codex"


_CODEX_SUBCOMMANDS = frozenset(
    {
        "exec",
        "e",
        "review",
        "login",
        "logout",
        "mcp",
        "plugin",
        "mcp-server",
        "app-server",
        "remote-control",
        "app",
        "completion",
        "update",
        "doctor",
        "sandbox",
        "debug",
        "apply",
        "a",
        "resume",
        "archive",
        "delete",
        "unarchive",
        "fork",
        "cloud",
        "exec-server",
        "features",
        "help",
    }
)
_CODEX_OPTIONS_WITH_VALUES = frozenset(
    {
        "-a",
        "--add-dir",
        "--ask-for-approval",
        "-c",
        "--cd",
        "-C",
        "-i",
        "--image",
        "-m",
        "--model",
        "-p",
        "--profile",
        "--remote",
        "--remote-auth-token-env",
        "-s",
        "--sandbox",
        "--local-provider",
    }
)


def _is_codex_interactive_command_tokens(tokens: Sequence[str]) -> bool:
    if not _is_codex_command_tokens(tokens):
        return False
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            return True
        if token in _CODEX_SUBCOMMANDS:
            return False
        if token.startswith("--") and "=" in token:
            continue
        if token in _CODEX_OPTIONS_WITH_VALUES:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return True
    return True


def _looks_like_env_assignment(token: str) -> bool:
    """Whether a token is a ``NAME=value`` prefix rather than the executable.

    The test belongs to the NAME. Rejecting any token containing ``/`` also
    rejected every assignment whose value is a path, which is most of them —
    `CODEX_HOME=/Users/...` is the one the codex provider actually emits, and
    it is why the codex interaction rules matched nothing. The slash guard was
    there to stop a bare path like ``/usr/bin/foo`` being read as an
    assignment; requiring the name to be a shell identifier does that job
    exactly, and a path has no ``=`` before its first slash anyway.
    """
    key, sep, _ = token.partition("=")
    if not sep or token.startswith("-"):
        return False
    return key.isidentifier()
