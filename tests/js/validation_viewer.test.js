// JS-vm tests for the canonical validation viewer (issue #6310 follow-up,
// Phase A).  Exercises:
//
//   1. ``registerValidationPlugin`` registry semantics — register,
//      lookup, unknown namespace silently skips, misbehaving renderer
//      doesn't crash the viewer.
//   2. ``renderCanonicalValidationViewer`` output for the four canonical
//      cases: passed run, failed run with triage cards, mixed
//      pass/fail, empty.
//   3. Browse-by-file expansion of non-failed cases, with per-test
//      stdout rendering.
//   4. Plugin extras: when a case carries a registered namespace, the
//      plugin renderer's HTML appears inside the test's expansion.
//
// All tests run in a node:vm context with the viewer source loaded,
// matching the existing ``tests/js/e2e_run_view_actions.test.js``
// pattern.  No DOM library — we assert on rendered HTML strings.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadViewer(overrides = {}) {
    // Stubs for the shared dashboard primitives the viewer calls into.
    // The viewer is intentionally self-contained: action-section
    // rendering is now an explicit ``options.renderActionSections``
    // dependency (reviewer Blocker 2 on PR #6314), so we no longer need
    // a global stub for it here.
    const baseStubs = {
        console,
        escapeHtml: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
    };
    const context = { ...baseStubs, ...overrides };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/validation_viewer.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'validation_viewer.js' });
    return context;
}

function loadViewerWithAgentPlugin(overrides = {}) {
    const ctx = loadViewer(overrides);
    // The plugin's "Open issue drawer" affordance renders through the
    // shared lifecycle Command pipeline (PR #6319 Blocker 1 fix).  In
    // production ``lifecycle_commands.js`` is loaded earlier in the
    // bundle; in JS-vm tests we load it before the plugin so the
    // symbol is in scope.
    const lifecycleSource = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/lifecycle_commands.js'),
        'utf8',
    );
    vm.runInContext(lifecycleSource, ctx, { filename: 'lifecycle_commands.js' });
    // ``_renderLifecycleCommandButton`` depends on ``_humanizeSnakeCase``
    // when falling back on a derived label.  Provide a thin stub —
    // tests pass an explicit fallbackLabel so the stub is never
    // actually consulted, but loading it avoids a ReferenceError if
    // some future test omits the label.
    if (typeof ctx._humanizeSnakeCase !== 'function') {
        ctx._humanizeSnakeCase = (s) => String(s || '');
    }
    const pluginSource = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/plugins/agent_context.js'),
        'utf8',
    );
    vm.runInContext(pluginSource, ctx, { filename: 'plugins/agent_context.js' });
    return ctx;
}

// ── Registry tests ────────────────────────────────────────────────────────

test('registry: registerValidationPlugin rejects empty namespace', () => {
    const ctx = loadViewer();
    assert.throws(() => ctx.registerValidationPlugin('', () => ''), /namespace/);
    assert.throws(() => ctx.registerValidationPlugin(null, () => ''), /namespace/);
});

test('registry: registerValidationPlugin rejects non-function renderer', () => {
    const ctx = loadViewer();
    assert.throws(() => ctx.registerValidationPlugin('test.foo', null), /renderer/);
    assert.throws(() => ctx.registerValidationPlugin('test.foo', 'not a function'), /renderer/);
});

test('registry: register + getValidationPlugin round-trips', () => {
    const ctx = loadViewer();
    const renderer = (payload) => `[rendered ${payload.x}]`;
    ctx.registerValidationPlugin('test.echo', renderer);
    assert.strictEqual(ctx.getValidationPlugin('test.echo'), renderer);
    assert.strictEqual(ctx.getValidationPlugin('test.missing'), null);
});

test('registry: renderPluginExtras dispatches by namespace', () => {
    const ctx = loadViewer();
    ctx.registerValidationPlugin('test.echo', (p) => `<div data-x="${p.x}">echo</div>`);
    const html = ctx.renderPluginExtras({
        extras: [{ namespace: 'test.echo', payload: { x: 7 } }],
    });
    assert.match(html, /data-x="7"/);
});

