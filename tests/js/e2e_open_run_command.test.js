// Cheap-integration test for the ``open_e2e_run`` typed Command
// (issue #6322; re-pointed at the inline runs-list by issue #6334).
// Three layers of the test pyramid, no DOM, no Playwright.  Mirrors
// the pattern in ``tests/js/e2e_canonical_command_surface.test.js``.
//
// Layer A — Unit: ``runLifecycleCommand({kind:'open_e2e_run',...})``
//   dispatches to ``expandE2ERunRow`` with the right args (NOT the
//   removed ``showUnifiedRunView`` — that was the modal-driver).
//   Malformed and unknown variants are handled (silent no-op /
//   toast warning).
//
// Layer B — Render → extract → dispatch: given the chip HTML the
//   server renders (with ``data-lifecycle-command``), regex-extract
//   the Command, dispatch it via the real dispatcher with a spy on
//   ``expandE2ERunRow``, assert the spy got the right args.
//
// Layer C — Round-trip with the real templates: read the Jinja
//   templates that emit the typed Command and confirm they still
//   serialize from the view-model dict (no hand-built JSON).

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function _baseStubs() {
    const calls = {
        expandE2ERunRow: [],
        loadE2ERunIntoRow: [],
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
            //
            // Issue #6334: ``open_e2e_run`` now routes to
            // ``expandE2ERunRow`` (the inline runs-list driver),
            // NOT to ``showUnifiedRunView`` (the dropped modal
            // driver).  ``expand_e2e_run`` routes to
            // ``loadE2ERunIntoRow``.
            expandE2ERunRow: (runId, opts) => { calls.expandE2ERunRow.push({ runId, opts }); },
            loadE2ERunIntoRow: (runId, el) => { calls.loadE2ERunIntoRow.push({ runId, el }); },
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

test('open_e2e_run command dispatches to expandE2ERunRow with the run_id (#6334)', () => {
    const { ctx, calls } = _loadDispatcher();
    ctx.runLifecycleCommand({ kind: 'open_e2e_run', run_id: 88 });
    assert.strictEqual(calls.expandE2ERunRow.length, 1);
    assert.strictEqual(calls.expandE2ERunRow[0].runId, 88);
    // No expand_run_details → handler receives ``{ expandRunDetails: false }``.
    assert.strictEqual(calls.expandE2ERunRow[0].opts.expandRunDetails, false);
});

test('open_e2e_run command forwards expand_run_details when true', () => {
    const { ctx, calls } = _loadDispatcher();
    ctx.runLifecycleCommand({
        kind: 'open_e2e_run',
        run_id: 88,
        expand_run_details: true,
    });
    assert.strictEqual(calls.expandE2ERunRow.length, 1);
    assert.strictEqual(calls.expandE2ERunRow[0].opts.expandRunDetails, true);
});

test('open_e2e_run command treats non-boolean expand_run_details as false', () => {
    const { ctx, calls } = _loadDispatcher();
    ctx.runLifecycleCommand({
        kind: 'open_e2e_run',
        run_id: 88,
        expand_run_details: 'yes-as-string',
    });
    assert.strictEqual(calls.expandE2ERunRow.length, 1);
    assert.strictEqual(calls.expandE2ERunRow[0].opts.expandRunDetails, false);
});

test('open_e2e_run command without run_id does NOT fire the handler', () => {
    // The dispatcher guards on ``command.run_id`` being truthy.
    // Missing run_id → guard fails → fall through.  This matches the
    // existing pattern for every other command kind (e.g. the
    // ``open_issue_timeline`` guard checks ``command.issue_number``).
    const { ctx, calls } = _loadDispatcher();
    ctx.runLifecycleCommand({ kind: 'open_e2e_run' });
    assert.strictEqual(calls.expandE2ERunRow.length, 0,
        'expandE2ERunRow must NOT fire without a run_id');
});

test('open_e2e_run command with run_id=0 does NOT fire the handler (falsy guard)', () => {
    const { ctx, calls } = _loadDispatcher();
    ctx.runLifecycleCommand({ kind: 'open_e2e_run', run_id: 0 });
    assert.strictEqual(calls.expandE2ERunRow.length, 0);
});


// ── Layer B: render → extract → dispatch ──────────────────────────
//
// The dashboard template renders the chip like:
//   <button class="card-focus"
//           data-lifecycle-command='{"kind":"open_e2e_run","run_id":88}'
//           onclick="runLifecycleCommandFromButton(this);event.stopPropagation();">
//     Run #88
//   </button>
// We build a representative chip string here, regex-extract the
// data-lifecycle-command, JSON.parse it, and dispatch via the real
// runLifecycleCommand.

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
        `onclick="runLifecycleCommandFromButton(this);event.stopPropagation();">Run #${runId}</button>`
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
    // Dispatch via the real dispatcher.  Issue #6334: routes to
    // ``expandE2ERunRow`` (the inline runs-list driver), not the
    // dropped ``showUnifiedRunView`` modal.
    ctx.runLifecycleCommand(command);
    assert.strictEqual(calls.expandE2ERunRow.length, 1);
    assert.strictEqual(calls.expandE2ERunRow[0].runId, 88);
});

test('chip render → extract → dispatch for a different run_id (no hard-coded captures)', () => {
    const { ctx, calls } = _loadDispatcher();
    const html = _buildChipHtml(427);
    ctx.runLifecycleCommand(_extractCommand(html));
    assert.strictEqual(calls.expandE2ERunRow[0].runId, 427);
});

// ── Layer C: round-trip with the real templates ───────────────────
//
// Reads the Jinja templates ``dashboard.html`` and ``issue_row.html``
// to confirm they emit the typed-Command shape we expect.  No Jinja
// rendering — we look for the literal expression in the template
// source.  If a future edit reverts to an inline
// ``onclick="showUnifiedRunView(...)"``, this test fails fast.

test('template guardrail: dashboard renders the runs-as-rows panel via typed RecentE2ERunsPayload (#6334)', () => {
    const tmpl = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/templates/dashboard.html'),
        'utf8',
    );
    // Issue #6334 retired the SSR ``class="card-focus"`` button
    // loop that hand-emitted ``open_e2e_run`` chips.  The runs list
    // now mounts client-side from the typed view-model payload
    // ``recent_e2e_runs`` (a ``RecentE2ERunsPayload`` dict), embedded
    // as inline JSON the chunk reads on DOMContentLoaded.
    assert.match(
        tmpl,
        /id="recentE2ERunsData" type="application\/json"/,
        'dashboard.html must embed the typed RecentE2ERunsPayload as inline JSON',
    );
    assert.match(
        tmpl,
        /id="e2eRunsListRoot"/,
        'dashboard.html must declare the #e2eRunsListRoot mount point',
    );
    // No hand-built JSON for the open_e2e_run shape (regression guard
    // — kept after #6334 because issue_row.html still emits typed
    // ``open_run_command`` chips via the view-model dict).
    assert.doesNotMatch(
        tmpl,
        /data-lifecycle-command='\{"kind":"open_e2e_run"/,
        'no hand-built JSON for open_e2e_run in dashboard.html',
    );
    // Same regression guard for the new typed kind.
    assert.doesNotMatch(
        tmpl,
        /data-lifecycle-command='\{"kind":"expand_e2e_run"/,
        'no hand-built JSON for expand_e2e_run in dashboard.html — the JS chunk owns row rendering',
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
