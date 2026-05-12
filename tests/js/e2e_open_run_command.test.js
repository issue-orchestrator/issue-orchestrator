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
    // No expand_run_details → handler receives ``{ expandRunDetails: false }``.
    assert.deepEqual(calls.showUnifiedRunView[0].opts, { expandRunDetails: false });
});

test('open_e2e_run command forwards expand_run_details when true', () => {
    const { ctx, calls } = _loadDispatcher();
    ctx.runE2ELifecycleCommand({
        kind: 'open_e2e_run',
        run_id: 88,
        expand_run_details: true,
    });
    assert.strictEqual(calls.showUnifiedRunView.length, 1);
    assert.deepEqual(calls.showUnifiedRunView[0].opts, { expandRunDetails: true });
});

test('open_e2e_run command treats non-boolean expand_run_details as false', () => {
    const { ctx, calls } = _loadDispatcher();
    ctx.runE2ELifecycleCommand({
        kind: 'open_e2e_run',
        run_id: 88,
        expand_run_details: 'yes-as-string',
    });
    assert.strictEqual(calls.showUnifiedRunView.length, 1);
    assert.deepEqual(calls.showUnifiedRunView[0].opts, { expandRunDetails: false });
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
    // Match the production template's rendered shape after
    // PR #6329 reviewer Blocker 1 (typed Command contract).  The
    // dashboard.html template now serializes the view-model's
    // ``open_run_command`` field (a ``OpenE2ERunCommand.model_dump()``
    // dict) through Jinja's ``| tojson | forceescape`` filter chain.
    // ``forceescape`` HTML-escapes the JSON's double-quotes as
    // ``&#34;`` so they can sit inside ``data-lifecycle-command="..."``.
    const cmd = {
        kind: 'open_e2e_run',
        label: 'Open E2E Run',
        run_id: runId,
        expand_run_details: false,
    };
    const cmdAttr = JSON.stringify(cmd).replace(/"/g, '&#34;');
    return (
        `<button class="card-focus" data-lifecycle-command="${cmdAttr}" ` +
        `onclick="runE2ELifecycleCommandFromButton(this);event.stopPropagation();">Run #${runId}</button>`
    );
}

function _extractCommand(html) {
    // Match the production attribute shape: double-quoted attribute
    // with HTML-escaped JSON inside.
    const match = html.match(/data-lifecycle-command="([^"]+)"/);
    if (!match) return null;
    // Un-escape the HTML entities Jinja's ``forceescape`` produced.
    const unescaped = match[1]
        .replace(/&#34;/g, '"')
        .replace(/&quot;/g, '"')
        .replace(/&amp;/g, '&');
    try {
        return JSON.parse(unescaped);
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
    assert.deepStrictEqual(command, {
        kind: 'open_e2e_run',
        label: 'Open E2E Run',
        run_id: 88,
        expand_run_details: false,
    });
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

test('template guardrail: dashboard chip serializes from the view-model open_run_command (no hand-built JSON)', () => {
    const tmpl = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/templates/dashboard.html'),
        'utf8',
    );
    // After PR #6329 review: templates must NOT hand-build the
    // typed-Command JSON.  They must consume the view-model's
    // ``open_run_command`` dict (built by ``OpenE2ERunCommand.model_dump()``)
    // through Jinja's ``| tojson | forceescape`` filter chain.
    assert.match(
        tmpl,
        /class="card-focus"[^>]*data-lifecycle-command="\{\{ run\.open_run_command \| tojson \| forceescape \}\}"/,
        'dashboard.html card-focus button must serialize the view-model open_run_command (typed contract owner)',
    );
    // The legacy ``onclick="showUnifiedRunView(...)"`` direct-call is gone.
    assert.doesNotMatch(
        tmpl,
        /<button class="card-focus" onclick="showUnifiedRunView\(/,
        'no card-focus button may use inline showUnifiedRunView() onclick',
    );
    // No hand-built JSON shape in the template (regression guard).
    assert.doesNotMatch(
        tmpl,
        /data-lifecycle-command='\{"kind":"open_e2e_run"/,
        'no hand-built JSON for open_e2e_run in dashboard.html (must serialize from view-model dict)',
    );
});

test('template guardrail: issue-row View buttons serialize from the view-model open_run_command', () => {
    const tmpl = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/templates/issue_row.html'),
        'utf8',
    );
    // Every View button must consume the view-model's open_run_command dict.
    const typedHits = (tmpl.match(/data-lifecycle-command="\{\{ issue\.open_run_command \| tojson \| forceescape \}\}"/g) || []).length;
    assert.ok(typedHits >= 2,
        `expected at least 2 typed-Command View buttons in issue_row.html, got ${typedHits}`);
    // No View button still uses the inline showUnifiedRunView direct-call.
    assert.doesNotMatch(
        tmpl,
        /class="issue-action-btn view-btn" onclick="showUnifiedRunView\(/,
        'no issue-action-btn view-btn may use inline showUnifiedRunView() onclick',
    );
    // No hand-built JSON shape either.
    assert.doesNotMatch(
        tmpl,
        /data-lifecycle-command='\{"kind":"open_e2e_run"/,
        'no hand-built JSON for open_e2e_run in issue_row.html',
    );
});
