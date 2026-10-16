// The Control Center's Tech lead page renderer and its one command (#7763).
const test = require('node:test');
const assert = require('node:assert');
const contractJson = require('../../src/issue_orchestrator/static/js/ui_contract_json.js');
const createView = require('../../src/issue_orchestrator/static/js/control_center_tech_lead.js');

const KEY = 'repo-' + 'a'.repeat(64);
const item = (number, extra = {}) => ({
    kind: 'proposal', number, operation: 'reset_retry', title: `Proposal ${number}`,
    recommendation: 'Reset it', approval_effect: 'Resets and retries', link: `https://github.com/o/a/issues/${number}`,
    waiting_since: '', status: 'awaiting_approval', status_label: 'Awaiting your approval',
    can_approve: true, can_decline: true, details: [], approval_steps: [], operator_steps: [], ...extra,
});
const section = (waiting, extra = {}) => ({
    repository: 'o/a', generated_at: 'now', waiting_count: waiting.length,
    run: { has_run: true, label: 'Health review — Whole board', phase: 'running', phase_label: 'Running', started_at: '2026-10-03T10:00:00Z', ended_at: '', detail: '' },
    waiting, doing: [], parked: [], triaged: [], case_files: [],
    health_review: { enabled: true, interval_minutes: 60, last_at: '', next_due_at: '', label: 'Every 60 min' },
    ...extra,
});
// As the Control Center builds it: a running engine that did not report now
// (unavailable or last_known) makes the total unknown (#8988).
const page = (repos) => ({
    waiting_count: repos.some(r => r.availability === 'unavailable' || r.availability === 'last_known')
        ? null
        : repos.reduce((n, r) => n + (r.availability === 'available' ? r.section.waiting_count : 0), 0),
    unreported_count: repos.filter(r => r.availability !== 'available').length,
    generated_at: 'now', repos,
});
const repo = (sec, extra = {}) => ({ repo_key: KEY, name: 'a', availability: 'available', detail: '', section: sec, ...extra });
const view = (fetch = async () => { throw new Error('no fetch'); }) => createView({ fetch, contractJson });
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

test('an approved proposal stays visible but is not counted as waiting on you', () => {
    const approved = item(9, { status: 'approved', status_label: 'Approved (by @octo); the engine will act on it', can_approve: false });
    const sec = section([approved], { waiting_count: 0 });
    const html = view().renderPage(page([repo(sec)]));
    assert.equal(html.waitingHeading, 'Waiting on you (0)');
    assert.match(html.waiting, /Nothing is waiting on you/);
    assert.match(html.waiting, /Proposal 9/);
});

// A minimal DOM stand-in (tests/js/AGENTS.md: no jsdom): each lane rebuilds
// its focusable controls from the markup it is given, so a refresh replaces
// every control object, as innerHTML does in a browser.
function domHarness() {
    let active = null;
    const focusable = name => ({ name, focus() { active = this; } });
    const lane = name => {
        const node = {
            controls: [],
            lastHtml: '',
            set innerHTML(html) {
                node.lastHtml = html;
                // A disabled native control refuses focus, as in a browser.
                node.controls = [...html.matchAll(/<[^>]*data-focus-key="([^"]*)"[^>]*>/g)]
                    .map(([tag, key]) => {
                        const disabled = /\sdisabled[\s>]/.test(tag);
                        const control = { ...focusable(`${name} ${key}`), dataset: { focusKey: key }, disabled };
                        if (disabled) control.focus = () => {};
                        return control;
                    });
            },
            querySelectorAll: () => node.controls,
            contains: control => node.controls.includes(control),
            control: key => node.controls.find(control => control.dataset.focusKey === key),
        };
        return node;
    };
    const lanes = { waiting: lane('waiting'), doing: lane('doing'), watching: lane('watching') };
    const headings = { waiting: focusable('waiting heading'), doing: focusable('doing heading'), watching: focusable('watching heading') };
    const nodes = {
        '#techLeadWaitingHeading': { ...headings.waiting, textContent: '' },
        '#techLeadDoingHeading': headings.doing, '#techLeadWatchingHeading': headings.watching,
        '#techLeadWaitingList': lanes.waiting, '#techLeadDoingList': lanes.doing, '#techLeadWatchingList': lanes.watching,
        '#techLeadRunStrip': { innerHTML: '' },
    };
    const root = {
        ownerDocument: { get activeElement() { return active; } },
        querySelector: selector => nodes[selector],
        addEventListener: () => {},
    };
    return { root, lanes, nodes, active: () => active };
}