test('registry: unknown namespace is silently skipped', () => {
    const ctx = loadViewer();
    ctx.registerValidationPlugin('test.echo', () => '<div>echo</div>');
    const html = ctx.renderPluginExtras({
        extras: [
            { namespace: 'test.unknown', payload: {} },
            { namespace: 'test.echo', payload: {} },
        ],
    });
    assert.match(html, /echo/);
    assert.doesNotMatch(html, /unknown/);
});

test('registry: misbehaving plugin renders an inline error, not a crash', () => {
    const ctx = loadViewer();
    ctx.registerValidationPlugin('test.crash', () => { throw new Error('boom'); });
    const html = ctx.renderPluginExtras({
        extras: [{ namespace: 'test.crash', payload: {} }],
    });
    assert.match(html, /Plugin <code>test\.crash<\/code> failed to render: boom/);
});

test('registry: extras with non-array shape is treated as empty', () => {
    const ctx = loadViewer();
    assert.strictEqual(ctx.renderPluginExtras({}), '');
    assert.strictEqual(ctx.renderPluginExtras({ extras: null }), '');
    assert.strictEqual(ctx.renderPluginExtras({ extras: 'not an array' }), '');
});

// ── Canonical viewer tests ────────────────────────────────────────────────

test('viewer: passed run renders a single Passed group and no triage cards', () => {
    // Phase D redesign (issue #6322): the canonical viewer renders
    // outcome-grouped expanders.  All-passing run → just the
    // Passed (N) group; no Failed/Errored/Skipped groups (zero-count
    // groups are hidden).  No triage cards (those only render under
    // Failed/Errored groups).
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [
            { case_id: 'a', display_name: 'test a', outcome: 'passed', duration_seconds: 0.003, suite_name: 'tests/test_a.py', extras: [] },
            { case_id: 'b', display_name: 'test b', outcome: 'passed', duration_seconds: 0.004, suite_name: 'tests/test_b.py', extras: [] },
        ],
    });
    assert.doesNotMatch(html, /cvv-triage-card/);
    // Passed group renders.
    assert.match(html, /cvv-group-passed/);
    // Group count reads "(2)".
    assert.match(html, /Passed<\/span><span class="cvv-summary">\(2\)/);
    // No Failed/Errored/Skipped groups (zero-count → hidden).
    assert.doesNotMatch(html, /cvv-group-failed/);
    assert.doesNotMatch(html, /cvv-group-error/);
    assert.doesNotMatch(html, /cvv-group-skipped/);
    // Each file rendered as its own expander inside the group.
    assert.match(html, /test_a\.py/);
    assert.match(html, /test_b\.py/);
});

test('viewer: skipped-only run renders the Skipped group with the SKIPPED icon on file rows (reviewer blocker on #6324)', () => {
    // Reviewer blocker: the Skipped outcome group used to render its
    // file rows with the Passed icon (``cvv-ico-passed`` / ``✓``)
    // because ``_renderBrowseByFile`` hard-coded that.  Now the group
    // passes its outcome down so the file row's icon matches.
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [
            { case_id: 's1', display_name: 'test_macos', outcome: 'skipped', suite_name: 'tests/test_platform.py', failure_details: 'platform mismatch', extras: [] },
            { case_id: 's2', display_name: 'test_windows', outcome: 'skipped', suite_name: 'tests/test_platform.py', failure_details: 'platform mismatch', extras: [] },
        ],
    });
    // Skipped group renders.
    assert.match(html, /cvv-group-skipped/);
    // No Passed group (zero passed → hidden).
    assert.doesNotMatch(html, /cvv-group-passed/);
    // The file row inside the Skipped group uses the SKIPPED icon class,
    // not the passed one.  Extract the file row's opening tag and check.
    const fileTag = html.match(/<details class="cvv-row cvv-file"[^>]*>(?=<summary>[^<]*<span class="cvv-caret">[^<]*<\/span><span class="cvv-ico cvv-ico-(?<icon>[^"]+)">)/);
    assert.ok(fileTag, 'expected file row inside the Skipped group');
    assert.strictEqual(fileTag.groups.icon, 'skipped',
        'file row in Skipped group must use the skipped icon class');
    // And the icon character is the skipped glyph (en-dash), not the check.
    assert.match(html, /cvv-ico cvv-ico-skipped">–</);
    assert.doesNotMatch(html, /cvv-ico cvv-ico-passed">✓<.*test_platform\.py/);
});

