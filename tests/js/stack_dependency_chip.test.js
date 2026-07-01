const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function escapeHtml(value) {
    return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

function loadModule() {
    const context = {
        compactCardState: {
            computeCompactCardFingerprint: () => 'fingerprint',
        },
        cssEscape: (value) => String(value),
        document: {},
        escapeAttr: escapeHtml,
        escapeHtml,
        formatDashboardTimestamps: () => {},
        localStorage: { getItem: () => null, setItem: () => {} },
        window: { dashboardData: { queueRefreshSeconds: 0 }, location: { href: 'http://example.test/' } },
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/kanban_columns.js'),
            'utf8',
        ),
        context,
    );
    return context;
}

function stackDependency(overrides = {}) {
    return {
        issue_number: 20,
        mode: 'stack',
        has_stack_edges: true,
        gates: [
            { gate: 'work', open: true, reason_codes: [], reasons: [] },
            { gate: 'publish', open: false, reason_codes: ['predecessor_not_merged'], reasons: ['the predecessor has not merged yet'] },
        ],
        predecessors: [{ ref: '#10', mode: 'stack', state: 'unsatisfied', problem: null }],
        successors: [],
        blocked_gates: ['merge'],
        blocked_reason_codes: ['predecessor_not_merged'],
        stale: false,
        stale_reason_codes: [],
        stack_base_branch: null,
        ...overrides,
    };
}

test('stack chip is empty when there is no stack participation', () => {
    const { renderStackChipHtml } = loadModule();
    assert.strictEqual(renderStackChipHtml({}), '');
    assert.strictEqual(renderStackChipHtml({ stack_dependency: null }), '');
    assert.strictEqual(
        renderStackChipHtml({ stack_dependency: stackDependency({ has_stack_edges: false }) }),
        '',
    );
});

test('stack chip renders mode and a text status (not colour-only) when blocked', () => {
    const { renderStackChipHtml } = loadModule();
    const html = renderStackChipHtml({ stack_dependency: stackDependency({ blocked_gates: ['publish', 'merge'] }) });
    assert.match(html, /stack-chip--blocked/);
    assert.match(html, /class="stack-chip-mode">Stack</);
    // Status text conveys state without relying on colour, and counts extras.
    assert.match(html, /class="stack-chip-status">publish \+1 blocked</);
});

test('stack chip shows stale status', () => {
    const { renderStackChipHtml } = loadModule();
    const html = renderStackChipHtml({ stack_dependency: stackDependency({ stale: true, stale_reason_codes: ['approval_stale'] }) });
    assert.match(html, /stack-chip--stale/);
    assert.match(html, /class="stack-chip-status">stale</);
});

test('stack chip shows ready when no gate is blocked', () => {
    const { renderStackChipHtml } = loadModule();
    const html = renderStackChipHtml({ stack_dependency: stackDependency({ blocked_gates: [], stale: false }) });
    assert.match(html, /stack-chip--ok/);
    assert.match(html, /class="stack-chip-status">ready</);
});

test('stack chip labels a base-of-stack issue (successors, no predecessors)', () => {
    const { renderStackChipHtml } = loadModule();
    const html = renderStackChipHtml({
        stack_dependency: stackDependency({
            mode: 'none',
            predecessors: [],
            successors: [{ issue_number: 30, ref: '#30', mode: 'stack' }],
            gates: [],
            blocked_gates: [],
        }),
    });
    assert.match(html, /class="stack-chip-mode">Base</);
    // Title tooltip carries the chain context.
    assert.match(html, /before #30/);
});

test('decorative chip icon is aria-hidden', () => {
    const { renderStackChipHtml } = loadModule();
    const html = renderStackChipHtml({ stack_dependency: stackDependency() });
    assert.match(html, /<span class="stack-chip-icon" aria-hidden="true">/);
});

test('compact card embeds the stack chip when stack data is present', () => {
    const { renderCompactCardHtml } = loadModule();
    const card = {
        issue_number: 20,
        issue_label: '#20',
        title: 'Stacked slice',
        state_label: 'queued',
        show_stale_badge: false,
        orchestrator_labels: [],
        stack_dependency: stackDependency(),
    };
    const html = renderCompactCardHtml(card);
    assert.match(html, /card-stack/);
    assert.match(html, /stack-chip--blocked/);
});

test('compact card omits the stack chip when there is no stack data', () => {
    const { renderCompactCardHtml } = loadModule();
    const card = {
        issue_number: 21,
        issue_label: '#21',
        title: 'Plain slice',
        state_label: 'queued',
        show_stale_badge: false,
        orchestrator_labels: [],
    };
    const html = renderCompactCardHtml(card);
    assert.doesNotMatch(html, /stack-chip/);
});
