// Tests for the inline E2E runs-as-rows list (issue #6334).  No DOM,
// no Playwright — pure vm.runInContext mounting of
// ``e2e_runs_list.js`` plus ``lifecycle_commands.js`` so the
// dispatcher and renderer exercise the same single-owner pipeline
// every typed Command in the dashboard rides.
//
// Layers covered:
//
//   A. Pure render: ``renderE2ERunsList(payload)`` produces N rows,
//      each a ``<details>`` with a typed ``expand_e2e_run`` Command
//      in ``data-lifecycle-command`` + an ``ontoggle`` that
//      dispatches via ``runE2ELifecycleCommandFromToggle``.
//
//   B. Predictable-collapse: rows are closed by default; the
//      ``runs: []`` case renders the empty state.
//
//   C. Per-tone color spans: ``outcome.tone`` drives the row's
//      tone class + count-span color classes — no string-matching,
//      no silent green for unknown tones (the OutcomeBadge contract
//      from PR #6333).
//
//   D. Dispatcher round-trip: render → extract Command from
//      ``data-lifecycle-command`` → dispatch via
//      ``runE2ELifecycleCommandFromToggle`` → assert
//      ``/api/e2e-run-detail/{n}?view=user`` was fetched.
//
//   E. Re-routed ``open_e2e_run``: the typed Command that used to
//      open the modal now expands the matching row (and opens the
//      inner "Run details & artifacts" disclosure when
//      ``expand_run_details: true``).  No modal-driver call survives.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DASHBOARD_JS_DIR = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard',
);

function _baseStubs() {
    return {
        console,
        URLSearchParams,
        Map,
        Set,
        Promise,
        setTimeout: (fn, _ms) => fn,
        Number,
        document: {
            readyState: 'complete',
            addEventListener: () => {},
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
        },
        navigator: {},
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
        _humanizeSnakeCase: (s) => String(s || ''),
        showToast: () => {},
        // Canonical viewer helpers — the row loader calls into them.
        // Default to identity-shaped stubs; individual tests can
        // override if they need to assert specific calls.
        renderE2EResultsPanel: (data) =>
            `<div class="cvv-root" data-test-stub-run-id="${data && data.run ? data.run.id : ''}"></div>`,
        renderE2ETimeline: () => {},
        normalizeE2ETimelineData: (data) => data,
        enhanceCanonicalValidationViewerAccessibility: () => {},
    };
}

function _loadRunsListModule(extra = {}) {
    const ctx = { ..._baseStubs(), ...extra };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(
        fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'e2e_runs_list.js'), 'utf8'),
        ctx,
        { filename: 'e2e_runs_list.js' },
    );
    return ctx;
}

function _loadRunsListPlusDispatcher(extra = {}) {
    const ctx = _loadRunsListModule(extra);
    vm.runInContext(
        fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'lifecycle_commands.js'), 'utf8'),
        ctx,
        { filename: 'lifecycle_commands.js' },
    );
    return ctx;
}

function _ob(label, tone) {
    return { label, tone };
}

function _row(runId, overrides = {}) {
    return {
        run_id: runId,
        outcome: 'outcome' in overrides ? overrides.outcome : _ob('Passed', 'passed'),
        started_at: '2026-05-12T10:00:00Z',
        finished_at: overrides.finished_at !== undefined ? overrides.finished_at : '2026-05-12T10:05:00Z',
        duration_seconds: overrides.duration_seconds !== undefined ? overrides.duration_seconds : 300.0,
        commit_sha: overrides.commit_sha !== undefined ? overrides.commit_sha : 'abc1234',
        branch: overrides.branch !== undefined ? overrides.branch : 'main',
        runner_kind: 'pytest',
        command_summary: 'pytest tests/e2e',
        results: overrides.results || {
            passed: 36, failed: 0, errored: 0, skipped: 0, quarantined: 0, total: 36,
        },
        note: overrides.note || null,
        expand_command: {
            kind: 'expand_e2e_run',
            label: 'Expand E2E Run',
            run_id: runId,
        },
    };
}

// ── Layer A + B: pure render, predictable collapse ─────────────────

