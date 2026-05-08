# Artifact Contracts Plan

## Problem

Session artifacts are currently identified too late and too loosely. A `run_dir`
is a storage container, but several downstream layers treat it as if it answers
"which artifact should this UI action open?" That creates a broad class of bugs:

- A reviewer timeline row can open an aggregate run recording.
- Missing role recordings can be masked by nearby files.
- Long-lived persistent pairs can stay alive after their pair-scoped recording
  paths have been removed.
- UI controls can manufacture artifact actions from partial context.

The durable fix is to make workflow outputs typed and producer-owned. Later code
should consume named artifacts and workflow aggregates, not rediscover files from
untyped directories.

## Target Invariant

Every UI artifact action is a projection of a typed artifact reference.

The pipeline should look like this:

```text
producer constructs typed artifact bundle
  -> constructor validates required evidence
  -> timeline records typed artifact refs
  -> UI renders typed artifact actions
  -> endpoint opens exactly that artifact
```

The pipeline should not look like this:

```text
producer writes files into run_dir
  -> later code guesses from run_dir and event name
  -> UI labels imply role/session intent
  -> route falls back to another file
```

`run_dir` remains useful metadata and storage location. It is not artifact
identity.

## Design Principles

1. Prefer required constructor arguments over optional fields.
2. Model state transitions with different types instead of nullable fields.
3. Validate filesystem facts at producer boundaries.
4. Represent missing required evidence as a named contract failure.
5. Do not substitute another artifact for a missing required artifact.
6. Keep provider-specific capture details behind stable workflow types.

## Proposed Domain Shapes

Core primitives:

- `IssueNumber`
- `ExchangeRunId`
- `PositiveRoundIndex`
- `AgentRole`
- `AgentProvider`
- `ExistingFile`
- `ExistingDirectory`
- `ArtifactScope`
- `RenderMode`

Artifact references:

- `PromptArtifact`
- `TerminalRecordingArtifact`
- `ChapterSidecarArtifact`
- `CompletionRecordArtifact`
- `ValidationResultArtifact`
- `ReviewResponseArtifact`
- `ReviewExchangeSummaryArtifact`

Workflow aggregates:

- `PromptBundle`
- `AgentSessionRef`
- `CoderRunStarted`
- `CoderRunCompleted`
- `ReviewerTurnStarted`
- `ReviewerTurnCompleted`
- `ReviewExchangeStarted`
- `ReviewExchangeCompleted`
- `CodingCycle`
- `PublishAttempt`

Provider-specific details should usually be modeled inside session or capture
types, not by subtyping the workflow itself:

- `CodexSession`
- `ClaudeCodeSession`
- `TerminalReplayCapture`
- `CodexJsonStreamCapture`
- `ClaudeSessionLogArtifact`

## Reviewer Turn Contract

A reviewer turn starts only after the producer can construct:

```text
ReviewerTurnStarted
  issue_number
  exchange_run_id
  round_index
  input_spec
  prompt
  reviewer_session
  terminal_recording
  chapters
```

A reviewer turn completes only after the producer can construct:

```text
ReviewerTurnCompleted
  started
  response_artifact
  parsed_response
```

Before response, there is no nullable response field. The system holds
`ReviewerTurnStarted`. After response, it holds `ReviewerTurnCompleted`.

## PR Stack

### PR 1: Stop False Artifact Substitution

Purpose:

- Stop presenting aggregate run recordings as reviewer recordings.
- Stop the issue-detail artifact popover from inventing run-level session links.
- Detect cached persistent pairs whose recording paths are gone and respawn the
  pair instead of creating a dead replacement path.
- Add this plan as the target contract for the stack.

Review focus:

- No UI action should silently switch from a role recording to another file.
- A missing required recording should surface as unavailable or fail at producer
  boundary.

### PR 2: Typed Artifact Primitives

Purpose:

- Add domain-level artifact primitives and required constructors.
- Add tests that missing required constructor parameters and missing filesystem
  evidence fail immediately.
- Do not wire behavior yet except for pure conversion helpers.

Review focus:

- Types are small, named, and not tied to UI implementation.
- `run_dir` appears as storage metadata only, not artifact identity.

### PR 3: Reviewer Turn Producer Contract

Purpose:

- Make persistent review exchange construct `ReviewerTurnStarted` and
  `ReviewerTurnCompleted` at the producer boundary.
- Emit typed artifact refs with role prompt/feedback events.
- Fail with a named contract violation if required reviewer evidence is absent.

Review focus:

- The reviewer prompt, recording, chapter sidecar, and response are not optional
  when the state type says they exist.
- Tests cover missing evidence at construction time, not only at UI lookup time.

### PR 4: Typed Timeline Actions

Purpose:

- Introduce typed `OpenArtifactAction` projection from artifact refs.
- Make timeline presentation consume typed artifact refs instead of event-name
  plus `run_dir` inference.
- Keep legacy actions only behind explicit migration adapters with tests.

Review focus:

- UI receives typed action payloads.
- Action builders no longer guess reviewer/coder role from labels or event names.

### PR 5: Artifact Endpoint Contract

Purpose:

- Replace partial query scoping with artifact-ref based endpoint resolution.
- Endpoints open exactly the artifact identity provided by the action.
- Remove route-level fallback to aggregate recordings for scoped role requests.

Review focus:

- Missing artifacts return explicit contract failures.
- Endpoints do not substitute another file.

### PR 6: Extend to Coder/Rework/Publish

Purpose:

- Apply the same typed artifact-bundle pattern to coder runs, rework turns,
  validation, and publish attempts.
- Remove remaining `run_dir`-as-identity call sites.

Review focus:

- Every workflow phase has known inputs and outputs.
- No new optional "maybe artifact" fields are introduced where state-specific
  types would be clearer.

## Migration Rule

During migration, compatibility adapters may read legacy manifests, but they
must produce one of these outcomes:

- typed artifact available
- typed artifact missing with reason
- not applicable for this workflow state

They must not return a different artifact as a fallback.

## Test Strategy

- Unit tests for constructor and filesystem validation.
- Producer tests for review exchange role prompt/feedback contracts.
- Timeline action tests for exact artifact-ref projection.
- Session route tests for exact artifact opening and explicit missing responses.
- Dashboard guardrail tests that UI renders backend-provided action refs only.

