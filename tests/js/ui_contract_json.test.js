// Shared fail-closed JSON readers (issue #6337).
//
// One test per browser JSON boundary the dashboard has — DOM data-*,
// inline <script type="application/json">, fetch().json(), and
// event/stream data — proving each one:
//   * returns the payload when it satisfies the contract,
//   * returns null (never a partial object) when it does not,
//   * reports exactly one diagnostic per rejection.
//
// The "returns null" half is the point: feature code fails closed by
// checking for null, so a reader that returned a half-valid object would
// silently defeat the whole layer.

const test = require('node:test');
const assert = require('node:assert');

const uiContractJson = require('../../src/issue_orchestrator/static/js/ui_contract_json.js');

const COMMAND = 'TimelineCommandPayload';
const VALID = { kind: 'open_e2e_run', label: 'Open E2E Run', run_id: 88 };

// Every test captures diagnostics rather than letting them hit the
// console/toast default, and restores the default afterwards.
function withCapture(fn) {
    const violations = [];
    uiContractJson.setViolationReporter((violation) => violations.push(violation));
    try {
        return fn(violations);
    } finally {
        uiContractJson.setViolationReporter(null);
    }
}

async function withCaptureAsync(fn) {
    const violations = [];
    uiContractJson.setViolationReporter((violation) => violations.push(violation));
    try {
        return await fn(violations);
    } finally {
        uiContractJson.setViolationReporter(null);
    }
}

function datasetElement(raw) {
    return { dataset: { lifecycleCommand: raw } };
}

function jsonResponse(body, url = '/api/test') {
    return { url, text: async () => body };
}

// ── DOM data-* payloads ─────────────────────────────────────────────

test('fromDataset: valid data-* JSON returns the payload', () => {
    withCapture((violations) => {
        const value = uiContractJson.fromDataset(
            datasetElement(JSON.stringify(VALID)), 'lifecycleCommand', COMMAND,
        );
        assert.deepEqual(value, VALID);
        assert.deepEqual(violations, []);
    });
});

test('fromDataset: contract-violating data-* JSON returns null and reports once', () => {
    withCapture((violations) => {
        const value = uiContractJson.fromDataset(
            datasetElement(JSON.stringify({ kind: 'open_e2e_run', run_id: 0 })),
            'lifecycleCommand',
            COMMAND,
        );
        assert.strictEqual(value, null);
        assert.strictEqual(violations.length, 1);
        assert.strictEqual(violations[0].schemaName, COMMAND);
        assert.strictEqual(violations[0].source, 'data-lifecycle-command');
        assert.match(violations[0].errors.join(' '), /run_id: expected >= 1/);
    });
});

test('fromDataset: unparseable data-* JSON returns null and names the boundary', () => {
    withCapture((violations) => {
        const value = uiContractJson.fromDataset(datasetElement('{not json'), 'lifecycleCommand', COMMAND);
        assert.strictEqual(value, null);
        assert.strictEqual(violations.length, 1);
        assert.match(violations[0].detail, /not valid JSON/);
        assert.strictEqual(violations[0].source, 'data-lifecycle-command');
    });
});

test('fromDataset: a missing attribute or dataset is reported, not thrown', () => {
    withCapture((violations) => {
        assert.strictEqual(uiContractJson.fromDataset(datasetElement(''), 'lifecycleCommand', COMMAND), null);
        assert.strictEqual(uiContractJson.fromDataset(null, 'lifecycleCommand', COMMAND), null);
        assert.strictEqual(violations.length, 2);
    });
});

// ── inline <script type="application/json"> bootstraps ──────────────

test('fromInlineScript: valid inline JSON returns the payload', () => {
    withCapture((violations) => {
        const node = { id: 'recentE2ERunsData', textContent: '{"runs": []}' };
        assert.deepEqual(uiContractJson.fromInlineScript(node, 'RecentE2ERunsPayload'), { runs: [] });
        assert.deepEqual(violations, []);
    });
});

test('fromInlineScript: contract-violating inline JSON returns null (no half-built render)', () => {
    withCapture((violations) => {
        const node = { id: 'recentE2ERunsData', textContent: '{"runs": {}}' };
        assert.strictEqual(uiContractJson.fromInlineScript(node, 'RecentE2ERunsPayload'), null);
        assert.strictEqual(violations.length, 1);
        assert.match(violations[0].source, /recentE2ERunsData/);
    });
});

test('fromInlineScript: a missing element is reported, not thrown', () => {
    withCapture((violations) => {
        assert.strictEqual(uiContractJson.fromInlineScript(null, 'RecentE2ERunsPayload'), null);
        assert.match(violations[0].detail, /inline JSON element is missing/);
    });
});

// ── fetch responses ─────────────────────────────────────────────────

test('fromResponse: a valid body returns the payload', async () => {
    await withCaptureAsync(async (violations) => {
        const value = await uiContractJson.fromResponse(
            jsonResponse('{"runs": []}', '/api/e2e-runs/recent'), 'RecentE2ERunsPayload',
        );
        assert.deepEqual(value, { runs: [] });
        assert.deepEqual(violations, []);
    });
});

