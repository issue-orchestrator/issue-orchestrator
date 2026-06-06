// Pure tests for the dashboard's single local timestamp formatter.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard/timestamp_formatting.js',
);

function loadFormatter() {
    const calls = [];
    const context = {
        console,
        Intl: {
            DateTimeFormat: function (locale, options) {
                calls.push({ locale, options });
                return {
                    format: (date) => `LOCAL:${date.toISOString()}`,
                };
            },
        },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(SOURCE, 'utf8'), context, {
        filename: 'timestamp_formatting.js',
    });
    return { context, calls };
}

test('formatTimestamp renders ISO values through one cached local timestamp formatter', () => {
    const { context, calls } = loadFormatter();

    assert.strictEqual(
        context.formatTimestamp('2026-05-07T12:34:56Z'),
        'LOCAL:2026-05-07T12:34:56.000Z',
    );
    assert.strictEqual(
        context.formatTimestamp('2026-05-08T12:34:56Z'),
        'LOCAL:2026-05-08T12:34:56.000Z',
    );
    assert.strictEqual(calls.length, 1);
    assert.deepEqual(calls[0].options, {
        year: 'numeric',
        month: 'short',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        timeZoneName: 'short',
    });
});

test('journey wrappers use the same local timestamp format as the dashboard', () => {
    const { context } = loadFormatter();

    assert.strictEqual(
        context.formatJourneyHeaderTimestamp('2026-05-07T12:34:56Z', 'old-label'),
        'LOCAL:2026-05-07T12:34:56.000Z',
    );
    assert.strictEqual(
        context.formatJourneyStepTimestamp('2026-05-07T12:34:56Z', 'old-label'),
        'LOCAL:2026-05-07T12:34:56.000Z',
    );
});

test('formatTimestamp handles common space-separated UTC timestamps', () => {
    const { context } = loadFormatter();

    assert.strictEqual(
        context.formatTimestamp('2026-05-07 12:34:56Z'),
        'LOCAL:2026-05-07T12:34:56.000Z',
    );
});

test('formatTimestamp returns the fallback for empty or invalid values', () => {
    const { context } = loadFormatter();

    assert.strictEqual(context.formatTimestamp('', 'Unavailable'), 'Unavailable');
    assert.strictEqual(context.formatTimestamp('not-a-timestamp', 'Unavailable'), 'Unavailable');
    assert.strictEqual(context.formatTimestamp('not-a-timestamp'), 'not-a-timestamp');
});

test('formatDashboardTimestamps hydrates declarative timestamp nodes', () => {
    const { context } = loadFormatter();
    const attributes = {};
    const child = {
        dataset: {
            dashboardTimestamp: '2026-05-07T12:34:56Z',
            dashboardTimestampFallback: '-',
        },
        textContent: 'Loading...',
        setAttribute: (name, value) => {
            attributes[name] = value;
        },
    };
    const root = {
        matches: () => false,
        querySelectorAll: (selector) => {
            assert.strictEqual(selector, '[data-dashboard-timestamp]');
            return [child];
        },
    };

    context.formatDashboardTimestamps(root);

    assert.strictEqual(child.textContent, 'LOCAL:2026-05-07T12:34:56.000Z');
    assert.strictEqual(attributes.title, 'LOCAL:2026-05-07T12:34:56.000Z');
});