test('viewer: mixed passed+skipped run renders each in its own group with matching icons', () => {
    // Same test should appear in only one group based on outcome.
    // Passed group's files get ✓; Skipped group's files get –.
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [
            { case_id: 'p1', display_name: 'test_linux', outcome: 'passed', suite_name: 'tests/test_platform.py', extras: [] },
            { case_id: 's1', display_name: 'test_macos', outcome: 'skipped', suite_name: 'tests/test_platform.py', failure_details: 'platform mismatch', extras: [] },
        ],
    });
    assert.match(html, /cvv-group-passed/);
    assert.match(html, /cvv-group-skipped/);
    // Pull out each group's body and check icon there.
    const passedGroupBody = html.match(/cvv-group-passed[^>]*>[\s\S]*?<\/details>$/m);
    const skippedGroupBody = html.match(/cvv-group-skipped[^>]*>[\s\S]*?(?=<details class="cvv-row cvv-group cvv-group-passed|$)/);
    assert.ok(skippedGroupBody, 'skipped group must render');
    assert.match(skippedGroupBody[0], /cvv-ico cvv-ico-skipped/,
        'skipped group must use skipped icon on its file row');
});

test('viewer: failed run renders one triage card per failed/errored test', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [
            {
                case_id: 'a', display_name: 'broken assertion', outcome: 'failed',
                duration_seconds: 0.012, suite_name: 'tests/test_a.py',
                failure_details: 'AssertionError: expected x to equal y\n  at tests/test_a.py:42',
                extras: [],
            },
            {
                case_id: 'b', display_name: 'fixture exploded', outcome: 'error',
                duration_seconds: 0.001, suite_name: 'tests/test_b.py',
                failure_details: "TypeError: cannot read 'init' of undefined",
                extras: [],
            },
            { case_id: 'c', display_name: 'still works', outcome: 'passed', duration_seconds: 0.003, suite_name: 'tests/test_c.py', extras: [] },
        ],
    });
    // Two triage cards
    const triageCount = (html.match(/cvv-triage-card/g) || []).length;
    assert.ok(triageCount >= 2, `expected at least 2 triage cards, got ${triageCount}`);
    // Headline content for the failed test
    assert.match(html, /AssertionError: expected x to equal y/);
    // Headline content for the errored test
    assert.match(html, /TypeError: cannot read/);
    // Failed gets is-failed class; errored gets is-error.  Phase C
    // added the 3a inline-headline variant for single-line failures —
    // the "fixture exploded" case has no traceback body so its
    // headline renders with the inline class instead of the
    // two-row red-box class.  Accept either form.
    assert.match(html, /(cvv-headline|cvv-inline-headline) is-failed/);
    assert.match(html, /(cvv-headline|cvv-inline-headline) is-error/);
    // Phase D: passed cases live under the Passed group.
    assert.match(html, /cvv-group-passed/);
    // Phase D: failed and errored cases live in separate outcome
    // groups (Failed and Errored), each with its own count.
    assert.match(html, /cvv-group-failed/);
    assert.match(html, /cvv-group-error/);
    assert.match(html, /Failed<\/span><span class="cvv-summary">\(1\)/);
    assert.match(html, /Errored<\/span><span class="cvv-summary">\(1\)/);
    assert.match(html, /Passed<\/span><span class="cvv-summary">\(1\)/);
});

test('viewer: triage cards render stdout and stderr expanders', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'with output', outcome: 'failed',
            duration_seconds: 0.01, suite_name: 'tests/test_a.py',
            failure_details: 'AssertionError: nope',
            system_out: 'before assertion',
            system_err: 'WARNING: spooky',
            extras: [],
        }],
    });
    assert.match(html, /before assertion/);
    assert.match(html, /WARNING: spooky/);
});

