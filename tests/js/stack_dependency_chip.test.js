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

// The chip renders from the server-precomputed card.stack_chip. The tone/label/
// status/title *logic* is covered by the Python projection tests
// (test_dependency_gate_view.py::stack_chip); these assert markup assembly.
function stackChip(overrides = {}) {
    return {
        tone: 'blocked',
        mode_label: 'Stack',
        status_text: 'publish +1 blocked',
        title: 'Stack: publish +1 blocked — after #10',
        ...overrides,
    };
}

test('stack chip is empty when there is no precomputed chip', () => {
    const { renderStackChipHtml } = loadModule();
    assert.strictEqual(renderStackChipHtml({}), '');
    assert.strictEqual(renderStackChipHtml({ stack_chip: null }), '');
});

test('stack chip renders mode and a text status (not colour-only) when blocked', () => {
    const { renderStackChipHtml } = loadModule();
    const html = renderStackChipHtml({ stack_chip: stackChip() });
    assert.match(html, /stack-chip--blocked/);
    assert.match(html, /class="stack-chip-mode">Stack</);
    // Status text conveys state without relying on colour.
    assert.match(html, /class="stack-chip-status">publish \+1 blocked</);
});

test('stack chip shows stale status', () => {
    const { renderStackChipHtml } = loadModule();
    const html = renderStackChipHtml({ stack_chip: stackChip({ tone: 'stale', status_text: 'stale' }) });
    assert.match(html, /stack-chip--stale/);
    assert.match(html, /class="stack-chip-status">stale</);
});

test('stack chip shows ready when no gate is blocked', () => {
    const { renderStackChipHtml } = loadModule();
    const html = renderStackChipHtml({ stack_chip: stackChip({ tone: 'ok', status_text: 'ready' }) });
    assert.match(html, /stack-chip--ok/);
    assert.match(html, /class="stack-chip-status">ready</);
});

test('stack chip renders the mode label and title chain context', () => {
    const { renderStackChipHtml } = loadModule();
    const html = renderStackChipHtml({
        stack_chip: stackChip({ mode_label: 'Base', title: 'Base: ready — before #30' }),
    });
    assert.match(html, /class="stack-chip-mode">Base</);
    assert.match(html, /before #30/);
});

test('decorative chip icon is aria-hidden', () => {
    const { renderStackChipHtml } = loadModule();
    const html = renderStackChipHtml({ stack_chip: stackChip() });
    assert.match(html, /<span class="stack-chip-icon" aria-hidden="true">/);
});

test('compact card embeds the stack chip when a precomputed chip is present', () => {
    const { renderCompactCardHtml } = loadModule();
    const card = {
        issue_number: 20,
        issue_label: '#20',
        title: 'Stacked slice',
        state_label: 'queued',
        show_stale_badge: false,
        orchestrator_labels: [],
        stack_chip: stackChip(),
    };
    const html = renderCompactCardHtml(card);
    assert.match(html, /card-stack/);
    assert.match(html, /stack-chip--blocked/);
});

test('compact card omits the stack chip when there is no chip', () => {
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