test('a refresh keeps keyboard focus on the same control, or moves it to its lane heading', () => {
    const v = view();
    const dom = domHarness();
    v.bind(dom.root);
    const withDetails = item(8, { details: [{ label: 'PR', value: 'o/a#9' }] });
    const doing = [{ decision_id: 'd1', action_kind: 'kill_hung_session', action_label: 'kill hung session', target_number: 402,
        link: 'https://github.com/o/a/issues/402', outcome: 'applied', outcome_label: 'Applied', at: 'now', reason: '', in_flight: false }];
    const render = (waiting, doingRows) => page([repo(section(waiting, { doing: doingRows }))]);
    v.paint(render([item(7), withDetails], doing));

    for (const key of [`approve:${KEY}:8`, `link:${KEY}:8`, `details:${KEY}:8`]) {
        const before = dom.lanes.waiting.control(key);
        before.focus();
        v.paint(render([item(7), withDetails], doing));
        assert.notEqual(dom.active(), before, key);  // the old control is gone...
        assert.equal(dom.active(), dom.lanes.waiting.control(key), key);  // ...its replacement has focus
    }
    dom.lanes.doing.control(`doing:${KEY}:d1`).focus();
    v.paint(render([item(7), withDetails], doing));
    assert.equal(dom.active(), dom.lanes.doing.control(`doing:${KEY}:d1`));

    dom.lanes.waiting.control(`approve:${KEY}:8`).focus();
    v.paint(render([item(7), { ...withDetails, can_approve: false }], doing));  // approved elsewhere
    assert.equal(dom.active().name, 'waiting heading');
    dom.lanes.doing.control(`doing:${KEY}:d1`).focus();

    v.paint(render([item(7), withDetails], []));  // the doing row aged out
    assert.equal(dom.active().name, 'doing heading');
    dom.lanes.waiting.control(`approve:${KEY}:8`).focus();
    v.paint(render([item(7)], []));  // #8 was decided elsewhere
    assert.equal(dom.active().name, 'waiting heading');
});

test('an unreported repository never reads as an all-clear', () => {
    const p = { ...page([repo(section([])), repo(null, { availability: 'engine_not_running', detail: 'Engine not running', name: 'b' })]), unreported_count: 1 };
    const html = view().renderPage(p);
    assert.doesNotMatch(html.waiting, /Nothing is waiting on you/);
    assert.match(html.waiting, /not every repository reported/);
    assert.equal(view().badgeText(0, 1), '0 waiting on you; 1 repository not reporting');
    assert.equal(view().badgeText(2, 3), '2 waiting on you; 3 repositories not reporting');
});

test('frame messages are read only from the embedded dashboard, in contract shape', () => {
    const v = view();
    const frame = {};
    const open = { type: 'cc-open-tech-lead', repository: 'o/a', number: 700 };
    assert.deepEqual(v.readFrameMessage({ source: frame, data: open }, frame), open);
    assert.equal(v.readFrameMessage({ source: {}, data: open }, frame), null);
    assert.equal(v.readFrameMessage({ source: frame, data: { ...open, number: 'x' } }, frame), null);
    assert.equal(v.readFrameMessage({ source: frame, data: open }, undefined), null);
    const ask = { type: 'cc-tech-lead-waiting-request' };
    assert.deepEqual(v.readFrameMessage({ source: frame, data: ask }, frame), ask);
    // Not a Tech lead message at all: left to the window's other handlers.
    assert.equal(v.readFrameMessage({ source: frame, data: { type: 'dashboard-status', payload: {} } }, frame), undefined);
});