test('viewer: errored case renders stderr expander COLLAPSED by default', () => {
    // Predictable-collapse rule: stdout, stderr, and the
    // traceback row all start closed regardless of outcome.  Previous
    // design auto-opened stderr for errored tests "because the crash
    // diagnostic lives in stderr" — but the predictable rule wins.
    // The user clicks ``stderr ▸`` when they want to drill in.
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'fixture error', outcome: 'error',
            failure_details: 'TypeError',
            system_err: 'fixture failure stderr',
            extras: [],
        }],
    });
    const stderrTag = html.match(/<details[^>]*"cvv-row"[^>]*>(?=<summary[^<]*<span[^>]*>[^<]*<\/span><span class="cvv-title">stderr<\/span>)/);
    assert.ok(stderrTag, `stderr <details> tag not found in: ${html.slice(0, 300)}…`);
    assert.doesNotMatch(stderrTag[0], /\bopen\b/,
        'stderr row must NOT carry the open attribute');
    assert.match(stderrTag[0], /aria-expanded="false"/,
        'stderr row must carry aria-expanded="false"');
});

test('viewer: empty payload renders cleanly with no triage and no browse', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({ junit_cases: [], status: 'passed' });
    assert.doesNotMatch(html, /cvv-triage-card/);
    assert.doesNotMatch(html, /cvv-row-browse/);
});

test('viewer: skipped tests appear in browse with skipped chip', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [
            { case_id: 'a', display_name: 'experimental', outcome: 'skipped', suite_name: 'tests/test_a.py', extras: [] },
        ],
    });
    assert.match(html, /1 skipped/);
    assert.match(html, /cvv-ico-skipped/);
});

test('viewer: legacy stdout_excerpt and stderr_excerpt appear in collapsed run-output expanders', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [],
        stdout_excerpt: ['line 1', 'line 2'],
        stderr_excerpt: ['err line'],
    });
    assert.match(html, /Run stdout/);
    assert.match(html, /Run stderr/);
    assert.match(html, /line 1\nline 2/);
});

test('viewer: action_sections render under a Validation artifacts expander when caller passes a renderer', () => {
    // Reviewer Blocker 2 (PR #6314): the viewer takes an explicit
    // renderer via ``options.renderActionSections`` and never reaches
    // into session_dialogs.js or any other module's globals.  The
    // ``loadViewer`` helper deliberately does NOT stub
    // ``renderValidationFailureActionSections`` — this test passes the
    // renderer directly through options.
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer(
        {
            status: 'failed',
            junit_cases: [],
            action_sections: [{ title: 'Validation Artifacts', actions: [{ type: 'open_path', label: 'Open Record' }] }],
        },
        {
            renderActionSections: (sections) => `<div data-test-action-sections-count="${sections.length}"></div>`,
        },
    );
    assert.match(html, /Validation artifacts/);
    assert.match(html, /data-test-action-sections-count="1"/);
});

test('viewer: action_sections are omitted when no renderer is passed (no hidden global lookup)', () => {
    // The viewer must NOT silently call into a global to render artifact
    // actions — that hidden cross-module dependency was the design smell
    // the reviewer flagged.  Without an explicit renderer the section is
    // simply omitted.
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [],
        action_sections: [{ title: 'Validation Artifacts', actions: [{ type: 'open_path', label: 'Open Record' }] }],
    });
    assert.doesNotMatch(html, /Validation artifacts/);
    assert.doesNotMatch(html, /cvv-artifacts/);
});

// ── failed_tests fallback (reviewer Blocker 1) ────────────────────────────

test('viewer: synthesizes triage cards from failed_tests when junit_cases is empty', () => {
    // Regression for PR #6314 reviewer Blocker 1: the canonical viewer
    // must surface the failing node IDs even when JUnit XML isn't
    // available (junit_cases empty) and the stdout excerpt doesn't carry
    // them.  Without this fallback only the chip-row count survives and
    // the operator can't tell *what* failed.
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        failed_tests: [
            'tests/unit/test_alpha.py::test_first',
            'tests/integration/test_beta.py::test_second',
            'tests/e2e/test_gamma.py::test_third',
        ],
        junit_cases: [],
        // stdout/stderr deliberately omit the node IDs so the *only*
        // surface for them is the synthesized triage cards.
        stdout_excerpt: ['================= short test summary ================='],
        stderr_excerpt: ['make: *** [validate] Error 2'],
    });
    // All three node IDs render as triage cards.
    assert.match(html, /tests\/unit\/test_alpha\.py::test_first/);
    assert.match(html, /tests\/integration\/test_beta\.py::test_second/);
    assert.match(html, /tests\/e2e\/test_gamma\.py::test_third/);
    // Three triage cards (one per synthesized failure).
    const triageCount = (html.match(/cvv-triage-card/g) || []).length;
    assert.strictEqual(triageCount, 3, `expected 3 synthesized triage cards, got ${triageCount}`);
    // Each card renders BOTH the inline-headline (in the summary row,
    // 1-line truncated) AND the body headline box (full text shown
    // when the card is expanded).  Phase D redesign (issue #6322)
    // dropped the inline/two-row variant distinction — every card has
    // the same shape.  So we expect 3 of each.  The two regexes are
    // disjoint: ``cvv-headline is-failed`` (body) does NOT match
    // inside ``cvv-inline-headline is-failed`` (summary) because the
    // ``-i`` between ``cvv-`` and ``headline`` breaks the substring.
    const inlineHeadlineCount = (html.match(/cvv-inline-headline is-failed/g) || []).length;
    const bodyHeadlineCount = (html.match(/cvv-headline is-failed/g) || []).length;
    assert.strictEqual(inlineHeadlineCount, 3, 'expected 3 inline headlines (one per card)');
    assert.strictEqual(bodyHeadlineCount, 3, 'expected 3 body headlines (one per card)');
    // And the fallback headline explains where the data came from so
    // the operator isn't confused by the missing traceback.
    assert.match(html, /No JUnit XML detail was available/);
});

