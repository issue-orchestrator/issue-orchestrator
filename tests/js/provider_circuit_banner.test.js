// JS-vm tests for the provider circuit-breaker outage banner (issue #5980).
//
// The banner renders from ``window.dashboardData.providerCircuit`` and must:
//   - stay hidden when no circuit is open,
//   - surface the summary + per-provider disclosure when a circuit opens,
//   - distinguish "Unavailable" (open) from "Recovering" (closed-but-tracked),
//   - toggle the DOM container's visibility.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function makeElement() {
    return { innerHTML: '', style: { display: '' } };
}

function loadModule(dashboardData) {
    const bannerEl = makeElement();
    const context = {
        console,
        window: { dashboardData: dashboardData || null },
        document: {
            getElementById: (id) => (id === 'providerCircuitBanner' ? bannerEl : null),
        },
        escapeHtml: (value) => String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;'),
        escapeAttr: (value) => String(value)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;'),
        formatTimestamp: (value) => (value ? `AT:${value}` : ''),
    };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/provider_circuit.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'provider_circuit.js' });
    return { context, bannerEl };
}

function openCircuit() {
    return {
        any_open: true,
        open_count: 1,
        open_providers: ['anthropic'],
        summary_text: 'Provider outage: anthropic unavailable — next retry in 4m 12s.',
        next_retry_at: '2026-07-10T12:04:12+00:00',
        entries: [
            {
                provider: 'anthropic',
                is_open: true,
                status_label: 'Unavailable',
                cooldown_remaining_label: '4m 12s',
                next_retry_at: '2026-07-10T12:04:12+00:00',
                consecutive_outages: 3,
                last_error_summary: 'HTTP 529 overloaded',
            },
        ],
    };
}

test('renders nothing when no circuit is provided', () => {
    const { context } = loadModule();
    assert.strictEqual(context.renderProviderCircuitBannerHtml(null), '');
    assert.strictEqual(context.renderProviderCircuitBannerHtml({ any_open: false, entries: [] }), '');
});

test('renders the outage banner for an open circuit', () => {
    const { context } = loadModule();
    const html = context.renderProviderCircuitBannerHtml(openCircuit());

    assert.match(html, /pcircuit-icon/);
    assert.match(html, /Provider outage: anthropic unavailable/);
    // Per-provider disclosure row: provider, Unavailable badge, cooldown, attempts, error.
    assert.match(html, /<details class="pcircuit-details">/);
    assert.match(html, /pcircuit-badge--open">Unavailable</);
    assert.match(html, /Retry in 4m 12s/);
    assert.match(html, /Attempts: 3/);
    assert.match(html, /HTTP 529 overloaded/);
    // Absolute retry time formatted via the shared timestamp helper.
    assert.match(html, /AT:2026-07-10T12:04:12/);
});

test('escapes untrusted provider error text', () => {
    const { context } = loadModule();
    const circuit = openCircuit();
    circuit.entries[0].last_error_summary = '<script>alert(1)</script>';
    const html = context.renderProviderCircuitBannerHtml(circuit);
    assert.doesNotMatch(html, /<script>alert/);
    assert.match(html, /&lt;script&gt;/);
});

test('renders a recovering provider without a cooldown', () => {
    const { context } = loadModule();
    const circuit = {
        any_open: true,
        open_count: 1,
        open_providers: ['openai'],
        summary_text: 'Provider outage: openai unavailable — next retry in 1m.',
        next_retry_at: '2026-07-10T12:01:00+00:00',
        entries: [
            {
                provider: 'openai', is_open: true, status_label: 'Unavailable',
                cooldown_remaining_label: '1m', next_retry_at: '2026-07-10T12:01:00+00:00',
                consecutive_outages: 1, last_error_summary: null,
            },
            {
                provider: 'gemini', is_open: false, status_label: 'Recovering',
                cooldown_remaining_label: null, next_retry_at: null,
                consecutive_outages: 2, last_error_summary: null,
            },
        ],
    };
    const html = context.renderProviderCircuitBannerHtml(circuit);
    assert.match(html, /pcircuit-badge--recovering">Recovering</);
    assert.match(html, /pcircuit-row--recovering/);
});

test('updateProviderCircuitBanner shows the container for an open circuit', () => {
    const { context, bannerEl } = loadModule();
    context.updateProviderCircuitBanner(openCircuit());
    assert.strictEqual(bannerEl.style.display, 'flex');
    assert.match(bannerEl.innerHTML, /Provider outage/);
});

test('updateProviderCircuitBanner hides the container when healthy', () => {
    const { context, bannerEl } = loadModule();
    // First open, then clear.
    context.updateProviderCircuitBanner(openCircuit());
    context.updateProviderCircuitBanner({ any_open: false, entries: [] });
    assert.strictEqual(bannerEl.style.display, 'none');
    assert.strictEqual(bannerEl.innerHTML, '');
});

test('renderProviderCircuitFromDashboardData reads the shared dashboard data', () => {
    const { context, bannerEl } = loadModule({ providerCircuit: openCircuit() });
    context.renderProviderCircuitFromDashboardData();
    assert.strictEqual(bannerEl.style.display, 'flex');
    assert.match(bannerEl.innerHTML, /anthropic/);
});

// Issue #5980: a failed circuit read must surface as a warning banner, never
// be hidden like a healthy fleet — a broken read cannot masquerade as "no
// outage".
function unavailableCircuit() {
    return {
        any_open: false,
        open_count: 0,
        open_providers: [],
        summary_text: 'Provider circuit status unavailable — could not read circuit state.',
        next_retry_at: null,
        entries: [],
        status_unavailable: true,
    };
}

test('renders a warning banner when circuit status is unavailable', () => {
    const { context } = loadModule();
    const html = context.renderProviderCircuitBannerHtml(unavailableCircuit());
    // Not hidden, even though any_open is false.
    assert.notStrictEqual(html, '');
    assert.match(html, /pcircuit-icon/);
    assert.match(html, /pcircuit-body--unavailable/);
    assert.match(html, /Provider circuit status unavailable/);
});

test('renders a default warning when unavailable circuit lacks a summary', () => {
    const { context } = loadModule();
    const circuit = unavailableCircuit();
    circuit.summary_text = '';
    const html = context.renderProviderCircuitBannerHtml(circuit);
    assert.match(html, /Provider circuit status unavailable/);
});

test('updateProviderCircuitBanner shows the container when status is unavailable', () => {
    const { context, bannerEl } = loadModule();
    context.updateProviderCircuitBanner(unavailableCircuit());
    assert.strictEqual(bannerEl.style.display, 'flex');
    assert.match(bannerEl.innerHTML, /unavailable/);
});

test('a healthy circuit (not open, not unavailable) still renders nothing', () => {
    const { context } = loadModule();
    const html = context.renderProviderCircuitBannerHtml({
        any_open: false, entries: [], status_unavailable: false,
    });
    assert.strictEqual(html, '');
});
