// Cheap-integration tests for the typed-Command surface of the
// canonical E2E run view (issue #6310 follow-up / PR #6319).
//
// Why this layer exists
// ─────────────────────
// We have three test layers for the canonical viewer:
//
//   * Unit (JS-vm pure data): individual helpers — translator,
//     layout selector, plugin renderer, Copy-error handler.
//   * Cheap integration (this file): given a payload, run the
//     translator + the renderer + the plugin registry, then
//     EXTRACT every typed Command from the rendered HTML and
//     EXTRACT every UI string that came from the payload.  Assert
//     on the content the user would see and the Commands their
//     clicks would dispatch.  No browser; ``node --test`` only.
//   * Real-browser (Playwright): thin smoke proving the wire-up
//     works in Chromium.
//
// The cheap-integration layer (this file) is the workhorse for
// "given a payload, the user sees X and a click of Y dispatches
// Z."  It covers far more scenarios than Playwright can per
// minute because there's no browser boot.
//
// Content-checked, not structure-only.  Every assertion targets a
// specific string from the payload — the test fails if the wrong
// data ends up on the wrong button.

const test = require('node:test');
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
        escapeHtml: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        _humanizeSnakeCase: (s) => String(s || '')
            .split('_').map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(' '),
        showToast: () => {},
    };
}

// Build a vm context with the full canonical-viewer rendering stack
// loaded: validation_viewer.js + lifecycle_commands.js + the agent-
// context plugin + the e2e canonical-payload translator.  No DOM —
// the renderer returns strings.
function loadCanonicalSurface(spies = {}) {
    const ctx = { ..._baseStubs(), ...spies };
    // ``inline_agent_attempts.js`` exposes
    // ``renderInlineAgentAttemptsExpander`` on ``window`` — the
    // agent-context plugin checks for it at render time.  Mirror the
    // production bundle so the plugin's inline expander shows up.
    ctx.Map = Map;
    ctx.Promise = Promise;
    ctx.setTimeout = (fn) => fn;
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(_readJs('validation_viewer.js'), ctx, {
        filename: 'validation_viewer.js',
    });
    vm.runInContext(_readJs('lifecycle_commands.js'), ctx, {
        filename: 'lifecycle_commands.js',
    });
    vm.runInContext(_readJs('hierarchical_timeline.js'), ctx, {
        filename: 'hierarchical_timeline.js',
    });
    vm.runInContext(_readJs('inline_agent_attempts.js'), ctx, {
        filename: 'inline_agent_attempts.js',
    });
    vm.runInContext(_readJs('plugins/agent_context.js'), ctx, {
        filename: 'plugins/agent_context.js',
    });
    vm.runInContext(_readJs('e2e_canonical_payload.js'), ctx, {
        filename: 'e2e_canonical_payload.js',
    });
    return ctx;
}

// Pull every ``data-lifecycle-command="..."`` out of an HTML string
// and JSON-decode it.  Returns the decoded Command objects in
// document order.  This is the "what Commands does this UI emit?"
// extractor that the matrix below asserts against.
function extractCommands(html) {
    const out = [];
    const re = /data-lifecycle-command="([^"]+)"/g;
    let match;
    while ((match = re.exec(html)) !== null) {
        const raw = match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&');
        out.push(JSON.parse(raw));
    }
    return out;
}

// ── A. payload → rendered Commands ───────────────────────────────────────
//
// Each test below feeds a known E2E run-detail payload into the
// full canonical-viewer stack and asserts on the Commands that
// appear in the rendered HTML.  This is the contract that future
// payload-shape changes have to respect.

