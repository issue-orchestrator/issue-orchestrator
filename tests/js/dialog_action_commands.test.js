// Typed-Command surface tests for the dialog action buttons
// (issue #6327).
//
// Before #6327 the validation / diagnostics / drawer dialogs rendered
// their action buttons with per-action inline ``onclick="openX(args)"``
// strings.  Those bypassed the typed-Command pipeline — you could not
// extract a Command from the rendered HTML because there wasn't one.
//
// This file covers BOTH sides of the migrated boundary at the JS-vm
// layer (see tests/AGENTS.md — the preferred middle layer):
//
//   A. Producer — render an action through the real
//      ``_renderDialogActionButton`` and EXTRACT the typed Command from
//      its ``data-lifecycle-command`` attribute; assert the payload
//      shape per action type.
//   B. Consumer — feed each Command kind to the real
//      ``runLifecycleCommand`` dispatcher (with handler spies) and
//      assert the right handler ran with the right args (including the
//      ``inline`` error surface the dialog uses).
//   C. Round-trip — render, extract, dispatch: proves the rendered
//      Command JSON agrees with what the dispatcher expects.

const test = require('node:test');
// Non-strict assert: vm.runInContext objects have cross-realm prototypes.
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

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

function renderCommand(action, ctx = loadRenderer()) {
    ctx.currentDiagnosticsRunDir = null;
    return extractCommand(ctx._renderDialogActionButton(action, null, 'btn-secondary'));
}

// ── A. Producer: action dict → typed Command payload ─────────────────────

test('producer: open_path → {kind, path}', () => {
    assert.deepStrictEqual(
        renderCommand({ type: 'open_path', label: 'Open Record', path: '/tmp/validation.json' }),
        { kind: 'open_path', path: '/tmp/validation.json' },
    );
});

test('producer: open_validation_failure carries run_dir + inline error surface', () => {
    assert.deepStrictEqual(
        renderCommand({ type: 'open_validation_failure', issue_number: 42, run_dir: '/run/x' }),
        { kind: 'open_validation_failure', issue_number: 42, run_dir: '/run/x', error_surface: 'inline' },
    );
});

test('producer: open_agent_log normalizes round_index and preserves session_role', () => {
    assert.deepStrictEqual(
        renderCommand({
            type: 'open_agent_log', issue_number: 42, run_dir: '/run/x',
            round_index: '2', session_role: 'coder',
        }),
        {
            kind: 'open_agent_log', issue_number: 42, run_dir: '/run/x',
            label: 'Session Recording', round_index: 2, session_role: 'coder',
            error_surface: 'inline',
        },
    );
    // Round 0 is a legitimate index — must survive as 0, not collapse to null.
    assert.strictEqual(
        renderCommand({ type: 'open_agent_log', issue_number: 7, run_dir: '/r', round_index: 0 }).round_index,
        0,
    );
    // A missing/non-integer round_index becomes null.
    assert.strictEqual(
        renderCommand({ type: 'open_agent_log', issue_number: 7, run_dir: '/r' }).round_index,
        null,
    );
});

test('producer: open_review_transcript carries typed context + inline surface', () => {
    assert.deepStrictEqual(
        renderCommand({
            type: 'open_review_transcript', issue_number: 9, run_dir: '/run/y',
            round_index: 1, transcript_role: 'reviewer',
        }),
        {
            kind: 'open_review_transcript', issue_number: 9, run_dir: '/run/y',
            round_index: 1, transcript_role: 'reviewer', error_surface: 'inline',
        },
    );
});

test('producer: open_review_artifact carries artifact path/type and null render_mode default', () => {
    assert.deepStrictEqual(
        renderCommand({
            type: 'open_review_artifact', issue_number: 9, run_dir: '/run/y',
            artifact_path: '/a/report.md', artifact_type: 'review_report',
        }),
        {
            kind: 'open_review_artifact', issue_number: 9, run_dir: '/run/y',
            artifact_path: '/a/report.md', artifact_type: 'review_report', render_mode: null,
        },
    );
});

test('producer: copy_agent_log and view_claude_log', () => {
    assert.deepStrictEqual(
        renderCommand({ type: 'copy_agent_log', issue_number: 42, run_dir: '/run/x' }),
        { kind: 'copy_agent_log', issue_number: 42, run_dir: '/run/x' },
    );
    assert.deepStrictEqual(
        renderCommand({ type: 'view_claude_log', issue_number: 42, run_dir: '/run/x' }),
        { kind: 'view_claude_log', issue_number: 42, run_dir: '/run/x', error_surface: 'inline' },
    );
});