test('renderE2ERunsList: produces N <details> rows each carrying typed expand_e2e_run Command', () => {
    const ctx = _loadRunsListModule();
    const payload = {
        runs: [_row(101), _row(102, { outcome: _ob('Failed', 'failed') })],
    };
    const html = ctx.renderE2ERunsList(payload);

    assert.ok(html.includes('<div class="e2e-runs-list"'), 'must render a runs-list root');
    // Two rows: ``<details class="e2e-run-row ...``.
    const rowMatches = html.match(/<details class="e2e-run-row /g);
    assert.strictEqual(rowMatches.length, 2, 'expected 2 rows');
    // Closed by default — no ``open`` attribute on the <details>.
    assert.ok(!/<details[^>]*\sopen[\s>]/.test(html), 'rows must be closed by default');
    // Each row carries a typed ``data-lifecycle-command`` AND the
    // shared toggle dispatcher (single-owner contract).
    const cmdMatches = html.match(/data-lifecycle-command="([^"]+)"/g);
    assert.strictEqual(cmdMatches.length, 2, 'each row must carry data-lifecycle-command');
    assert.ok(
        (html.match(/ontoggle="runE2ELifecycleCommandFromToggle\(this\)"/g) || []).length === 2,
        'each row must dispatch via runE2ELifecycleCommandFromToggle',
    );
    assert.ok(
        html.includes('id="e2e-run-row-summary-101" aria-controls="e2e-run-row-content-101"'),
        'summary must name the controlled row detail region for assistive tech',
    );
    assert.ok(
        html.includes('id="e2e-run-row-content-101" role="region" aria-labelledby="e2e-run-row-summary-101"'),
        'expanded body must be a labelled region tied to the summary',
    );
    // No bespoke per-element handler.
    assert.ok(!html.includes('_handleE2ERunRow'), 'no legacy per-element handler may survive');
});

test('renderE2ERunRow: summary/body accessibility ids are unique per run', () => {
    const ctx = _loadRunsListModule();
    const html = ctx.renderE2ERunsList({ runs: [_row(88), _row(99)] });

    for (const runId of [88, 99]) {
        assert.ok(
            html.includes(`id="e2e-run-row-summary-${runId}" aria-controls="e2e-run-row-content-${runId}"`),
            `run ${runId} summary must point at its own body`,
        );
        assert.ok(
            html.includes(`id="e2e-run-row-content-${runId}" role="region" aria-labelledby="e2e-run-row-summary-${runId}"`),
            `run ${runId} body must be labelled by its own summary`,
        );
    }
});

test('renderE2ERunsList: empty payload renders the empty state', () => {
    const ctx = _loadRunsListModule();
    assert.ok(ctx.renderE2ERunsList({ runs: [] }).includes('No E2E run history'));
    assert.ok(ctx.renderE2ERunsList(null).includes('No E2E run history'));
});