test('cmd surface: untracked-failure-only run produces NO Commands in the body', () => {
    // No linked issue → no io.agent-context plugin block → no
    // Open-issue-drawer Command.  The untracked-failures banner
    // is a separate rendering pass (in e2e_run_view.js) that this
    // file's canonical-viewer-only test doesn't include; here we
    // prove the body itself emits zero Commands when there's no
    // orchestrator context to surface.
    const ctx = loadCanonicalSurface();
    const canonical = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            untriaged: [{
                nodeid: 'tests/e2e/test_a.py::test_alpha',
                outcome: 'failed',
                failure_summary: 'AssertionError: untriaged break',
                longrepr: 'AssertionError: untriaged break\n  at line 7',
            }],
        },
    });
    const html = ctx.renderCanonicalValidationViewer(canonical);
    const commands = extractCommands(html);
    assert.deepEqual(commands, [],
        `expected no Commands for an untracked failure body, got: ${JSON.stringify(commands)}`);
    // Content sanity: the test display name + headline DID render so
    // the user can scan the failure.  The canonical viewer shortens
    // the nodeid to just the test name (the suffix after ``::``), so
    // we assert on the short form, not the full path.
    assert.match(html, /test_alpha/);
    assert.match(html, /AssertionError: untriaged break/);
});

test('cmd surface: linked-failure run renders an inline Attempts expander backed by an open_inline_agent_attempts typed Command', () => {
    // Issue #6322 follow-up: the plugin no longer teleports to the
    // drawer via an Open-issue-drawer typed Command.  The drill-in
    // is an inline ``▸ Attempts on issue #N`` expander, but the
    // expander is STILL backed by a typed Command — the
    // ``open_inline_agent_attempts`` kind — so dispatch flows
    // through the same single-owner pipeline as every other
    // affordance in the canonical viewer.  The new Command
    // replaces the legacy ``open_issue_timeline`` teleport.
    const ctx = loadCanonicalSurface();
    const canonical = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            has_issue: [{
                nodeid: 'tests/e2e/test_b.py::test_publish',
                outcome: 'failed',
                failure_summary: 'TimeoutError: publish did not complete',
                existing_issue: { number: 4503, title: 'publish flake', state: 'open' },
            }],
        },
    });
    const html = ctx.renderCanonicalValidationViewer(canonical);
    // Exactly one typed Command — the inline-attempts loader for #4503.
    const commands = extractCommands(html);
    assert.strictEqual(commands.length, 1);
    assert.deepStrictEqual(commands[0], {
        kind: 'open_inline_agent_attempts',
        label: 'Open Inline Agent Attempts',
        issue_number: 4503,
    });
    // The inline expander carries the issue number and is closed.
    assert.match(html, /agent-context-attempts-expander/);
    assert.match(html, /data-issue-number="4503"/);
    assert.match(html, /Attempts on issue #4503/);
    assert.match(html, /ontoggle="runLifecycleCommandFromToggle\(this\)"/);
    assert.ok(!/<details[^>]*data-issue-number="4503"[^>]*\sopen[\s>]/.test(html),
        'inline expander must start closed');
    // The user sees the right issue + final state + summary.
    assert.match(html, /#4503/);
    assert.match(html, /publish flake/);
    assert.match(html, /TimeoutError: publish did not complete/);
});

