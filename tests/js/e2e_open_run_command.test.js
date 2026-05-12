// Cheap-integration test for the ``open_e2e_run`` typed Command
// (issue #6322).  Three layers of the test pyramid, no DOM, no
// Playwright.  Mirrors the pattern in
// ``tests/js/e2e_canonical_command_surface.test.js``.
//
// Layer A — Unit: ``runE2ELifecycleCommand({kind:'open_e2e_run',...})``
//   dispatches to ``showUnifiedRunView`` with the right args.
//   Malformed and unknown variants are handled (silent no-op /
//   toast warning).
//
// Layer B — Render → extract → dispatch: given the chip HTML the
//   server renders (with ``data-lifecycle-command``), regex-extract
//   the Command, dispatch it via the real dispatcher with a spy on
//   ``showUnifiedRunView``, assert the spy got the right args.
//
// Layer C — Round-trip with the chip HTML: builds the actual HTML
//   shape from the templates' inline expression and runs A+B
//   end-to-end so a template-text drift would break the round-trip.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function _baseStubs() {
    const calls = {
        showUnifiedRunView: [],
        openIssueTimeline: [],
        openAgentLogAction: [],
        openReviewTranscript: [],
        openValidationFailure: [],
        openPath: [],
        showToast: [],
    };
    return {
        ctx: {
            console,
            URLSearchParams,
            window: {},
            document: {
                getElementById: () => null,
                addEventListener: () => {},
                querySelectorAll: () => [],
            },
            navigator: {},
            // Stub each handler the dispatcher routes to.  Records every
            // call so the test can assert on args.
            showUnifiedRunView: (runId, opts) => { calls.showUnifiedRunView.push({ runId, opts }); },
            openIssueTimeline: (n, t, o) => { calls.openIssueTimeline.push({ n, t, o }); },
            openAgentLogAction: (n, r, l, s, c) => { calls.openAgentLogAction.push({ n, r, l, s, c }); },
            openReviewTranscript: (n, r, c, s) => { calls.openReviewTranscript.push({ n, r, c, s }); },
            openValidationFailure: (n, r, s) => { calls.openValidationFailure.push({ n, r, s }); },
            openPath: (p) => { calls.openPath.push({ p }); },
            showToast: (msg, level) => { calls.showToast.push({ msg, level }); },
            // String escapers the dispatcher's button renderer uses (when
            // we exercise it).
            escapeHtml: (value) => String(value == null ? '' : value)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
            escapeAttr: (value) => String(value == null ? '' : value)
                .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        },
        calls,
    };
}

function _loadDispatcher() {
    const { ctx, calls } = _baseStubs();
    vm.createContext(ctx);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/lifecycle_commands.js'),
        'utf8',
    );
    vm.runInContext(source, ctx, { filename: 'lifecycle_commands.js' });
    return { ctx, calls };
}

// ── Layer A: unit ─────────────────────────────────────────────────

test('open_e2e_run command dispatches to showUnifiedRunView with the run_id', () => {
    const { ctx, calls } = _loadDispatcher();
    ctx.runE2ELifecycleCommand({ kind: 'open_e2e_run', run_id: 88 });
    assert.strictEqual(calls.showUnifiedRunView.length, 1);
    assert.strictEqual(calls.showUnifiedRunView[0].runId, 88);
    // No options passed → handler receives {} (the dispatcher's
    // fall-through for command.options).
    assert.deepEqual(calls.showUnifiedRunView[0].opts, {});
});

test('open_e2e_run command forwards options when provided', () => {
    const { ctx, calls } = _loadDispatcher();
    ctx.runE2ELifecycleCommand({
        kind: 'open_e2e_run',
        run_id: 88,
        options: { expandRunDetails: true },
    });
    assert.strictEqual(calls.showUnifiedRunView.length, 1);
    assert.deepStrictEqual(calls.showUnifiedRunView[0].opts, { expandRunDetails: true });
});

test('open_e2e_run command without run_id does NOT fire the handler', () => {
    // The dispatcher guards on ``command.run_id`` being truthy.
    // Missing run_id → guard fails → fall through.  This matches the
    // existing pattern for every other command kind (e.g. the
    // ``open_issue_timeline`` guard checks ``command.issue_number``).
    const { ctx, calls } = _loadDispatcher();
    ctx.runE2ELifecycleCommand({ kind: 'open_e2e_run' });
    assert.strictEqual(calls.showUnifiedRunView.length, 0,
        'showUnifiedRunView must NOT fire without a run_id');
});

test('open_e2e_run command with run_id=0 does NOT fire the handler (falsy guard)', () => {
    const { ctx, calls } = _loadDispatcher();
    ctx.runE2ELifecycleCommand({ kind: 'open_e2e_run', run_id: 0 });
    assert.strictEqual(calls.showUnifiedRunView.length, 0);
});