test('renderE2ERunsList: single-run case still renders as a 1-element list', () => {
    // Issue #6334 spec line: "Single-run case renders as a 1-element
    // list so the idiom holds even with one run."
    const ctx = _loadRunsListModule();
    const html = ctx.renderE2ERunsList({ runs: [_row(88)] });
    const rowMatches = html.match(/<details class="e2e-run-row /g);
    assert.strictEqual(rowMatches.length, 1);
});

// ── Layer C: per-tone color matrix ─────────────────────────────────

const _TONES_FROM_PAYLOAD = [
    { tone: 'passed', label: 'Passed', expectedRowClass: 'e2e-run-row-passed' },
    { tone: 'failed', label: 'Failed', expectedRowClass: 'e2e-run-row-failed' },
    { tone: 'error', label: 'Error', expectedRowClass: 'e2e-run-row-error' },
    { tone: 'in_progress', label: 'Running', expectedRowClass: 'e2e-run-row-in_progress' },
    { tone: 'neutral', label: 'Canceled', expectedRowClass: 'e2e-run-row-neutral' },
];

for (const { tone, label, expectedRowClass } of _TONES_FROM_PAYLOAD) {
    test(`renderE2ERunRow: tone='${tone}' wires through to the row + outcome classes`, () => {
        const ctx = _loadRunsListModule();
        const html = ctx.renderE2ERunsList({
            runs: [_row(1, { outcome: _ob(label, tone) })],
        });
        assert.ok(
            html.includes(expectedRowClass),
            `expected ${expectedRowClass} on the row for tone=${tone}`,
        );
        assert.ok(
            html.includes(`e2e-run-row-outcome-${tone}`),
            `expected outcome class for tone=${tone}`,
        );
        assert.ok(
            html.includes(`cvv-ico-${tone}`),
            `expected cvv-ico tone class for tone=${tone}`,
        );
        assert.ok(html.includes(label), 'outcome label must render');
    });
}

test('renderE2ERunRow: unknown / missing tone falls through to neutral (no silent green)', () => {
    // OutcomeBadge contract: unknown tone MUST NOT classify as
    // ``passed``.  This is the PR #6333 silent-green bug applied
    // to the new runs-list shape.
    const ctx = _loadRunsListModule();
    const cases = [
        { outcome: _ob('Mystery state', 'unknown-tone') },
        { outcome: { label: 'Bare label' } },  // missing tone
        { outcome: null },
        { outcome: undefined },
    ];
    for (const overrides of cases) {
        const html = ctx.renderE2ERunsList({ runs: [_row(1, overrides)] });
        assert.ok(
            html.includes('e2e-run-row-outcome-neutral'),
            `expected neutral fallback for ${JSON.stringify(overrides)}`,
        );
        assert.ok(
            !html.includes('e2e-run-row-outcome-passed'),
            `unknown / missing tone must NOT render as passed (silent-green guard)`,
        );
    }
});

test('renderE2ERunRow: per-outcome counts render with the right tone color classes', () => {
    const ctx = _loadRunsListModule();
    const html = ctx.renderE2ERunsList({
        runs: [_row(1, {
            outcome: _ob('Failed', 'failed'),
            results: { passed: 36, failed: 1, errored: 0, skipped: 2, quarantined: 0, total: 39 },
        })],
    });
    assert.ok(html.includes('1 failed') && html.includes('e2e-run-count-failed'),
        'failed count must carry the failed tone class');
    assert.ok(html.includes('36 passed') && html.includes('e2e-run-count-passed'),
        'passed count must carry the passed tone class');
    assert.ok(html.includes('2 skipped') && html.includes('e2e-run-count-neutral'),
        'skipped count must carry the neutral tone class');
    // Zero-count buckets are omitted (compact display).
    assert.ok(!html.includes('0 failed'));
    assert.ok(!html.includes('0 errored'));
});

// ── Layer D: render → extract → dispatch round-trip ────────────────

function _attachFakeFetch(ctx) {
    const calls = { fetch: [] };
    ctx.fetch = (url) => {
        calls.fetch.push(url);
        return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({
                run: { id: 88, started_at: '2026-05-12T10:00:00Z', status: 'passed', commit_sha: 'abc' },
                results_summary: {},
            }),
        });
    };
    return calls;
}

function _fakeRow(runId, payloadAttr, contentEl) {
    return {
        open: true,
        dataset: {
            e2eRunId: String(runId),
            loaded: '',
            lifecycleCommand: payloadAttr,
        },
        querySelector(sel) {
            if (sel === '.e2e-run-row-content') return contentEl;
            if (sel === '.cvv-root') return null;
            // ``.e2e-timeline-content`` is the row-scoped class
            // that replaced ``#e2eTimelineContent`` (PR #6336 round-2).
            if (sel === '.e2e-timeline-content') return null;
            return null;
        },
    };
}

test('dispatcher round-trip: toggling a row calls loadE2ERunIntoRow and fetches /api/e2e-run-detail', async () => {
    const ctx = _loadRunsListPlusDispatcher();
    const calls = _attachFakeFetch(ctx);

    // Render a row, then extract the typed Command from the
    // rendered HTML (the same path a real toggle would hit).
    const html = ctx.renderE2ERunsList({ runs: [_row(88)] });
    const match = html.match(/data-lifecycle-command="([^"]+)"/);
    assert.ok(match, 'rendered HTML must carry data-lifecycle-command');
    const cmdRaw = match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&');
    const cmd = JSON.parse(cmdRaw);
    assert.deepStrictEqual(cmd, {
        kind: 'expand_e2e_run',
        label: 'Expand E2E Run',
        run_id: 88,
    });

    // Wire up a fake <details> element and dispatch through the
    // shared toggle helper — the same hook the rendered ``ontoggle``
    // attribute would fire.
    const contentEl = { innerHTML: '', querySelector: () => null };
    const detailsEl = _fakeRow(88, cmdRaw, contentEl);
    ctx.runE2ELifecycleCommandFromToggle(detailsEl);

    // The dispatcher routes ``expand_e2e_run`` → ``loadE2ERunIntoRow``
    // → ``fetch('/api/e2e-run-detail/88?view=user')``.  Async — wait
    // for the microtask queue to drain.
    assert.strictEqual(detailsEl.dataset.loaded, '1', 'row must mark itself loaded');
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepStrictEqual(calls.fetch, ['/api/e2e-run-detail/88?view=user']);
});