test('cmd surface: mixed run (linked + untracked) renders one inline expander per LINKED failure, untracked failures get no expander', () => {
    // Three failures: one linked (#4503), one linked (#4504), one
    // untracked.  Each linked failure renders an inline Attempts
    // expander with its own data-issue-number.  The untracked
    // failure surfaces in the viewer body (name + headline) but
    // gets no expander.
    const ctx = loadCanonicalSurface();
    const canonical = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            untriaged: [{
                nodeid: 'tests/e2e/test_a.py::test_new',
                outcome: 'failed',
                failure_summary: 'AssertionError: brand new',
            }],
            has_issue: [
                {
                    nodeid: 'tests/e2e/test_b.py::test_one',
                    outcome: 'failed',
                    failure_summary: 'TimeoutError #1',
                    existing_issue: { number: 4503, title: 'flake one', state: 'open' },
                },
                {
                    nodeid: 'tests/e2e/test_c.py::test_two',
                    outcome: 'failed',
                    failure_summary: 'TimeoutError #2',
                    existing_issue: { number: 4504, title: 'flake two', state: 'blocked' },
                },
            ],
        },
    });
    const html = ctx.renderCanonicalValidationViewer(canonical);
    // One typed Command per LINKED failure (kind
    // ``open_inline_agent_attempts``).  Untracked failures have no
    // Command — they have nothing to drill into.
    const commands = extractCommands(html);
    const inlineCmds = commands.filter((c) => c.kind === 'open_inline_agent_attempts');
    assert.strictEqual(inlineCmds.length, 2);
    const issueNumbers = inlineCmds.map((c) => c.issue_number).sort((a, b) => a - b);
    assert.deepStrictEqual(issueNumbers, [4503, 4504]);
    // One expander per LINKED failure.
    const expanderIssues = Array.from(
        html.matchAll(/agent-context-attempts-expander[^>]*data-issue-number="(\d+)"/g),
    ).map((m) => Number(m[1]));
    assert.deepStrictEqual(expanderIssues.sort((a, b) => a - b), [4503, 4504],
        `expected exactly one expander per linked failure; got ${JSON.stringify(expanderIssues)}`);
    // Each expander has its corresponding human-readable title.
    assert.match(html, /Attempts on issue #4503/);
    assert.match(html, /Attempts on issue #4504/);
    // Content sanity for the untracked failure surface (no expander,
    // but the test name + headline DID render).
    assert.match(html, /test_new/);
    assert.match(html, /AssertionError: brand new/);
});

test('cmd surface: all-passing run produces NO Commands and only the Passed group', () => {
    // Phase D redesign (issue #6322): all-passing run → just the
    // Passed group (count 3), no Failed/Errored/Skipped groups.
    // Predictable-collapse: the Passed group starts CLOSED.
    const ctx = loadCanonicalSurface();
    const canonical = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            passed: [
                { nodeid: 'tests/e2e/test_a.py::test_p1', outcome: 'passed', duration_seconds: 0.01 },
                { nodeid: 'tests/e2e/test_a.py::test_p2', outcome: 'passed', duration_seconds: 0.02 },
                { nodeid: 'tests/e2e/test_b.py::test_p3', outcome: 'passed', duration_seconds: 0.03 },
            ],
        },
    });
    const html = ctx.renderCanonicalValidationViewer(canonical);
    assert.deepEqual(extractCommands(html), []);
    // No triage cards (those only render under Failed/Errored).
    assert.doesNotMatch(html, /cvv-triage-card/);
    // Passed group renders, closed by default.
    assert.match(html, /cvv-group-passed[^"]*"[^>]*aria-expanded="false"/);
    // Group count reads "(3)".
    assert.match(html, /Passed<\/span><span class="cvv-summary">\(3\)/);
    // No Failed/Errored/Skipped groups (zero-count → hidden).
    assert.doesNotMatch(html, /cvv-group-failed/);
    assert.doesNotMatch(html, /cvv-group-error/);
    assert.doesNotMatch(html, /cvv-group-skipped/);
    // Specific test names appear inside the Passed group body.
    assert.match(html, /test_p1/);
    assert.match(html, /test_p2/);
    assert.match(html, /test_p3/);
});

