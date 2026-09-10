const test = require('node:test');
const assert = require('node:assert');

const createStopView = require(
    '../../src/issue_orchestrator/static/js/control_center_recovery_stop.js',
);

const REPO_KEY = `repo-${'a'.repeat(64)}`;

function escapeHtml(value) {
    return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
}

function engine(instanceId = 'worker-a') {
    return {
        repo_root: '/repo',
        instance_id: instanceId,
        host: 'host-a',
        label: instanceId || 'default',
        process: {
            host: 'host-a',
            pid: 1234,
            started_at: '12345',
            instance_id: instanceId,
        },
    };
}

function row(availability = 'available') {
    const ownerEngine = engine();
    return {
        kind: 'owned',
        work: {
            authority: { record_id: 'record-a', issue_number: 42 },
        },
        owner: {
            engine: ownerEngine,
            owner_fence: 7,
            stop_availability: availability,
        },
        stop_action: availability === 'available' ? {
            record_id: 'record-a',
            expected_engine: ownerEngine,
            expected_owner_fence: 7,
            graceful_timeout_seconds: 120,
            force_on_timeout: true,
        } : null,
    };
}

test('renders a guarded Stop engine command only for an available exact owner', () => {
    const view = createStopView({ escapeHtml, fetch: async () => {} });

    const html = view.renderAction(row(), REPO_KEY);

    assert.match(html, />\s*Stop engine\s*</);
    assert.match(html, /aria-label="Stop engine worker-a owning retained work for issue 42"/);
    assert.match(html, new RegExp(`data-recovery-stop-repo-key="${REPO_KEY}"`));
    assert.match(html, /data-recovery-stop-instance-key="worker-a"/);
    assert.match(html, /&quot;record_id&quot;:&quot;record-a&quot;/);
    assert.match(html, /&quot;expected_owner_fence&quot;:7/);
});

test('keeps remote ownership informational and links unavailable local targets to engine controls', () => {
    const view = createStopView({ escapeHtml, fetch: async () => {} });

    const remote = view.renderAction(row('remote_host'), REPO_KEY);
    const unsupported = view.renderAction(row('exact_target_unavailable'), REPO_KEY);

    assert.match(remote, /runs on host-a/);
    assert.match(unsupported, /Exact engine stop is unavailable here/);
    assert.doesNotMatch(remote, /<button|data-recovery-stop-action/);
    assert.match(unsupported, /<button[^>]+data-recovery-engine-controls=/);
    assert.match(unsupported, /Go to engine controls/);
    assert.doesNotMatch(unsupported, /data-recovery-stop-action|disabled/);
});

test('refuses a rendered action whose engine or owner fence differs from its owner', () => {
    const view = createStopView({ escapeHtml, fetch: async () => {} });
    const changedEngine = row();
    changedEngine.stop_action.expected_engine = engine('worker-b');
    const changedFence = row();
    changedFence.stop_action.expected_owner_fence = 8;

    assert.throws(() => view.validateRow(changedEngine, changedEngine.owner.engine), /engine changed/);
    assert.throws(() => view.validateRow(changedFence, changedFence.owner.engine), /fence changed/);
});

test('posts the exact rendered command and operator reason through browser auth fetch', async () => {
    const calls = [];
    const owner = row().owner;
    const view = createStopView({
        escapeHtml,
        fetch: async (url, options) => {
            calls.push({ url, options });
            return {
                ok: true,
                json: async () => ({
                    status: 'stopped',
                    observed_owner: owner,
                    message: 'Expected engine stopped gracefully',
                }),
            };
        },
    });
    const action = row().stop_action;

    const outcome = await view.requestStop(
        { repoKey: REPO_KEY, instanceKey: 'worker-a', action },
        'Engine is wedged',
    );

    assert.equal(outcome.status, 'stopped');
    assert.equal(calls[0].url,
        `/api/control-center/repositories/${REPO_KEY}/engines/worker-a/stop-validated-work-owner`);
    assert.deepEqual(JSON.parse(calls[0].options.body), {
        record_id: action.record_id,
        expected_engine: action.expected_engine,
        expected_owner_fence: action.expected_owner_fence,
        reason: 'Engine is wedged',
    });
    assert.doesNotMatch(calls[0].options.body, /graceful_timeout_seconds|force_on_timeout/);
    assert.deepEqual(calls[0].options.headers, { 'Content-Type': 'application/json' });
});

test('routes keyboard activation of unavailable exact targets to independent controls only', () => {
    let navigated = null;
    let stopRequests = 0;
    const listeners = {};
    const view = createStopView({
        document: {
            getElementById: () => ({ addEventListener: () => {} }),
        },
        escapeHtml,
        fetch: async () => { stopRequests += 1; },
        navigateToEngineControls: repoKey => { navigated = repoKey; },
    });
    const navigation = {
        dataset: { recoveryEngineControls: REPO_KEY },
        closest: selector => selector === '[data-recovery-engine-controls]' ? navigation : null,
    };
    const container = {
        addEventListener: (event, handler) => { listeners[event] = handler; },
    };

    view.bind(container);
    listeners.click({ target: navigation });

    assert.equal(navigated, REPO_KEY);
    assert.equal(stopRequests, 0);
});

test('rejects malformed response ownership and HTTP-status disagreement', async () => {
    const missingOwner = createStopView({
        escapeHtml,
        fetch: async () => ({
            ok: true,
            json: async () => ({ status: 'stopped', observed_owner: null, message: 'stopped' }),
        }),
    });
    const badStatus = createStopView({
        escapeHtml,
        fetch: async () => ({
            ok: false,
            json: async () => ({ status: 'stopped', observed_owner: row().owner, message: 'stopped' }),
        }),
    });
    const command = {
        repoKey: REPO_KEY,
        instanceKey: 'worker-a',
        action: row().stop_action,
    };

    await assert.rejects(missingOwner.requestStop(command, 'reason'), /owner is missing/);
    await assert.rejects(badStatus.requestStop(command, 'reason'), /HTTP status disagrees/);
});
