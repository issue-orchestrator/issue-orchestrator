"""The pre-push ref-line contract shared by every generated pre-push hook.

Git feeds a pre-push hook one line per ref on stdin::

    <local ref> <local sha> <remote ref> <remote sha>

A push that only deletes remote refs pushes no code, so io's validation gate
has nothing to validate. Running it anyway is not just slow: on porchpin a
``git push origin --delete <branch>`` ran the full ``validate.sh`` suite in the
main checkout and Node ran out of heap (#7765).

This module owns the one decision, ``push_is_delete_only``, and the one way to
read the ref lines, ``capture_push_refs``. Both are rendered verbatim into the
managed repo wrapper (``repo_guardrails._render_repo_pre_push_hook``), the
worktree chained wrapper and the bundled orchestrator hook
(``adapters/worktree/_worktree_hooks``), so the rule cannot drift by path.

The decision fails closed: the gate is skipped only when there is at least one
ref line and every line is a well-formed deletion. Empty stdin, a blank line,
an extra field, a non-zero local sha or anything else unparseable runs the
gate.

Git writes the ref lines once, so a wrapper that runs several consumers
(project hook, verify-pr, post-verify, the orchestrator hook) captures stdin
into ``$PUSH_REFS_FILE`` first and replays that file to each consumer.
``capture_push_refs`` never reads a terminal: a hook run by hand, or by a
process whose stdin is a TTY, would otherwise stop on SIGTTIN or wait for
input. Such a run sees no ref lines and therefore runs the gate.
"""

from __future__ import annotations

DELETE_ONLY_SKIP_REASON = "delete-only"

# Kept as a module constant (not inline in an f-string template) so its shell
# braces are literal rather than format fields. It must not contain the managed
# pre-push marker: doctor treats a file containing that marker as the wrapper.
PRE_PUSH_REFS_SHELL = """\
# --- issue-orchestrator pre-push refs (generated from infra/hooks/pre_push_refs.py) ---
PUSH_REFS_FILE=""

# Read git's ref lines once so every consumer can be handed the same input.
capture_push_refs() {
  PUSH_REFS_FILE="$(mktemp "${TMPDIR:-/tmp}/io-pre-push-refs.XXXXXX")"
  trap 'rm -f "$PUSH_REFS_FILE"' EXIT
  if [ -t 0 ]; then
    return 0
  fi
  cat > "$PUSH_REFS_FILE"
}

# Succeeds only when there is at least one ref line and every line deletes a
# remote ref. Anything else (no lines, a blank or malformed line, any update)
# fails, so the caller runs its gate.
push_is_delete_only() {
  local refs_file="$1"
  local ref_lines=0
  local local_ref="" local_sha="" remote_ref="" remote_sha="" extra=""
  local zero_sha='^(0{40}|0{64})$'
  local object_sha='^([0-9a-f]{40}|[0-9a-f]{64})$'
  while read -r local_ref local_sha remote_ref remote_sha extra || [ -n "$local_ref" ]; do
    ref_lines=$((ref_lines + 1))
    [ "$local_ref" = "(delete)" ] || return 1
    [ -z "$extra" ] || return 1
    [[ "$local_sha" =~ $zero_sha ]] || return 1
    [[ "$remote_ref" == refs/* ]] || return 1
    [[ "$remote_sha" =~ $object_sha ]] || return 1
  done < "$refs_file"
  [ "$ref_lines" -gt 0 ]
}
# --- end issue-orchestrator pre-push refs ---
"""


def pre_push_refs_shell() -> str:
    """Return the shell functions every generated pre-push hook embeds."""
    return PRE_PUSH_REFS_SHELL
