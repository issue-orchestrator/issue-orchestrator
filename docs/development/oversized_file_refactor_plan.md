# Oversized File Refactor Plan

This tracks the large-file refactor effort. The goal is to reduce obvious scale debt by extracting logical seams only: route families, behavior owners, adapters, and test fixtures that already have domain boundaries.

## Size Bands

- Critical: over 1,500 lines. These files should be split first.
- Watch: 1,001-1,500 lines. These files should be considered after the critical band or when touched by a related seam.
- Entry point ideal: 600-900 lines for controllers and web surfaces where feasible.

## Source Targets

Snapshot: planning counts captured on 2026-04-16 before the #5886-#5894 oversized-file stack. Counts are prioritization input, not an assertion about current `main` after any subset of the stack lands.

| Lines | File | Primary seam candidates |
| ---: | --- | --- |
| 2,824 | `src/issue_orchestrator/control/session_launcher.py` | launch preconditions, claim handling, environment preparation, terminal startup, review/rework launch policies |
| 2,799 | `src/issue_orchestrator/entrypoints/web.py` | dashboard/read-model APIs, dialog APIs, operator actions, logs, history/retry/bulk actions, settings/create-issue surfaces |
| 2,628 | `src/issue_orchestrator/entrypoints/cli.py` | command families, lifecycle commands, diagnostics, completion helpers, setup/e2e commands |
| 2,529 | `src/issue_orchestrator/control/completion_processor.py` | completion parsing, validation, outcome application, review/rework transitions, failure reporting |
| 2,517 | `src/issue_orchestrator/infra/config.py` | config discovery/loading, validation, defaults, serialization, migration helpers |
| 2,064 | `src/issue_orchestrator/entrypoints/control_api.py` | shutdown routes/state, goal-pilot routes, tool action routes, dashboard compatibility APIs, issue debug/detail APIs |
| 2,006 | `src/issue_orchestrator/infra/e2e_db.py` | schema setup, run persistence, test result persistence, queries/read models, cleanup |
| 1,856 | `src/issue_orchestrator/adapters/worktree/_worktree.py` | branch naming, clone/reuse, cleanup, metadata, recovery |
| 1,788 | `src/issue_orchestrator/control/completion_handler.py` | coding completion, reviewer completion, blocked/needs-human handling, PR resolution |
| 1,774 | `src/issue_orchestrator/infra/hooks/hooks.py` | hook installation, validation, policy checks, shell integration |
| 1,607 | `src/issue_orchestrator/entrypoints/cli_tools/setup_wizard.py` | prompt generation, config writing, GitHub labels, interactive presentation |
| 1,562 | `src/issue_orchestrator/adapters/github/http_client.py` | auth/session setup, request execution, pagination, rate-limit handling, error translation |
| 1,507 | `src/issue_orchestrator/infra/settings_schema.py` | schema sections, validation metadata, docs generation inputs |
| 1,498 | `src/issue_orchestrator/execution/session_output_adapter.py` | terminal capture, output normalization, persistence, replay metadata |
| 1,480 | `src/issue_orchestrator/view_models/dashboard.py` | repo summary, issue rows, queue state, status/read-model assembly |
| 1,468 | `src/issue_orchestrator/control/planner.py` | observation normalization, action selection, dependency rules, review/rework plans |
| 1,441 | `src/issue_orchestrator/adapters/github/github_adapter.py` | issue operations, PR operations, label operations, review operations |
| 1,362 | `src/issue_orchestrator/control/review_exchange_loop.py` | exchange discovery, reviewer launch, rework launch, cycle limits |
| 1,299 | `src/issue_orchestrator/domain/models.py` | session records, issue records, completion records, enum/value objects |
| 1,187 | `src/issue_orchestrator/control/action_applier.py` | action dispatch, label transitions, queue mutations, event emission |
| 1,174 | `src/issue_orchestrator/control/orchestrator_support.py` | support services, issue refresh helpers, status aggregation |
| 1,166 | `src/issue_orchestrator/view_models/issue_detail.py` | timeline detail, diagnostics, artifacts, phase summaries |
| 1,122 | `src/issue_orchestrator/execution/review_exchange_local_loop.py` | local loop discovery, state machine transitions, completion handoff |
| 1,006 | `src/issue_orchestrator/control/session_controller.py` | session lifecycle state, terminal actions, cancellation/cleanup |
| 995 | `src/issue_orchestrator/entrypoints/bootstrap.py` | composition sub-builders, adapter factory wiring, runtime service wiring |
| 906 | `src/issue_orchestrator/execution/goal_pilot_store.py` | persistence schema, journey operations, action queue operations |

## Test Targets

| Lines | File | Primary seam candidates |
| ---: | --- | --- |
| 7,065 | `tests/unit/test_web.py` | route family test modules, shared web fixtures, dialog/view-model assertions |
| 4,154 | `tests/unit/test_control_api.py` | route family test modules, shutdown route tests, e2e route tests, setup route tests |
| 3,249 | `tests/unit/test_config.py` | discovery/loading tests, validation tests, schema/default tests |
| 3,200 | `tests/unit/test_orchestrator.py` | lifecycle tests, refresh tests, review/rework tests, event tests |
| 2,481 | `tests/unit/test_completion_handler.py` | coding completion, review completion, blocked/needs-human paths |
| 2,469 | `tests/unit/test_planner.py` | dependency planning, review planning, failure planning |
| 2,183 | `tests/unit/test_session_launcher.py` | launch preconditions, worktree setup, terminal startup, review/rework launch |
| 2,122 | `tests/unit/test_completion_processor.py` | parser/validator/outcome test families |
| 1,927 | `tests/unit/test_e2e_timeline_convergence.py` | timeline builders, convergence assertions, fixture builders |
| 1,906 | `tests/unit/test_worktree.py` | clone/reuse/cleanup/recovery test families |
| 1,896 | `tests/unit/test_cli.py` | command family test modules and shared CLI runner fixtures |
| 1,793 | `tests/unit/test_orchestrator_support.py` | support service test families |
| 1,702 | `tests/unit/test_agent_done.py` | coding-done/reviewer-done shared core tests |
| 1,520 | `tests/unit/test_setup_wizard.py` | config generation, prompts, labels, interactive paths |
| 1,514 | `tests/unit/test_hooks.py` | hook install, policy, shell integration test families |

## Planned Stack

1. Extract `control_api.py` shutdown state and `/control/shutdown*` routes.
2. Extract `control_api.py` goal-pilot routes.
3. Extract `control_api.py` tool/action and residual control-center route families until the file is below 1,500 lines.
4. Extract `web.py` dashboard/read-model/dialog/status routes.
5. Extract `web.py` operator actions, logs, history/retry/bulk actions, settings, and create-issue surfaces until the file is below 1,000 lines.
6. Split visible command surfaces: `cli.py` and `setup_wizard.py`.
7. Split large behavior modules: `session_launcher.py`, `completion_processor.py`, `completion_handler.py`, and `planner.py`.
8. Split large infrastructure/adapters: `config.py`, `e2e_db.py`, `_worktree.py`, `hooks.py`, `http_client.py`, and `github_adapter.py`.
9. Split companion unit tests along the same route/behavior seams when test size or fixture coupling blocks maintainability.

## Guardrails

- Preserve behavior; each PR should be mostly movement plus direct import/dependency rewiring.
- Prefer owner abstractions over compatibility re-export layers.
- Use FastAPI `Depends()` for extracted route dependencies rather than module-level service locators.
- Keep reviewable PR boundaries: one route family, behavior owner, adapter concern, or fixture family at a time.