test('dispatcher round-trip: re-opening a loaded row is a no-op (predictable-collapse rule)', async () => {
    const ctx = _loadRunsListPlusDispatcher();
    const calls = _attachFakeFetch(ctx);
    const html = ctx.renderE2ERunsList({ runs: [_row(88)] });
    const match = html.match(/data-lifecycle-command="([^"]+)"/);
    const cmdRaw = match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&');
    const contentEl = { innerHTML: '', querySelector: () => null };
    const detailsEl = _fakeRow(88, cmdRaw, contentEl);
    // Already loaded → re-toggle must not fetch again.
    detailsEl.dataset.loaded = '1';
    ctx.runE2ELifecycleCommandFromToggle(detailsEl);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepStrictEqual(calls.fetch, [], 're-opening must NOT re-fetch (dataset.loaded === "1")');
});

test('dispatcher round-trip: closed <details> does not fire the loader', async () => {
    const ctx = _loadRunsListPlusDispatcher();
    const calls = _attachFakeFetch(ctx);
    const html = ctx.renderE2ERunsList({ runs: [_row(88)] });
    const match = html.match(/data-lifecycle-command="([^"]+)"/);
    const cmdRaw = match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&');
    const contentEl = { innerHTML: '', querySelector: () => null };
    const detailsEl = _fakeRow(88, cmdRaw, contentEl);
    detailsEl.open = false;
    ctx.runE2ELifecycleCommandFromToggle(detailsEl);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepStrictEqual(calls.fetch, []);
});

// ── Layer E: re-routed open_e2e_run ───────────────────────────────

test('open_e2e_run dispatcher branch: re-routes to expandE2ERunRow, NOT to showUnifiedRunView', () => {
    const ctx = _loadRunsListPlusDispatcher();
    const calls = { expand: [] };
    // Override ``expandE2ERunRow`` to assert the dispatcher hits it.
    ctx.expandE2ERunRow = (runId, options) => {
        calls.expand.push({ runId, options });
        return true;
    };
    ctx.showUnifiedRunView = () => {
        throw new Error('showUnifiedRunView was removed in #6334; open_e2e_run must route via expandE2ERunRow');
    };
    ctx.runE2ELifecycleCommand({
        kind: 'open_e2e_run',
        label: 'Open E2E Run',
        run_id: 88,
        expand_run_details: true,
    });
    // Cross-vm prototype mismatch makes deepStrictEqual unreliable
    // here — assert on field values directly.
    assert.strictEqual(calls.expand.length, 1);
    assert.strictEqual(calls.expand[0].runId, 88);
    assert.strictEqual(calls.expand[0].options.expandRunDetails, true);
});

test('expandE2ERunRow: opens the matching <details> by run_id and scrolls it into view', () => {
    const ctx = _loadRunsListModule();
    // Simulate the runs list rendered in the DOM: register a fake
    // <details> with the right data-e2e-run-id.
    let scrollCalls = 0;
    const matchingRow = {
        open: false,
        dataset: { e2eRunId: '88' },
        scrollIntoView: () => { scrollCalls++; },
        querySelector: () => null,  // no disclosure yet
    };
    ctx.document.querySelector = (sel) => {
        if (sel === 'details.e2e-run-row[data-e2e-run-id="88"]') return matchingRow;
        return null;
    };
    const opened = ctx.expandE2ERunRow(88);
    assert.strictEqual(opened, true);
    assert.strictEqual(matchingRow.open, true, 'row must be opened');
    assert.strictEqual(scrollCalls, 1, 'row must scroll into view');
});

test('expandE2ERunRow: missing row toasts and returns false (no modal fallback)', () => {
    const ctx = _loadRunsListModule();
    const toasts = [];
    ctx.showToast = (msg, kind) => { toasts.push({ msg, kind }); };
    ctx.document.querySelector = () => null;
    const opened = ctx.expandE2ERunRow(999);
    assert.strictEqual(opened, false);
    assert.strictEqual(toasts.length, 1);
    assert.ok(toasts[0].msg.includes('Run #999'));
});