test('no handler branches on a raw Tech lead message type before the contract reads it', () => {
    // #7763 review r12 F3: the discriminator is contract-owned.
    const fs = require('node:fs');
    const path = require('node:path');
    const js = path.join(__dirname, '../../src/issue_orchestrator/static/js');
    for (const file of ['control_center.js', 'dashboard/tech_lead_badge.js']) {
        const source = fs.readFileSync(path.join(js, file), 'utf8');
        assert.doesNotMatch(source, /event\.data\??\.type\s*[!=]==\s*'cc-(open-tech-lead|tech-lead-waiting)/, file);
        assert.match(source, /fromUnionMember\(event\.data, 'TechLeadFrameMessage'|readFrameMessage\(/, file);
    }
});


test('a failed or off-contract refresh after an all-clear shows "unable to check", never the old answer', async () => {
    // #7763 review r17 F2.
    let answer = { payload: page([repo(section([]))]) };
    let badges = [];
    const v = createView({
        fetch: async () => {
            if (answer.throws) throw new Error('network down');
            return response(200, answer.payload);
        },
        contractJson,
        onBadgeChange: () => badges.push(v.badgeState()),
    });
    contractJson.setViolationReporter(() => {});
    const dom = domHarness();
    v.bind(dom.root);

    await v.refresh();
    assert.equal(badges.at(-1).text, 'Nothing waiting on you');

    const doing = [{ decision_id: 'd1', action_kind: 'kill_hung_session', action_label: 'kill hung session', target_number: 402,
        link: 'https://github.com/o/a/issues/402', outcome: 'applied', outcome_label: 'Applied', at: 'now', reason: '', in_flight: false }];
    answer = { payload: page([repo(section([item(8)], { doing }))]) };
    await v.refresh();
    assert.match(dom.lanes.doing.lastHtml, /kill hung session/);
    dom.lanes.doing.control(`doing:${KEY}:d1`).focus();
    answer = { throws: true };
    await v.refresh();
    // r27 F2: every lane and the run strip drop the last good read...
    assert.doesNotMatch(dom.lanes.doing.lastHtml, /kill hung session/);
    assert.match(dom.lanes.doing.lastHtml, /Unable to check/);
    assert.match(dom.lanes.watching.lastHtml, /Unable to check/);
    assert.match(dom.nodes['#techLeadRunStrip'].innerHTML, /Unable to check/);
    assert.equal(dom.active().name, 'doing heading');  // ...and focus goes to the lane heading
    answer = { payload: page([repo(section([item(8)]))]) };
    await v.refresh();
    for (const failure of [{ throws: true }, { payload: { not: 'the contract' } }]) {
        const approve = dom.lanes.waiting.control(`approve:${KEY}:8`);
        if (approve) approve.focus();  // r18 F2: focus on a control the failure removes
        answer = failure;
        await v.refresh();
        assert.equal(v.latest(), null);
        assert.equal(badges.at(-1).text, 'Unable to check what waits on you');
        assert.equal(badges.at(-1).count, null);  // #8988: unknown, never a count of 0
        assert.match(dom.nodes['#techLeadWaitingList'].lastHtml, /Unable to check what waits on you/);
        assert.doesNotMatch(dom.nodes['#techLeadWaitingList'].lastHtml, /Nothing waiting/);
        if (approve) assert.equal(dom.active().name, 'waiting heading');
    }
    contractJson.setViolationReporter(null);
});


test('an older refresh that resolves last never overwrites a newer answer', async () => {
    // #7763 review r19 F2: polling and a command refresh overlap.
    const pending = [];
    const badges = [];
    const v = createView({
        fetch: () => new Promise(resolve => pending.push(resolve)),
        contractJson,
        onBadgeChange: () => badges.push(v.badgeState()),
    });
    const dom = domHarness();
    v.bind(dom.root);
    contractJson.setViolationReporter(() => {});

    const older = v.refresh();
    const newer = v.refresh();
    pending[1](response(200, { not: 'the contract' }));  // the newer read: unavailable
    await newer;
    pending[0](response(200, page([repo(section([]))])));  // the older all-clear lands last
    await older;
    contractJson.setViolationReporter(null);

    assert.deepEqual(badges.map(b => b.text), ['Unable to check what waits on you']);
    assert.equal(v.badgeState().text, 'Unable to check what waits on you');
    assert.doesNotMatch(dom.nodes['#techLeadWaitingList'].lastHtml, /Nothing/);
});


test('a hostile repository name or title never leaves its attribute', () => {
    // #7763 review r29 F2: the production encoder, not a test stub.
    const hostile = 'x" onmouseover="alert(1)';
    const html = view().renderPage(page([repo(section([item(9, { title: hostile })]), { name: hostile })])).waiting;
    assert.doesNotMatch(html, /onmouseover="alert/);
    assert.match(html, /x&quot; onmouseover=&quot;alert\(1\)/);
    for (const tag of html.match(/<button[^>]*>/g)) {
        // Tokenize as a browser would: each name="value" pair consumes its
        // whole quoted value, so text inside a value is never a new attribute.
        const attributes = [...tag.matchAll(/\s([a-z-]+)(?:="[^"]*")?/g)].map(([, name]) => name);
        assert.ok(!attributes.includes('onmouseover'), tag);
    }
});

test('a decision shows the steps approval runs and the operator checklist, each list labelled (#8691)', () => {
    const decision = item(530, {
        operation: 'propose_decision',
        approval_steps: ["Rewrites PR #525's `Closes #327` to `Refs #327`.", 'Sends PR #525 back for rework.'],
        operator_steps: ['Raise the CI ceiling in .github/workflows/ci.yml <script>'],
    });
    const html = view().renderPage(page([repo(section([decision]))])).waiting;
    const approval = html.match(/<p id="([^"]+)"><strong>Approving also:<\/strong><\/p><ol class="tl-steps" aria-labelledby="([^"]+)">(.*?)<\/ol>/);
    assert.ok(approval, html);
    assert.equal(approval[1], approval[2]);
    assert.equal((approval[3].match(/<li>/g) || []).length, 2);
    const checklist = html.match(/<p id="([^"]+)"><strong>You do by hand \(io cannot\):<\/strong><\/p><ul class="tl-checklist" aria-labelledby="([^"]+)">(.*?)<\/ul>/);
    assert.ok(checklist, html);
    assert.equal(checklist[1], checklist[2]);
    assert.match(checklist[3], /aria-hidden="true">&#9744;<\/span> Raise the CI ceiling/);
    assert.doesNotMatch(html, /<script>/);
});

test('an item with no steps renders neither list', () => {
    const html = view().renderPage(page([repo(section([item(7)]))])).waiting;
    assert.doesNotMatch(html, /Approving also|by hand/);
});

test('the integration delivery PR is a labelled card linking the PR, with what to do and no buttons (#8144)', () => {
    const delivery = item(600, {
        kind: 'delivery_pr', operation: 'deliver', title: 'Deliver integration to main',
        recommendation: 'Delivers 3 pull request(s) merged into integration to main.',
        approval_effect: 'Merge this PR on GitHub with a merge commit (not squash or rebase); io then fast-forwards integration to main. Never delete integration.',
        link: 'https://github.com/o/a/pull/600', status: 'delivery_ready', status_label: 'Ready for you to merge',
        can_approve: false, can_decline: false,
        details: [{ label: 'Pull requests', value: '#476, #479, #511' }],
    });
    const html = view().renderPage(page([repo(section([delivery]))]));
    assert.equal(html.waitingHeading, 'Waiting on you (1)');
    const card = html.waiting;
    assert.match(card, /<article class="tl-card tl-kind-delivery_pr"[^>]*aria-labelledby="tl-card-[^"]+-600"/);
    assert.match(card, /<span class="tl-kind">Delivery PR<\/span>/);
    assert.match(card, /<a href="https:\/\/github.com\/o\/a\/pull\/600"[^>]*>#600 Deliver integration to main<\/a>/);
    assert.match(card, /<strong>What to do:<\/strong> Merge this PR on GitHub with a merge commit/);
    assert.match(card, /<strong>Status:<\/strong> Ready for you to merge/);
    assert.match(card, /#476, #479, #511/);
    assert.doesNotMatch(card, /<button/);
});


const OTHER_KEY = 'repo-' + 'b'.repeat(64);
const AS_OF = '2026-10-08T12:00:00+00:00';
const lastKnown = (sec, extra = {}) => repo({ ...sec, generated_at: AS_OF }, {
    repo_key: OTHER_KEY, name: 'porchpin', availability: 'last_known',
    detail: 'The engine is running but did not answer.', ...extra,
});

test('a running repository that did not report makes the count unknown, never 0 (#8988)', () => {
    const p = page([repo(section([])), repo(null, { repo_key: OTHER_KEY, name: 'porchpin', availability: 'unavailable', detail: 'The engine is running but did not answer.' })]);
    assert.equal(p.waiting_count, null);
    const html = view().renderPage(p);
    assert.equal(html.waitingHeading, 'Waiting on you (unknown)');
    assert.doesNotMatch(html.waiting, /Nothing is waiting on you|Nothing reported as waiting/);
    assert.match(html.waiting, /what waits on you is not fully known/);
    assert.match(html.waiting, /<strong>porchpin<\/strong>: The engine is running but did not answer\./);
    assert.equal(view().badgeText(null, 1), 'What waits on you is unknown; 1 repository not reporting');
    assert.equal(view().badgeText(null, 2), 'What waits on you is unknown; 2 repositories not reporting');
});

test('a last-known section shows wherever it appears with its as-of time; a current one does not (#8988)', () => {
    const doing = [{ decision_id: 'd1', action_kind: 'kill_hung_session', action_label: 'kill hung session', target_number: 402,
        link: 'https://github.com/o/a/issues/402', outcome: 'applied', outcome_label: 'Applied', at: 'now', reason: '', in_flight: false }];
    const watched = { triaged: [{ issue_number: 403, link: 'https://github.com/o/a/issues/403', triage_label: 'Explained', reason: 'r' }] };
    const p = page([repo(section([item(7)])), lastKnown(section([item(8)], { doing, ...watched }))]);
    const html = view().renderPage(p);
    const stamp = `(last reported <time datetime="${AS_OF}">${AS_OF}</time>)`;

    assert.equal(html.waitingHeading, 'Waiting on you (unknown)');
    // The not-reporting list says why, and as of when.
    assert.ok(html.waiting.includes(`<li><strong>porchpin</strong>: The engine is running but did not answer. <span class="tl-as-of">${stamp}</span></li>`), html.waiting);
    // Its card is still listed, marked, with its decision buttons named as before...
    const cards = html.waiting.split('<li><article').slice(1);
    const stale = cards.find(card => card.includes('#8 Proposal 8'));
    assert.ok(stale.includes(`<span class="tl-repo">porchpin</span> <span class="tl-as-of">${stamp}</span>`), stale);
    assert.match(stale, /aria-label="Approve proposal #8 in porchpin"/);
    // ...while the current repository's card carries no as-of.
    const current = cards.find(card => card.includes('#7 Proposal 7'));
    assert.doesNotMatch(current, /tl-as-of/);
    assert.ok(html.doing.includes(`porchpin <span class="tl-as-of">${stamp}</span>`), html.doing);
    assert.ok(html.watching.includes(`<h3>porchpin <span class="tl-as-of">${stamp}</span></h3>`), html.watching);
    assert.ok(html.runStrip.includes(`porchpin <span class="tl-as-of">${stamp}</span>: Running`), html.runStrip);
    assert.ok(html.runStrip.includes('a: Running'), html.runStrip);
});

test('load accepts an unknown count and a last-known repository in contract shape (#8988)', async () => {
    const p = page([lastKnown(section([item(8)]))]);
    const loaded = await view(async () => response(200, p)).load();
    assert.equal(loaded.waiting_count, null);
    assert.equal(loaded.repos[0].availability, 'last_known');
    contractJson.setViolationReporter(() => {});
    try {
        // The total is a non-negative integer or null: nothing else is a count.
        assert.equal(await view(async () => response(200, { ...p, waiting_count: 'unknown' })).load(), null);
        assert.equal(await view(async () => response(200, { ...p, waiting_count: -1 })).load(), null);
    } finally {
        contractJson.setViolationReporter(null);
    }
});

test('the badge state carries an unknown count as null, worded, after a refresh (#8988)', async () => {
    const badges = [];
    const v = createView({
        fetch: async () => response(200, page([lastKnown(section([item(8)]))])),
        contractJson,
        onBadgeChange: () => badges.push(v.badgeState()),
    });
    v.bind(domHarness().root);

    await v.refresh();

    assert.deepEqual(badges, [{ count: null, text: 'What waits on you is unknown; 1 repository not reporting' }]);
});

// r1 F2: availability decides whether a row carries a section, in the
// generated contract, so a row the renderer could not paint never reaches it.
const DISAGREEING_ROWS = [
    ['last_known without a section', lastKnown(section([]), { section: null })],
    ['available without a section', repo(null)],
    ['unavailable with a section', repo(section([item(8)]), { availability: 'unavailable', detail: 'The engine is running but did not answer.' })],
    ['engine_not_running with a section', repo(section([item(8)]), { availability: 'engine_not_running', detail: 'Engine not running.' })],
    ['last_known without a reason', lastKnown(section([]), { detail: '' })],
    ['unavailable without a reason', repo(null, { availability: 'unavailable', detail: '' })],
];

test('load refuses a row whose availability and section disagree (r1 F2)', async () => {
    const violations = [];
    contractJson.setViolationReporter(violation => violations.push(violation));
    try {
        for (const [name, row] of DISAGREEING_ROWS) {
            const p = { ...page([repo(section([]))]), repos: [row] };
            assert.equal(await view(async () => response(200, p)).load(), null, name);
        }
    } finally {
        contractJson.setViolationReporter(null);
    }
    assert.equal(violations.length, DISAGREEING_ROWS.length);
});

test('a refused row is the unavailable state, with its badge, never a paint (r1 F2)', async () => {
    const badges = [];
    const v = createView({
        fetch: async () => response(200, { ...page([]), waiting_count: null, unreported_count: 1, repos: [lastKnown(section([]), { section: null })] }),
        contractJson,
        onBadgeChange: () => badges.push(v.badgeState()),
    });
    const dom = domHarness();
    v.bind(dom.root);
    contractJson.setViolationReporter(() => {});
    try {
        await v.refresh();
    } finally {
        contractJson.setViolationReporter(null);
    }

    assert.equal(v.latest(), null);
    assert.deepEqual(badges, [{ count: null, text: 'Unable to check what waits on you' }]);
    assert.match(dom.nodes['#techLeadWaitingList'].lastHtml, /Unable to check what waits on you/);
    assert.equal(dom.nodes['#techLeadWaitingHeading'].textContent, 'Waiting on you');
});