test('producer: run-dir-optional commands emit null run_dir when absent', () => {
    // open_orchestrator_log and open_session_diagnostics render even
    // without a run dir — the handler tolerates a null run_dir.
    assert.deepStrictEqual(
        renderCommand({ type: 'open_orchestrator_log', issue_number: 42 }),
        { kind: 'open_orchestrator_log', issue_number: 42, run_dir: null, error_surface: 'inline' },
    );
    assert.deepStrictEqual(
        renderCommand({ type: 'open_session_diagnostics', issue_number: 42 }),
        { kind: 'open_session_diagnostics', issue_number: 42, run_dir: null },
    );
    assert.deepStrictEqual(
        renderCommand({ type: 'open_review_feedback', issue_number: 42 }),
        { kind: 'open_review_feedback', issue_number: 42 },
    );
});

test('producer: an un-openable action (missing run_dir) renders nothing', () => {
    const ctx = loadRenderer();
    ctx.currentDiagnosticsRunDir = null;
    for (const type of ['open_validation_failure', 'open_agent_log', 'open_review_transcript',
        'copy_agent_log', 'view_claude_log']) {
        const html = ctx._renderDialogActionButton({ type, issue_number: 1 }, null, 'btn-secondary');
        assert.strictEqual(html, '', `type=${type} with no run_dir must render empty`);
    }
    // open_path with no path is likewise un-openable.
    assert.strictEqual(
        ctx._renderDialogActionButton({ type: 'open_path', label: 'X' }, null, 'btn-secondary'),
        '',
    );
});

// ── B. Consumer: typed Command → handler invocation ──────────────────────

function loadDispatcherWithSpies() {
    const ctx = { ..._baseStubs() };
    const calls = [];
    ctx.openPath = (p) => calls.push(['openPath', p]);
    ctx.openValidationFailure = (issue, runDir, mode) => calls.push(['openValidationFailure', issue, runDir, mode]);
    ctx.openAgentLogAction = (issue, runDir, label, mode, opts) => calls.push(['openAgentLogAction', issue, runDir, label, mode, opts]);
    ctx.openReviewTranscript = (issue, runDir, opts, mode) => calls.push(['openReviewTranscript', issue, runDir, opts, mode]);
    ctx.openReviewArtifact = (issue, runDir, p, t, mode) => calls.push(['openReviewArtifact', issue, runDir, p, t, mode]);
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
    ctx.runLifecycleCommand({ kind: 'open_path', path: '/tmp/x.log' });
    assert.deepEqual(ctx.calls, [['openPath', '/tmp/x.log']]);
});

test('dispatch: open_validation_failure → openValidationFailure(issue, run_dir, inline)', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({ kind: 'open_validation_failure', issue_number: 42, run_dir: '/run/x', error_surface: 'inline' });
    assert.deepEqual(ctx.calls, [['openValidationFailure', 42, '/run/x', 'inline']]);
});

test('dispatch: open_agent_log → openAgentLogAction with inline surface and round 0 preserved', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({
        kind: 'open_agent_log', issue_number: 42, run_dir: '/run/x',
        label: 'Session Recording', round_index: 0, session_role: 'coder', error_surface: 'inline',
    });
    assert.strictEqual(ctx.calls.length, 1);
    const call = ctx.calls[0];
    assert.strictEqual(call[0], 'openAgentLogAction');
    assert.strictEqual(call[1], 42);
    assert.strictEqual(call[2], '/run/x');
    assert.strictEqual(call[3], 'Session Recording');
    assert.strictEqual(call[4], 'inline');
    assert.deepEqual(call[5], { round_index: 0, session_role: 'coder' });
});

test('dispatch: open_review_transcript → openReviewTranscript with inline surface', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({
        kind: 'open_review_transcript', issue_number: 9, run_dir: '/run/y',
        round_index: 1, transcript_role: 'reviewer', error_surface: 'inline',
    });
    assert.deepEqual(ctx.calls, [[
        'openReviewTranscript', 9, '/run/y', { round_index: 1, transcript_role: 'reviewer' }, 'inline',
    ]]);
});

