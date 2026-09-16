// Typed-Command surface tests for the dialog action buttons
// (issue #6327).
//
// The validation / diagnostics dialogs render their action buttons through the
// shared ``data-lifecycle-command`` pipeline.  The backend dialog view models
// (``view_models/dialog_commands.py``) emit typed ``DialogActionCommand``
// payloads directly, so the JS renderer renders the provided command instead of
// reconstructing one from a loose ``action.type`` dict.  Each action is
// ``{ command, group }``.
//
// This file covers BOTH sides of the boundary at the JS-vm layer
// (see tests/AGENTS.md — the preferred middle layer):
//
//   A. Renderer — feed a ``{ command, group }`` action to the real
//      ``_renderDialogActionButton`` and assert the button carries the
//      EXACT command in ``data-lifecycle-command`` plus the right label +
//      affordance glyph (keyed off ``command.kind``).
//   B. Consumer — feed each Command kind to the real ``runLifecycleCommand``
//      dispatcher (with handler spies) and assert the right handler ran with
//      the right args (including the ``inline`` error surface the dialog uses).
//   C. Round-trip — render, extract, dispatch: proves the rendered Command
//      JSON agrees with what the dispatcher expects.

const test = require('node:test');
// Non-strict assert: vm.runInContext objects have cross-realm prototypes.
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { uiContractJson } = require('./ui_contract_test_support.js');

const DASHBOARD_JS_DIR = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard',
);

function _readJs(relative) {
    return fs.readFileSync(path.join(DASHBOARD_JS_DIR, relative), 'utf8');
}

function _baseStubs() {
    return {
        console,
        uiContractJson,
        URLSearchParams,
        escapeHtml: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        _humanizeSnakeCase: (s) => String(s || '')
            .split('_').map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(' '),
        showToast: () => {},
        formatTimestamp: (value, fallback = '') => (value && value !== '-' ? String(value) : fallback),
    };
}

// ── Renderer harness: real ``_renderDialogActionButton`` ─────────────────
// session_dialogs.js delegates the validation body to the canonical viewer
// and routes action buttons through lifecycle_commands.js's shared button
// renderer, so both are loaded first (production order per dashboard_assets).
function loadRenderer() {
    const ctx = { ..._baseStubs() };
    ctx.window = { dashboardData: { startupComplete: true } };
    ctx.document = { getElementById: () => ({ innerHTML: '' }), addEventListener: () => {} };
    vm.createContext(ctx);
    vm.runInContext(_readJs('validation_viewer.js'), ctx, { filename: 'validation_viewer.js' });
    vm.runInContext(_readJs('lifecycle_commands.js'), ctx, { filename: 'lifecycle_commands.js' });
    vm.runInContext(_readJs('session_dialogs.js'), ctx, { filename: 'session_dialogs.js' });
    return ctx;
}

// Extract the (single) typed Command from a rendered button's
// ``data-lifecycle-command`` attribute and JSON-decode it.
function extractCommand(html) {
    const match = html.match(/data-lifecycle-command="([^"]+)"/);
    if (!match) return null;
    return JSON.parse(match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
}

// Backend-shaped dialog actions (``{ command, group }``), mirroring what
// ``view_models/dialog_commands.py`` emits.
const _RECORDING = {
    kind: 'open_session_recording', label: 'View Session Recording',
    issue_number: 42, run_dir: '/run/x', session_role: null, round_index: null,
    error_surface: 'inline',
};
const _COPY = { kind: 'copy_session_recording', label: 'Copy Session Recording', issue_number: 42, run_dir: '/run/x' };
const _CLAUDE = { kind: 'view_claude_log', label: 'View Claude Log', issue_number: 42, run_dir: '/run/x', error_surface: 'inline' };
const _ORCH = { kind: 'open_orchestrator_log', label: 'Open Orchestrator Log', issue_number: 42, run_dir: '/run/x', error_surface: 'inline' };
const _DIAG = { kind: 'open_session_diagnostics', label: 'Full Diagnostics', issue_number: 42, run_dir: '/run/x' };
const _PATH = { kind: 'open_path', label: 'Open Validation Record', path: '/tmp/v.json' };

function action(command, group = 'session_evidence') {
    return { command, group };
}

// ── A. Renderer: {command, group} → button carrying the exact command ─────

test('renderer: emits the provided command verbatim in data-lifecycle-command', () => {
    const ctx = loadRenderer();
    for (const command of [_RECORDING, _COPY, _CLAUDE, _ORCH, _DIAG, _PATH]) {
        const html = ctx._renderDialogActionButton(action(command), null, 'btn-secondary');
        assert.deepStrictEqual(
            extractCommand(html), command,
            `kind=${command.kind}: rendered command must equal the backend command`,
        );
    }
});

test('renderer: label carries the affordance glyph keyed off command.kind', () => {
    const ctx = loadRenderer();
    // open_path / open_orchestrator_log → external ↗; recording/claude/diag → modal ⧉;
    // copy → local action, no glyph.
    assert.match(ctx._renderDialogActionButton(action(_PATH), null, 'x'), /Open Validation Record ↗/);
    assert.match(ctx._renderDialogActionButton(action(_ORCH), null, 'x'), /Open Orchestrator Log ↗/);
    assert.match(ctx._renderDialogActionButton(action(_RECORDING), null, 'x'), /View Session Recording ⧉/);
    assert.match(ctx._renderDialogActionButton(action(_CLAUDE), null, 'x'), /View Claude Log ⧉/);
    assert.match(ctx._renderDialogActionButton(action(_DIAG), null, 'x'), /Full Diagnostics ⧉/);
    const copyHtml = ctx._renderDialogActionButton(action(_COPY), null, 'x');
    assert.match(copyHtml, /Copy Session Recording</);
    assert.ok(!/Copy Session Recording ↗|Copy Session Recording ⧉/.test(copyHtml));
});