test('fromResponse: a contract-violating body returns null and names the endpoint', async () => {
    await withCaptureAsync(async (violations) => {
        const value = await uiContractJson.fromResponse(
            jsonResponse('{"runs": [], "unexpected": 1}', '/api/e2e-runs/recent'), 'RecentE2ERunsPayload',
        );
        assert.strictEqual(value, null);
        assert.strictEqual(violations.length, 1);
        assert.strictEqual(violations[0].source, '/api/e2e-runs/recent');
        assert.match(violations[0].errors.join(' '), /unexpected: unexpected property/);
    });
});

test('fromResponse: an HTML error page where JSON was expected returns null', async () => {
    // The classic "server returned a 500 page" case: previously this
    // produced a confusing downstream TypeError deep in a renderer.
    await withCaptureAsync(async (violations) => {
        const value = await uiContractJson.fromResponse(
            jsonResponse('<html>500</html>'), 'RecentE2ERunsPayload',
        );
        assert.strictEqual(value, null);
        assert.match(violations[0].detail, /not valid JSON/);
    });
});

test('fromResponse: an unreadable body is reported, not thrown', async () => {
    await withCaptureAsync(async (violations) => {
        const response = { url: '/api/x', text: async () => { throw new Error('socket closed'); } };
        assert.strictEqual(await uiContractJson.fromResponse(response, 'RecentE2ERunsPayload'), null);
        assert.match(violations[0].detail, /could not be read \(socket closed\)/);
    });
});

// ── error bodies (deliberately uncontracted) ────────────────────────

test('errorMessage: prefers the server-provided error field', async () => {
    const response = { ok: false, status: 404, text: async () => '{"error": "run 88 is gone"}' };
    assert.strictEqual(await uiContractJson.errorMessage(response), 'run 88 is gone');
});

test('errorMessage: falls back to FastAPI-style detail', async () => {
    const response = { ok: false, status: 422, text: async () => '{"detail": "bad nodeid"}' };
    assert.strictEqual(await uiContractJson.errorMessage(response), 'bad nodeid');
});

test('errorMessage: a non-JSON error body degrades to the status', async () => {
    const response = { ok: false, status: 502, text: async () => '<html>502 Bad Gateway</html>' };
    assert.strictEqual(await uiContractJson.errorMessage(response), 'HTTP 502');
});

test('errorMessage: an unreadable body degrades to the status rather than throwing', async () => {
    const response = { ok: false, status: 500, text: async () => { throw new Error('socket closed'); } };
    assert.strictEqual(await uiContractJson.errorMessage(response), 'HTTP 500');
});

test('errorMessage: the caller-supplied fallback wins over the bare status', async () => {
    const response = { ok: false, status: 500, text: async () => '{}' };
    assert.strictEqual(
        await uiContractJson.errorMessage(response, 'Failed to load run details'),
        'Failed to load run details',
    );
});

test('errorMessage: never reports a contract violation — error bodies have no schema', async () => {
    await withCaptureAsync(async (violations) => {
        const response = { ok: false, status: 500, text: async () => '{"error": "boom"}' };
        assert.strictEqual(await uiContractJson.errorMessage(response), 'boom');
        assert.deepEqual(violations, [], 'an uncontracted body is not a contract failure');
    });
});

// ── streaming / event payloads ──────────────────────────────────────

test('fromEventData: a valid event payload returns the payload', () => {
    withCapture((violations) => {
        const value = uiContractJson.fromEventData(JSON.stringify(VALID), COMMAND, 'sse:lifecycle');
        assert.deepEqual(value, VALID);
        assert.deepEqual(violations, []);
    });
});

test('fromEventData: a contract-violating event payload returns null and reports once', () => {
    withCapture((violations) => {
        const value = uiContractJson.fromEventData('{"kind": "open_e2e_run"}', COMMAND, 'sse:lifecycle');
        assert.strictEqual(value, null);
        assert.strictEqual(violations.length, 1);
        assert.strictEqual(violations[0].source, 'sse:lifecycle');
    });
});

// ── diagnostics ─────────────────────────────────────────────────────

test('violations describe the schema, the boundary, and the specific errors', () => {
    withCapture((violations) => {
        uiContractJson.fromDataset(
            datasetElement('{"kind": "open_e2e_run", "label": "x", "run_id": "88"}'),
            'lifecycleCommand',
            COMMAND,
        );
        const message = uiContractJson.describeViolation(violations[0]);
        assert.match(message, /TimelineCommandPayload/);
        assert.match(message, /data-lifecycle-command/);
        assert.match(message, /run_id: expected integer, got string "88"/);
    });
});

test('the default reporter is restorable and reports through console.error', () => {
    const errors = [];
    const originalError = console.error;
    console.error = (...args) => errors.push(args);
    try {
        uiContractJson.setViolationReporter(null);
        assert.strictEqual(uiContractJson.parse('{bad', COMMAND, 'probe'), null);
        assert.strictEqual(errors.length, 1);
        assert.match(String(errors[0][0]), /\[ui-contract\] Rejected TimelineCommandPayload payload from probe/);
    } finally {
        console.error = originalError;
    }
});

test('fromValue validates an already-decoded object', () => {
    withCapture((violations) => {
        assert.deepEqual(uiContractJson.fromValue(VALID, COMMAND, 'inline'), VALID);
        assert.strictEqual(uiContractJson.fromValue({ kind: 'nope' }, COMMAND, 'inline'), null);
        assert.strictEqual(violations.length, 1);
    });
});