test('cmd surface: skipped tests render their skip reason verbatim, no Commands', () => {
    const ctx = loadCanonicalSurface();
    const reason = "Skipped: not implemented on macOS — see PR #5500 for the shim";
    // The E2E run-detail payload puts the JUnit ``<skipped>`` text
    // on ``failure_details``.  The translator must preserve it for
    // skipped tests (not just failed ones) so the viewer can render
    // the reason inline.
    const canonical = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            skipped: [{
                nodeid: 'tests/integration/test_platform.py::test_macos',
                outcome: 'skipped',
                failure_details: reason,
            }],
        },
    });
    assert.strictEqual(canonical.junit_cases[0].failure_details, reason,
        'translator must carry failure_details through for skipped tests');

    const html = ctx.renderCanonicalValidationViewer(canonical);
    assert.deepEqual(extractCommands(html), []);
    // Reason text appears verbatim.
    assert.match(html, /Skipped: not implemented on macOS — see PR #5500 for the shim/);
    // Placeholder is NOT shown when we have a reason.
    assert.doesNotMatch(html, /No skip reason was recorded/);
    // Test name still appears.
    assert.match(html, /test_macos/);

    // Also accept ``skip_reason`` as an alternative source.  Some E2E
    // payload shapes put it there; the translator should honor either.
    const altCanonical = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            skipped: [{
                nodeid: 'tests/integration/test_platform.py::test_other',
                outcome: 'skipped',
                skip_reason: 'flaky-on-CI: tracked in #6310',
            }],
        },
    });
    assert.strictEqual(altCanonical.junit_cases[0].failure_details,
        'flaky-on-CI: tracked in #6310',
        'translator should also accept ``skip_reason`` for skipped tests');

    // And: if NEITHER source carries a reason, failure_details stays
    // null and the viewer falls back to the placeholder.
    const noneCanonical = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            skipped: [{
                nodeid: 'tests/integration/test_platform.py::test_silent',
                outcome: 'skipped',
            }],
        },
    });
    assert.strictEqual(noneCanonical.junit_cases[0].failure_details, null);
    const noneHtml = ctx.renderCanonicalValidationViewer(noneCanonical);
    assert.match(noneHtml, /No skip reason was recorded/);
});

test('cmd surface: skipped test surfaced via the validation-modal path renders its reason inline', () => {
    // The validation-modal path passes JUnitCases directly (with
    // ``failure_details`` populated for skips — the JUnit parser
    // puts the ``<skipped message="...">`` text there).  That
    // path's canonical-viewer render shows the reason inline.
    // This test exercises the skip-reason render directly, not
    // through the E2E translator.
    const ctx = loadCanonicalSurface();
    const reason = "Skipped: not implemented on macOS — see PR #5500";
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [{
            case_id: 'a',
            display_name: 'test_macos_specific',
            suite_name: 'tests/integration/test_platform.py',
            outcome: 'skipped',
            failure_details: reason,
            extras: [],
        }],
    });
    assert.deepEqual(extractCommands(html), []);
    // Exact reason text appears in the rendered DOM.
    assert.match(html, /Skipped: not implemented on macOS — see PR #5500/);
});

test('cmd surface: HTML in payload data is escaped, never interpreted', () => {
    // Defense-in-depth: a hostile JUnit XML (or compromised backend)
    // could put ``<script>`` into a headline or a skip reason.  The
    // viewer MUST escape it in body content.  We assert on:
    //   1. the escaped form ``&lt;script&gt;`` IS present (proves
    //      the escape pass ran on body content)
    //   2. NO real ``<script>`` ELEMENT was created — i.e. no
    //      ``<script>`` appears outside a quoted attribute value.
    // Note: attribute values like ``title="<script>..."`` are not
    // executable HTML — browsers do not parse attribute contents as
    // markup — so we deliberately don't fail the test on raw ``<``
    // inside an attribute.
    const ctx = loadCanonicalSurface();
    const malicious = "<script>alert('xss')</script>";
    const canonical = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            untriaged: [{
                nodeid: 'tests/test.py::test_bad',
                outcome: 'failed',
                failure_summary: malicious,
                longrepr: malicious,
            }],
        },
    });
    const html = ctx.renderCanonicalValidationViewer(canonical);
    // (1) Body content was escaped.
    assert.match(html, /&lt;script&gt;alert/,
        'expected the escaped form `&lt;script&gt;` to appear in body text');
    // (2) No real ``<script>`` element was constructed.  Strip out
    // all double-quoted attribute values, then assert ``<script``
    // is gone — this reliably catches a missing escapeHtml() on body
    // content without false-positiving on safe attribute content.
    const bodyOnly = html.replace(/"[^"]*"/g, '""');
    assert.doesNotMatch(bodyOnly, /<script/i,
        'expected no real <script> element in body content (after stripping attributes)');
});