test('open_e2e_run command ignores non-object options', () => {
    const { ctx, calls } = _loadDispatcher();
    ctx.runE2ELifecycleCommand({ kind: 'open_e2e_run', run_id: 88, options: 'not-an-object' });
    assert.strictEqual(calls.showUnifiedRunView.length, 1);
    assert.deepEqual(calls.showUnifiedRunView[0].opts, {},
        'options must default to {} when not an object');
});

// ── Layer B: render → extract → dispatch ──────────────────────────
//
// The dashboard template renders the chip like:
//   <button class="card-focus"
//           data-lifecycle-command='{"kind":"open_e2e_run","run_id":88}'
//           onclick="runE2ELifecycleCommandFromButton(this);event.stopPropagation();">
//     Run #88
//   </button>
// We build a representative chip string here, regex-extract the
// data-lifecycle-command, JSON.parse it, and dispatch via the real
// runE2ELifecycleCommand.

function _buildChipHtml(runId) {
    const cmdAttr = JSON.stringify({ kind: 'open_e2e_run', run_id: runId });
    return (
        `<button class="card-focus" data-lifecycle-command='${cmdAttr}' ` +
        `onclick="runE2ELifecycleCommandFromButton(this);event.stopPropagation();">Run #${runId}</button>`
    );
}

function _extractCommand(html) {
    const match = html.match(/data-lifecycle-command='([^']+)'/);
    if (!match) return null;
    try {
        return JSON.parse(match[1]);
    } catch (e) {
        return null;
    }
}

test('chip render → extract → dispatch: end-to-end through the typed Command', () => {
    const { ctx, calls } = _loadDispatcher();
    const html = _buildChipHtml(88);
    // Extract.
    const command = _extractCommand(html);
    assert.ok(command, 'extracting data-lifecycle-command must succeed');
    assert.deepStrictEqual(command, { kind: 'open_e2e_run', run_id: 88 });
    // Dispatch via the real dispatcher.
    ctx.runE2ELifecycleCommand(command);
    assert.strictEqual(calls.showUnifiedRunView.length, 1);
    assert.strictEqual(calls.showUnifiedRunView[0].runId, 88);
});

test('chip render → extract → dispatch for a different run_id (no hard-coded captures)', () => {
    const { ctx, calls } = _loadDispatcher();
    const html = _buildChipHtml(427);
    ctx.runE2ELifecycleCommand(_extractCommand(html));
    assert.strictEqual(calls.showUnifiedRunView[0].runId, 427);
});

// ── Layer C: round-trip with the real templates ───────────────────
//
// Reads the Jinja templates ``dashboard.html`` and ``issue_row.html``
// to confirm they emit the typed-Command shape we expect.  No Jinja
// rendering — we look for the literal expression in the template
// source.  If a future edit reverts to an inline
// ``onclick="showUnifiedRunView(...)"``, this test fails fast.

test('template guardrail: dashboard chip emits data-lifecycle-command (no raw showUnifiedRunView onclick)', () => {
    const tmpl = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/templates/dashboard.html'),
        'utf8',
    );
    // The Run-history card button must use the typed Command.
    assert.match(
        tmpl,
        /class="card-focus"[^>]*data-lifecycle-command='\{"kind":"open_e2e_run","run_id":\{\{ run\.e2e_run_id \}\}\}'/,
        'dashboard.html Run-history card-focus button must carry the open_e2e_run typed Command',
    );
    // The legacy ``onclick="showUnifiedRunView(...)"`` direct-call is gone
    // from this template path.
    assert.doesNotMatch(
        tmpl,
        /<button class="card-focus" onclick="showUnifiedRunView\(/,
        'no card-focus button may use inline showUnifiedRunView() onclick',
    );
});

test('template guardrail: issue-row View buttons emit data-lifecycle-command (no raw showUnifiedRunView onclick)', () => {
    const tmpl = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/templates/issue_row.html'),
        'utf8',
    );
    // Every View button in this template should now carry the typed Command.
    const typedHits = (tmpl.match(/data-lifecycle-command='\{"kind":"open_e2e_run","run_id":\{\{ issue\.e2e_run_id \}\}\}'/g) || []).length;
    assert.ok(typedHits >= 2,
        `expected at least 2 typed-Command View buttons in issue_row.html, got ${typedHits}`);
    // No View button still uses the inline showUnifiedRunView direct-call.
    assert.doesNotMatch(
        tmpl,
        /class="issue-action-btn view-btn" onclick="showUnifiedRunView\(/,
        'no issue-action-btn view-btn may use inline showUnifiedRunView() onclick',
    );
});
