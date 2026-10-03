// The Control Center's Tech lead page renderer and its one command (#7763).
const test = require('node:test');
const assert = require('node:assert');
const contractJson = require('../../src/issue_orchestrator/static/js/ui_contract_json.js');
const createView = require('../../src/issue_orchestrator/static/js/control_center_tech_lead.js');

const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const KEY = 'repo-' + 'a'.repeat(64);
const item = (number, extra = {}) => ({
    kind: 'proposal', number, operation: 'reset_retry', title: `Proposal ${number}`,
    recommendation: 'Reset it', approval_effect: 'Resets and retries', link: `https://github.com/o/a/issues/${number}`,
    waiting_since: '', status: 'awaiting_approval', status_label: 'Awaiting your approval',
    can_approve: true, can_decline: true, details: [], ...extra,
});
const section = (waiting, extra = {}) => ({
    repository: 'o/a', generated_at: 'now', waiting_count: waiting.length,
    run: { has_run: true, label: 'Health review — Whole board', phase: 'running', phase_label: 'Running', started_at: '2026-10-03T10:00:00Z', ended_at: '', detail: '' },
    waiting, doing: [], parked: [], triaged: [], case_files: [],
    health_review: { enabled: true, interval_minutes: 60, last_at: '', next_due_at: '', label: 'Every 60 min' },
    ...extra,
});
const page = (repos) => ({
    waiting_count: repos.reduce((n, r) => n + (r.section ? r.section.waiting_count : 0), 0),
    generated_at: 'now', repos,
});
const repo = (sec, extra = {}) => ({ repo_key: KEY, name: 'a', availability: 'available', detail: '', section: sec, ...extra });
const view = (fetch = async () => { throw new Error('no fetch'); }) => createView({ fetch, contractJson, escapeHtml });
const response = (status, body) => ({ ok: status < 400, status, url: 'test', text: async () => JSON.stringify(body) });

test('badge text is a label, never a bare number', () => {
    assert.equal(view().badgeText(0), 'Nothing waiting on you');
    assert.equal(view().badgeText(3), '3 waiting on you');
});

test('empty state is a quiet, explicit message, not a hidden panel', () => {
    const html = view().renderPage(page([repo(section([]))]));
    assert.match(html.waiting, /Nothing is waiting on you/);
    assert.equal(html.waitingHeading, 'Waiting on you (0)');
    assert.match(html.doing, /Nothing in the last 24 hours/);
});

test('waiting cards are oldest first across repos, with approve/decline only on proposals', () => {
    const merge = item(5, { kind: 'merge_ready_pr', status: 'merge_held', status_label: 'Merge held', can_approve: false, can_decline: false, waiting_since: '2026-09-01T00:00:00Z' });
    const proposal = item(7, { waiting_since: '2026-10-01T00:00:00Z', title: '<script>x</script>' });
    const html = view().renderPage(page([repo(section([proposal, merge]))])).waiting;
    assert.ok(html.indexOf('#5') < html.indexOf('#7'), 'oldest first');
    assert.match(html, /aria-label="Approve proposal #7 in a"/);
    assert.match(html, /aria-label="Decline proposal #7 in a"/);
    assert.doesNotMatch(html, /proposal #5 in a/);
    assert.match(html, /&lt;script&gt;x&lt;\/script&gt;/);
    assert.match(html, /<strong>Status:<\/strong> Merge held/);
    assert.match(html, /<span class="tl-kind">Merge-ready PR<\/span>/);
});

test('a proposal already decided disables its buttons, keeping their names', () => {
    const html = view().renderPage(page([repo(section([item(8, { can_approve: false, can_decline: false, status: 'executing', status_label: 'Approved; rework queued' })]))])).waiting;
    assert.match(html, /disabled aria-label="Approve proposal #8 in a"/);
    assert.match(html, /disabled aria-label="Decline proposal #8 in a"/);
});

test('engines not running are named, not dropped', () => {
    const html = view().renderPage(page([repo(null, { availability: 'engine_not_running', detail: 'Engine not running: start it.', name: 'b' })])).waiting;
    assert.match(html, /<strong>b<\/strong>: Engine not running/);
});

test('load reads the page through the generated contract and fails closed', async () => {
    const good = page([repo(section([item(9)]))]);
    assert.equal((await view(async () => response(200, good)).load()).waiting_count, 1);
    contractJson.setViolationReporter(() => {});
    try {
        const bad = { ...good, repos: [{ ...good.repos[0], availability: 'maybe' }] };
        assert.equal(await view(async () => response(200, bad)).load(), null);
    } finally {
        contractJson.setViolationReporter(null);
    }
});

test('approve and decline post the one typed command to the repository endpoint', async () => {
    for (const decision of ['approve', 'decline']) {
        let seen;
        const outcome = await view(async (url, options) => {
            seen = { url, options };
            return response(200, { proposal_issue_number: 9, outcome: decision === 'approve' ? 'approved' : 'declined', detail: 'ok' });
        }).dispatch(KEY, 9, decision);
        assert.equal(seen.url, `/api/control-center/repositories/${KEY}/tech-lead/proposals`);
        assert.equal(seen.options.method, 'POST');
        assert.deepEqual(JSON.parse(seen.options.body), { proposal_issue_number: 9, decision });
        assert.equal(outcome.proposal_issue_number, 9);
    }
});

test('a refused command surfaces its detail instead of reporting success', async () => {
    await assert.rejects(
        view(async () => response(409, { proposal_issue_number: 9, outcome: 'unavailable', detail: 'Proposal is closed or missing' })).dispatch(KEY, 9, 'approve'),
        /Proposal is closed or missing/,
    );
});

test('the run strip names the latest run per repository', () => {
    assert.match(view().renderPage(page([repo(section([]))])).runStrip, /a: Running — Health review — Whole board/);
});