// ── B. Command → handler invocation ──────────────────────────────────────
//
// Given a Command, runLifecycleCommand must call the right
// handler with the right args.  These spies replace each handler
// with a recorder; the assertion is on the recorded call.

function loadDispatcherWithSpies() {
    const ctx = { ..._baseStubs() };
    const calls = [];
    ctx.openIssueTimeline = (issueNumber, triggerEl, opts) => {
        calls.push(['openIssueTimeline', issueNumber, triggerEl, opts]);
    };
    ctx.openAgentLogAction = (issueNumber, runDir, label, mode, opts) => {
        calls.push(['openAgentLogAction', issueNumber, runDir, label, mode, opts]);
    };
    ctx.openReviewTranscript = (issueNumber, runDir, opts, mode) => {
        calls.push(['openReviewTranscript', issueNumber, runDir, opts, mode]);
    };
    ctx.openValidationFailure = (issueNumber, runDir, mode) => {
        calls.push(['openValidationFailure', issueNumber, runDir, mode]);
    };
    ctx.openPath = (p) => {
        calls.push(['openPath', p]);
    };
    ctx.calls = calls;
    vm.createContext(ctx);
    vm.runInContext(_readJs('lifecycle_commands.js'), ctx, {
        filename: 'lifecycle_commands.js',
    });
    return ctx;
}

test('dispatch: open_issue_timeline → openIssueTimeline(issue, null, {e2eRunId} | {})', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({
        kind: 'open_issue_timeline',
        issue_number: 4503,
        scope_kind: 'dashboard',
    });
    ctx.runLifecycleCommand({
        kind: 'open_issue_timeline',
        issue_number: 7777,
        scope_kind: 'e2e_run',
        e2e_run_id: 88,
    });
    assert.deepEqual(ctx.calls, [
        ['openIssueTimeline', 4503, null, {}],
        ['openIssueTimeline', 7777, null, { e2eRunId: 88 }],
    ]);
});

test('dispatch: open_session_recording → openAgentLogAction with the right args', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({
        kind: 'open_session_recording',
        issue_number: 42,
        run_dir: '/tmp/run-42',
        label: 'Coder Session',
        round_index: 2,
        session_role: 'coder',
    });
    assert.strictEqual(ctx.calls.length, 1);
    const call = ctx.calls[0];
    assert.strictEqual(call[0], 'openAgentLogAction');
    assert.strictEqual(call[1], 42);
    assert.strictEqual(call[2], '/tmp/run-42');
    assert.strictEqual(call[3], 'Coder Session');
    assert.strictEqual(call[4], 'toast');
    assert.deepEqual(call[5], { round_index: 2, session_role: 'coder' });
});

test('dispatch: open_review_transcript → openReviewTranscript with the right args', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({
        kind: 'open_review_transcript',
        issue_number: 100,
        run_dir: '/tmp/run-100',
        round_index: 1,
        transcript_role: 'reviewer',
    });
    assert.deepEqual(ctx.calls, [[
        'openReviewTranscript', 100, '/tmp/run-100',
        { round_index: 1, transcript_role: 'reviewer' }, 'toast',
    ]]);
});

test('dispatch: open_validation_details → openValidationFailure with the right args', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({
        kind: 'open_validation_details',
        issue_number: 4244,
        run_dir: '/tmp/run-4244',
    });
    assert.deepEqual(ctx.calls, [
        ['openValidationFailure', 4244, '/tmp/run-4244', 'toast'],
    ]);
});

test('dispatch: open_completion_record → openPath with the right path', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({
        kind: 'open_completion_record',
        path: '/tmp/run-42/completion-record.json',
    });
    assert.deepEqual(ctx.calls, [['openPath', '/tmp/run-42/completion-record.json']]);
});