test('renderer: an action without a command renders nothing', () => {
    const ctx = loadRenderer();
    assert.strictEqual(ctx._renderDialogActionButton({ group: 'diagnostics' }, null, 'x'), '');
    assert.strictEqual(ctx._renderDialogActionButton(null, null, 'x'), '');
});

// ── B. Consumer: typed Command → handler invocation ──────────────────────

function loadDispatcherWithSpies() {
    const ctx = { ..._baseStubs() };
    const calls = [];
    ctx.openPath = (p) => calls.push(['openPath', p]);
    ctx.openAgentLogAction = (issue, runDir, label, mode, opts) => calls.push(['openAgentLogAction', issue, runDir, label, mode, opts]);
    ctx.copyAgentLogAction = (issue, runDir) => calls.push(['copyAgentLogAction', issue, runDir]);
    ctx.viewClaudeLog = (issue, runDir, mode) => calls.push(['viewClaudeLog', issue, runDir, mode]);
    ctx.openFilteredOrchestratorLog = (issue, runDir, mode) => calls.push(['openFilteredOrchestratorLog', issue, runDir, mode]);
    ctx.openReviewFeedback = (issue) => calls.push(['openReviewFeedback', issue]);
    ctx.openSessionManifest = (issue, runDir) => calls.push(['openSessionManifest', issue, runDir]);
    ctx.calls = calls;
    vm.createContext(ctx);
    vm.runInContext(_readJs('lifecycle_commands.js'), ctx, { filename: 'lifecycle_commands.js' });
    return ctx;
}

test('dispatch: open_path → openPath(path)', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({ kind: 'open_path', label: 'Open Path', path: '/tmp/x.log' });
    assert.deepEqual(ctx.calls, [['openPath', '/tmp/x.log']]);
});

test('dispatch: open_session_recording → openAgentLogAction honours inline error surface', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand(_RECORDING);
    assert.deepEqual(ctx.calls, [[
        'openAgentLogAction', 42, '/run/x', 'View Session Recording', 'inline',
        { round_index: null, session_role: null },
    ]]);
});

test('dispatch: copy_session_recording / view_claude_log', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand(_COPY);
    ctx.runLifecycleCommand(_CLAUDE);
    assert.deepEqual(ctx.calls, [
        ['copyAgentLogAction', 42, '/run/x'],
        ['viewClaudeLog', 42, '/run/x', 'inline'],
    ]);
});

test('dispatch: open_orchestrator_log / open_session_diagnostics / open_review_feedback', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand(_ORCH);
    ctx.runLifecycleCommand(_DIAG);
    ctx.runLifecycleCommand({ kind: 'open_review_feedback', label: 'Review Feedback', issue_number: 42 });
    assert.deepEqual(ctx.calls, [
        ['openFilteredOrchestratorLog', 42, '/run/x', 'inline'],
        ['openSessionManifest', 42, '/run/x'],
        ['openReviewFeedback', 42],
    ]);
});

test('dispatch: run-dir-optional commands tolerate a null run_dir', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({ kind: 'open_orchestrator_log', label: 'Orchestrator Log', issue_number: 42, run_dir: null, error_surface: 'inline' });
    ctx.runLifecycleCommand({ kind: 'open_session_diagnostics', label: 'Diagnostics', issue_number: 42, run_dir: null });
    assert.deepEqual(ctx.calls, [
        ['openFilteredOrchestratorLog', 42, null, 'inline'],
        ['openSessionManifest', 42, null],
    ]);
});

test('dispatch: a dialog command missing its required run_dir is a no-op (no handler, no crash)', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({ kind: 'copy_session_recording', label: 'Copy Recording', issue_number: 42 });
    ctx.runLifecycleCommand({ kind: 'view_claude_log', label: 'Claude Log', issue_number: 42, error_surface: 'inline' });
    assert.deepEqual(ctx.calls, []);
});

// ── C. Round-trip: render → extract → dispatch ───────────────────────────

test('round-trip: render every dialog action, extract its Command, dispatch, observe the handler', () => {
    const rendererCtx = loadRenderer();
    const dispatcherCtx = loadDispatcherWithSpies();

    // [command, expected handler call after dispatch]
    const rows = [
        [_PATH, ['openPath', '/tmp/v.json']],
        [_RECORDING, ['openAgentLogAction', 42, '/run/x', 'View Session Recording', 'inline', { round_index: null, session_role: null }]],
        [_COPY, ['copyAgentLogAction', 42, '/run/x']],
        [_CLAUDE, ['viewClaudeLog', 42, '/run/x', 'inline']],
        [_ORCH, ['openFilteredOrchestratorLog', 42, '/run/x', 'inline']],
        [_DIAG, ['openSessionManifest', 42, '/run/x']],
    ];

    for (const [command, expectedCall] of rows) {
        dispatcherCtx.calls.length = 0;
        const html = rendererCtx._renderDialogActionButton(action(command), null, 'btn-secondary');
        const extracted = extractCommand(html);
        assert.ok(extracted, `kind=${command.kind}: expected a typed Command in the rendered button`);
        dispatcherCtx.runLifecycleCommand(extracted);
        assert.deepEqual(dispatcherCtx.calls, [expectedCall],
            `kind=${command.kind}: round-trip dispatch mismatch`);
    }
});