test('dispatch: open_review_artifact → openReviewArtifact(issue, run_dir, path, type, render_mode)', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({
        kind: 'open_review_artifact', issue_number: 9, run_dir: '/run/y',
        artifact_path: '/a/report.md', artifact_type: 'review_report', render_mode: null,
    });
    assert.deepEqual(ctx.calls, [['openReviewArtifact', 9, '/run/y', '/a/report.md', 'review_report', null]]);
});

test('dispatch: copy_agent_log / view_claude_log', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({ kind: 'copy_agent_log', issue_number: 42, run_dir: '/run/x' });
    ctx.runLifecycleCommand({ kind: 'view_claude_log', issue_number: 42, run_dir: '/run/x', error_surface: 'inline' });
    assert.deepEqual(ctx.calls, [
        ['copyAgentLogAction', 42, '/run/x'],
        ['viewClaudeLog', 42, '/run/x', 'inline'],
    ]);
});

test('dispatch: open_orchestrator_log / open_review_feedback / open_session_diagnostics', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({ kind: 'open_orchestrator_log', issue_number: 42, run_dir: null, error_surface: 'inline' });
    ctx.runLifecycleCommand({ kind: 'open_review_feedback', issue_number: 42 });
    ctx.runLifecycleCommand({ kind: 'open_session_diagnostics', issue_number: 42, run_dir: null });
    assert.deepEqual(ctx.calls, [
        ['openFilteredOrchestratorLog', 42, null, 'inline'],
        ['openReviewFeedback', 42],
        ['openSessionManifest', 42, null],
    ]);
});

test('dispatch: a dialog command missing its required run_dir is a no-op (no handler, no crash)', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({ kind: 'open_validation_failure', issue_number: 42 });
    ctx.runLifecycleCommand({ kind: 'copy_agent_log', issue_number: 42 });
    assert.deepEqual(ctx.calls, []);
});

// ── C. Round-trip: render → extract → dispatch ───────────────────────────

test('round-trip: render every dialog action, extract its Command, dispatch, observe the handler', () => {
    const rendererCtx = loadRenderer();
    rendererCtx.currentDiagnosticsRunDir = null;
    const dispatcherCtx = loadDispatcherWithSpies();

    // [action, expected handler call after dispatch]
    const rows = [
        [{ type: 'open_path', label: 'Open Record', path: '/tmp/v.json' },
            ['openPath', '/tmp/v.json']],
        [{ type: 'open_validation_failure', issue_number: 42, run_dir: '/run/x' },
            ['openValidationFailure', 42, '/run/x', 'inline']],
        [{ type: 'open_agent_log', issue_number: 42, run_dir: '/run/x', round_index: 2, session_role: 'coder' },
            ['openAgentLogAction', 42, '/run/x', 'Session Recording', 'inline', { round_index: 2, session_role: 'coder' }]],
        [{ type: 'open_review_transcript', issue_number: 9, run_dir: '/run/y', round_index: 1, transcript_role: 'reviewer' },
            ['openReviewTranscript', 9, '/run/y', { round_index: 1, transcript_role: 'reviewer' }, 'inline']],
        [{ type: 'open_review_artifact', issue_number: 9, run_dir: '/run/y', artifact_path: '/a.md', artifact_type: 'review_report' },
            ['openReviewArtifact', 9, '/run/y', '/a.md', 'review_report', null]],
        [{ type: 'copy_agent_log', issue_number: 42, run_dir: '/run/x' },
            ['copyAgentLogAction', 42, '/run/x']],
        [{ type: 'view_claude_log', issue_number: 42, run_dir: '/run/x' },
            ['viewClaudeLog', 42, '/run/x', 'inline']],
        [{ type: 'open_orchestrator_log', issue_number: 42 },
            ['openFilteredOrchestratorLog', 42, null, 'inline']],
        [{ type: 'open_review_feedback', issue_number: 42 },
            ['openReviewFeedback', 42]],
        [{ type: 'open_session_diagnostics', issue_number: 42 },
            ['openSessionManifest', 42, null]],
    ];

    for (const [action, expectedCall] of rows) {
        dispatcherCtx.calls.length = 0;
        const html = rendererCtx._renderDialogActionButton(action, null, 'btn-secondary');
        const command = extractCommand(html);
        assert.ok(command, `type=${action.type}: expected a typed Command in the rendered button`);
        dispatcherCtx.runLifecycleCommand(command);
        assert.deepEqual(dispatcherCtx.calls, [expectedCall],
            `type=${action.type}: round-trip dispatch mismatch`);
    }
});
