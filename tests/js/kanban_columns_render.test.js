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
        localStorage: {
            getItem: () => null,
            setItem: () => {},
        },
        window: {
            dashboardData: { queueRefreshSeconds: 0 },
            location: { href: 'http://example.test/' },
        },
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

function staleCard(overrides = {}) {
    return {
        issue_number: 4057,
        issue_label: 'M9-012 · #4057',
        title: 'Completed terminal item',
        state_label: 'completed',
        phase: 'Done',
        is_stale: true,
        stale_reason: 'Older than 15m stale threshold',
        show_stale_badge: true,
        issue_url: 'https://example.test/issues/4057',
        github_url: 'https://example.test/issues/4057',
        orchestrator_labels: [],
        ...overrides,
    };
}

test('compact card hides stale warning chrome when display flag is false', () => {
    const { renderCompactCardHtml } = loadModule();

    const html = renderCompactCardHtml(staleCard({ show_stale_badge: false }));

    assert.match(html, /data-stale="true"/);
    assert.match(html, /data-show-stale-badge="false"/);
    assert.doesNotMatch(html, /class="stale-dot"/);
    assert.doesNotMatch(html, /badge-stale/);
});

test('compact card shows stale warning chrome when display flag is true', () => {
    const { renderCompactCardHtml } = loadModule();

    const html = renderCompactCardHtml(staleCard());

    assert.match(html, /data-stale="true"/);
    assert.match(html, /data-show-stale-badge="true"/);
    assert.match(html, /class="stale-dot"/);
    assert.match(html, /badge-stale/);
});