test('dispatch: malformed Command (no kind) is a silent no-op (no toast spam)', () => {
    const ctx = loadDispatcherWithSpies();
    ctx.runLifecycleCommand({});
    ctx.runLifecycleCommand(null);
    ctx.runLifecycleCommand({ kind: '' });
    assert.deepEqual(ctx.calls, []);
});

test('dispatch: unknown kind toasts a warning (visible signal, no crash)', () => {
    const ctx = loadDispatcherWithSpies();
    const toasts = [];
    ctx.showToast = (msg, severity) => toasts.push([msg, severity]);
    ctx.runLifecycleCommand({ kind: 'this_is_not_a_command' });
    assert.strictEqual(toasts.length, 1);
    assert.match(toasts[0][0], /Unsupported lifecycle command: this_is_not_a_command/);
    assert.strictEqual(toasts[0][1], 'warning');
});

// ── C. round-trip: render → extract → dispatch ──────────────────────────
//
// The strongest "Command pattern works" assertion is: render a
// payload, pull a Command out of the rendered HTML, feed it back
// to the dispatcher, and verify the right handler ran with the
// right args.  Catches an entire class of bugs where the
// rendered Command JSON disagrees with what the dispatcher
// expects.

test('round-trip: render a linked-failure payload, extract the typed Command from the expander, dispatch through runLifecycleCommandFromToggle, observe the lazy fetch', () => {
    // The strongest "Command pattern works end-to-end" assertion:
    // render the payload, pull the typed Command out of the
    // rendered ``data-lifecycle-command`` on the inline expander,
    // simulate the ``<details>`` toggle through the SHARED
    // dispatcher (``runLifecycleCommandFromToggle``), and prove
    // the lazy-fetch URL on the spy.  Catches an entire class of
    // bugs where the rendered Command JSON disagrees with what the
    // dispatcher expects.
    const fetchCalls = [];
    const ctx = loadCanonicalSurface({
        fetch: (url) => {
            fetchCalls.push(url);
            return Promise.resolve({
                ok: true,
                status: 200,
                json: () => Promise.resolve({ runs: [] }),
            });
        },
    });
    const canonical = ctx.e2eRunToCanonicalPayload({
        results_by_category: {
            has_issue: [{
                nodeid: 'tests/e2e/test.py::test_x',
                outcome: 'failed',
                failure_summary: 'TimeoutError: oops',
                existing_issue: { number: 7777, title: 'oops issue', state: 'open' },
            }],
        },
    });
    const html = ctx.renderCanonicalValidationViewer(canonical);
    // 1. Extract the typed Command from the expander's
    //    ``data-lifecycle-command`` attribute.
    const commands = extractCommands(html);
    assert.strictEqual(commands.length, 1);
    assert.deepStrictEqual(commands[0], {
        kind: 'open_inline_agent_attempts',
        label: 'Open Inline Agent Attempts',
        issue_number: 7777,
    });
    // 2. Build a fake ``<details>`` element that carries the same
    //    typed JSON and dispatch through the shared toggle helper.
    const body = { innerHTML: '' };
    const detailsEl = {
        open: true,
        dataset: {
            issueNumber: '7777',
            loaded: '',
            lifecycleCommand: JSON.stringify(commands[0]),
        },
        querySelector: (sel) => (sel === '.agent-context-attempts-body' ? body : null),
    };
    ctx.runLifecycleCommandFromToggle(detailsEl);
    // 3. The dispatcher must have routed through the loader, which
    //    called ``fetch`` exactly once with the ops-scoped
    //    issue-detail URL for issue 7777.
    assert.deepStrictEqual(fetchCalls, ['/api/issue-detail/7777?view=ops']);
    // And the loaded marker is set, so a re-toggle short-circuits.
    assert.strictEqual(detailsEl.dataset.loaded, '1');
});