test('viewer: failed_tests fallback skips node IDs already represented in junit_cases', () => {
    // When the parser populated *some* junit_cases, the viewer must not
    // double-render the same node ID as both a real case and a
    // synthesized fallback card.
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        failed_tests: [
            'tests/unit/test_one.py::test_a',  // already in junit_cases (display_name match)
            'tests/unit/test_two.py::test_b',  // missing — must be synthesized
        ],
        junit_cases: [{
            case_id: 'cid-1',
            display_name: 'tests/unit/test_one.py::test_a',
            outcome: 'failed',
            duration_seconds: 0.01,
            suite_name: 'tests/unit/test_one.py',
            failure_details: 'AssertionError: nope',
            system_out: '',
            system_err: '',
            extras: [],
        }],
    });
    // Exactly two triage cards: one real (with traceback) + one synthesized.
    const triageCount = (html.match(/cvv-triage-card/g) || []).length;
    assert.strictEqual(triageCount, 2, `expected 2 triage cards, got ${triageCount}`);
    // The real one carries the traceback headline.
    assert.match(html, /AssertionError: nope/);
    // The synthesized one carries the fallback explanation.
    assert.match(html, /No JUnit XML detail was available/);
    // And the node ID for the missing one shows up.
    assert.match(html, /tests\/unit\/test_two\.py::test_b/);
});

test('viewer: empty failed_tests + empty junit_cases renders no triage cards', () => {
    // Sanity: a clean passed run with neither junit_cases nor
    // failed_tests must not render any triage cards (regression guard
    // against the synthesis accidentally running on empty lists).
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [],
        failed_tests: [],
    });
    assert.doesNotMatch(html, /cvv-triage-card/);
});

// ── Plugin integration ────────────────────────────────────────────────────

test('plugin: agent-context plugin renders when case carries the namespace', () => {
    const ctx = loadViewerWithAgentPlugin();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'e2e drove issue', outcome: 'failed',
            failure_details: 'AssertionError: not completed',
            extras: [{
                namespace: 'io.agent-context',
                payload: {
                    issue_number: 4503,
                    issue_title: 'fixture cohort split',
                    final_state: 'blocked',
                    summary: 'agent retried 2x then blocked on validation',
                },
            }],
        }],
    });
    assert.match(html, /Linked issue · driven by orchestrator/);
    assert.match(html, /#4503/);
    assert.match(html, /fixture cohort split/);
    assert.match(html, /blocked/);
    assert.match(html, /Open issue drawer/);
    // PR #6319 Blocker 1: the Open-issue-drawer affordance must route
    // through the typed-Command pipeline (data-lifecycle-command on
    // a real button) so a click actually dispatches into
    // ``runE2ELifecycleCommand`` → ``openIssueTimeline``.  Plain
    // ``<a href="/api/...">`` had no real route behind it.
    const buttonMatch = html.match(/<button[^>]*data-lifecycle-command="([^"]+)"[^>]*>/);
    assert.ok(buttonMatch, `expected a data-lifecycle-command button; got: ${html.slice(0, 400)}…`);
    const decoded = buttonMatch[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&');
    const cmd = JSON.parse(decoded);
    assert.strictEqual(cmd.kind, 'open_issue_timeline');
    assert.strictEqual(cmd.issue_number, 4503);
    assert.strictEqual(cmd.scope_kind, 'dashboard');
});

