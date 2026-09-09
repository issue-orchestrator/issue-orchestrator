const test = require('node:test');
const assert = require('node:assert');

const createRecoveryView = require(
    '../../src/issue_orchestrator/static/js/control_center_recovery.js',
);

const REPO_KEY = `repo-${'a'.repeat(64)}`;

function escapeHtml(value) {
    return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
}

function engine(label = 'worker-a', instanceId = 'instance-a') {
    return {
        repo_root: '/repo',
        instance_id: instanceId,
        host: 'host-a',
        label,
        process: {
            host: 'host-a',
            pid: 1234,
            started_at: '2026-09-08T12:00:00Z',
            instance_id: instanceId,
        },
    };
}

function work(kind, issueNumber, branch = 'issue-42') {
    const ownerEngine = engine();
    return {
        kind,
        work: {
            authority: {
                record_id: `record-${issueNumber}`,
                evidence_id: `evidence-${issueNumber}`,
                observation_revision: 3,
                validated_head_sha: 'a'.repeat(40),
                issue_number: issueNumber,
                branch_name: branch,
                repo_slug: 'owner/repo',
                pr_number: null,
                expected_remote_head_sha: null,
                remote_baseline_status: 'unobserved',
            },
            state: 'queued',
            failure: null,
            reason: `Retained <${issueNumber}>`,
            escrow_retained: true,
        },
        ...(kind === 'owned' ? {
            owner: {
                engine: ownerEngine,
                owner_fence: 7,
                stop_availability: 'exact_target_unavailable',
            },
            stop_action: null,
        } : {}),
    };
}

function availablePayload() {
    return {
        repo_key: REPO_KEY,
        status: 'available',
        message: 'Two retained records',
        engine_groups: [{
            engine: engine(),
            presentation: 'missing',
            presentation_message: 'The prior engine is no longer running',
            records: [work('owned', 42, 'feature/<unsafe>')],
        }],
        unowned_records: [work('unowned', 43)],
    };
}

test('loads each registered repository through its opaque key', async () => {
    const calls = [];
    const payload = availablePayload();
    const view = createRecoveryView({
        escapeHtml,
        fetch: async (url) => {
            calls.push(url);
            return { ok: true, json: async () => payload };
        },
    });
    const repos = [{ repo_key: REPO_KEY }];

    await view.load(repos);

    assert.deepEqual(calls, [
        `/api/control-center/repositories/${REPO_KEY}/validated-work`,
    ]);
    assert.equal(repos[0].validated_work, payload);
    assert.equal(repos[0].validated_work_error, null);
});

test('bounds recovery reads during fast repository polling', async () => {
    let currentTime = 1000;
    let requestCount = 0;
    const view = createRecoveryView({
        escapeHtml,
        now: () => currentTime,
        fetch: async () => {
            requestCount += 1;
            return { ok: true, json: async () => availablePayload() };
        },
    });

    await Promise.all([
        view.load([{ repo_key: REPO_KEY }]),
        view.load([{ repo_key: REPO_KEY }]),
    ]);
    currentTime += 29999;
    await view.load([{ repo_key: REPO_KEY }]);
    assert.equal(requestCount, 1);

    currentTime += 1;
    await view.load([{ repo_key: REPO_KEY }]);
    assert.equal(requestCount, 2);
});

test('renders owned and unowned retained work behind native disclosure', () => {
    const view = createRecoveryView({ escapeHtml, fetch: async () => {} });
    const html = view.render({ validated_work: availablePayload() });

    assert.match(html, /^<details class="repo-recovery" data-recovery-repo-key=/);
    assert.match(html, /<summary>/);
    assert.match(html, /Preserved validated work/);
    assert.match(html, /repo-recovery-count">2</);
    assert.match(html, /worker-a · instance-a/);
    assert.match(html, />missing</);
    assert.match(html, /No engine owner/);
    assert.match(html, /Issue #42/);
    assert.match(html, /feature\/&lt;unsafe&gt;/);
    assert.match(html, /Retained &lt;42&gt;/);
    assert.doesNotMatch(html, /stop_action|data-action/);
});

test('does not repeat an engine label that is also its instance id', () => {
    const view = createRecoveryView({ escapeHtml, fetch: async () => {} });
    const payload = availablePayload();
    payload.engine_groups[0].engine.instance_id = 'worker-a';

    const html = view.render({ validated_work: payload });

    assert.match(html, /<h4>worker-a<\/h4>/);
    assert.doesNotMatch(html, /worker-a · worker-a/);
});

test('renders empty, absent, and unreadable stores as distinct textual states', () => {
    const view = createRecoveryView({ escapeHtml, fetch: async () => {} });
    const base = {
        repo_key: REPO_KEY,
        engine_groups: [],
        unowned_records: [],
        message: 'No preserved validated work',
    };

    const empty = view.render({ validated_work: { ...base, status: 'empty' } });
    assert.match(empty, /role="status"/);
    assert.match(empty, /Preserved work:/);
    assert.match(empty, /No preserved validated work/);
    const absent = view.render({
        validated_work: { ...base, status: 'database_absent', message: 'State database not found' },
    });
    assert.match(absent, /Preserved work database absent/);
    assert.match(absent, /State database not found/);
    const unreadable = view.render({
        validated_work: { ...base, status: 'unreadable', message: 'Database is locked' },
    });
    assert.match(unreadable, /role="status"/);
    assert.match(unreadable, /Preserved work status unavailable/);
    assert.match(unreadable, /Database is locked/);
});

test('accepts the public contract empty reason without hiding records', () => {
    const view = createRecoveryView({ escapeHtml, fetch: async () => {} });
    const payload = availablePayload();
    payload.engine_groups[0].records[0].work.reason = '';

    assert.doesNotThrow(() => view.validatePayload(payload, REPO_KEY));
    assert.match(view.render({ validated_work: payload }), /Issue #42/);
});

test('turns malformed and failed responses into a visible per-repo status', async () => {
    const view = createRecoveryView({
        escapeHtml,
        fetch: async () => ({
            ok: true,
            json: async () => ({ ...availablePayload(), repo_key: `repo-${'b'.repeat(64)}` }),
        }),
    });
    const repos = [{ repo_key: REPO_KEY }];

    await view.load(repos);

    assert.equal(repos[0].validated_work, null);
    assert.match(repos[0].validated_work_error, /repository key mismatch/);
    assert.match(view.render(repos[0]), /role="status"/);
    assert.match(view.render(repos[0]), /repository key mismatch/);
});

test('rejects records on unavailable discriminants', () => {
    const view = createRecoveryView({ escapeHtml, fetch: async () => {} });
    const payload = availablePayload();

    assert.throws(
        () => view.validatePayload({ ...payload, status: 'unsupported_schema' }, REPO_KEY),
        /must not contain records/,
    );
});