test('plugin: agent-context plugin degrades to text-only when lifecycle_commands.js is not loaded', () => {
    // Generic JUnit consumers (tixmeup et al.) may register the
    // agent-context plugin but NOT load
    // ``lifecycle_commands.js`` — they don't have a typed-Command
    // pipeline.  The plugin must still render the linked-issue
    // summary (issue number, title, final state, summary text)
    // without throwing or emitting a broken button.  The
    // Open-issue-drawer button is simply omitted when the shared
    // command renderer is unavailable.
    //
    // This test loads only the viewer + the plugin (no
    // lifecycle_commands.js) and exercises a normal linked-issue
    // payload.  Without the guard in the plugin, the call to
    // ``_renderLifecycleCommandButton`` would ReferenceError.
    const ctx = loadViewer();
    const pluginSource = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/plugins/agent_context.js'),
        'utf8',
    );
    vm.runInContext(pluginSource, ctx, { filename: 'plugins/agent_context.js (no lifecycle bundle)' });
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'driven failure', outcome: 'failed',
            failure_details: 'AssertionError',
            extras: [{
                namespace: 'io.agent-context',
                payload: {
                    issue_number: 4503,
                    issue_title: 'fixture cohort split',
                    final_state: 'blocked',
                    summary: 'agent retried 2x then blocked on validation',
                },
            }],
        }],
    });
    // Plugin block still renders the data.
    assert.match(html, /Linked issue · driven by orchestrator/);
    assert.match(html, /#4503/);
    assert.match(html, /fixture cohort split/);
    assert.match(html, /blocked/);
    // ... but with no Open-issue-drawer button (no typed-Command
    // pipeline available to dispatch the click).
    assert.doesNotMatch(html, /data-lifecycle-command/);
    assert.doesNotMatch(html, /Open issue drawer/);
});

test('plugin: agent-context plugin ignores legacy run_url even if a stale payload passes one', () => {
    // PR #6319 round 2 (Blocker 2): an earlier iteration of the
    // plugin emitted ``<a href={run_url}>`` and the translator built a
    // ``/api/dashboard/issue/N`` URL that had no real backing route.
    // The runtime no longer cares about ``run_url`` — but an old
    // backend or third-party producer might still send it.  The
    // renderer must not surface that URL as a click target.  Prove
    // both directions: the URL string never appears in the output,
    // and there's no ``<a href>`` rendered at all (the drawer
    // affordance is the typed-Command button).
    const ctx = loadViewerWithAgentPlugin();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'stale payload', outcome: 'failed',
            failure_details: 'AssertionError',
            extras: [{
                namespace: 'io.agent-context',
                payload: {
                    issue_number: 4503,
                    issue_title: 'fixture cohort split',
                    final_state: 'blocked',
                    summary: 'stale payload',
                    // Stale field — should be ignored.
                    run_url: '/api/dashboard/issue/4503?focus=timeline',
                },
            }],
        }],
    });
    assert.doesNotMatch(html, /\/api\/dashboard\/issue/);
    assert.doesNotMatch(html, /<a\s[^>]*href=/);
});

test('plugin: agent-context plugin renders nothing when case lacks the namespace', () => {
    // Generic JUnit consumers don't populate ``extras``.  The plugin is
    // loaded but never invoked.  This is the tixmeup-style scenario.
    const ctx = loadViewerWithAgentPlugin();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'generic failure', outcome: 'failed',
            failure_details: 'AssertionError',
            extras: [],
        }],
    });
    assert.doesNotMatch(html, /agent-context/);
    assert.doesNotMatch(html, /Linked issue/);
});

test('plugin: agent-context rejects malformed payload (no issue_number)', () => {
    const ctx = loadViewerWithAgentPlugin();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'malformed extras', outcome: 'failed',
            failure_details: 'AssertionError',
            extras: [{ namespace: 'io.agent-context', payload: { /* missing issue_number */ } }],
        }],
    });
    assert.doesNotMatch(html, /Linked issue/);
});
