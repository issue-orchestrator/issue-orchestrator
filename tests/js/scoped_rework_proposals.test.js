const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const contract = require('../../src/issue_orchestrator/static/js/ui_action_contract.js');
function moduleApi() {
    const context = {window: {uiActionContract: contract}, document: {getElementById: () => null}};
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/issue_orchestrator/static/js/scoped_rework_proposals.js'), 'utf8'), context);
    return context.window.scopedReworkProposals;
}
const proposal = {proposal_issue_number: 501, pr_number: 94, issue_number: 5, repository: 'porchpin/porchpin', expected_head: 'a'.repeat(40), evidence_identity: 'actor-reads', report: '<script>bad</script>', feedback: 'Actor-scope reads', mutations: 'Preserve branch', status: 'awaiting_approval', detail: 'Awaiting approval', can_approve: true, can_decline: true};
test('producer payload renders named native commands and escaped evidence', () => {
    const html = moduleApi().renderProposal(proposal);
    assert.match(html, /aria-label="Approve rework for PR 94"/);
    assert.match(html, /aria-label="Decline rework for PR 94"/);
    assert.match(html, /<details><summary>Review report<\/summary>/);
    assert.match(html, /&lt;script&gt;bad&lt;\/script&gt;/);
    assert.match(html, /data-rework-command=".*proposal_issue_number.*501/);
    assert.doesNotMatch(html, / disabled/);
});
test('stale state disables approval and preserves decline', () => {
    const html = moduleApi().renderProposal({...proposal, status: 'stale', can_approve: false});
    assert.match(html, /disabled aria-label="Approve rework/);
    assert.doesNotMatch(html, /disabled aria-label="Decline/);
    assert.match(html, /<strong>stale<\/strong>/);
});
test('approve and decline send the shared typed command', async () => {
    for (const decision of ['approve', 'decline']) {
        const command = {proposal_issue_number: 501, decision};
        let observed;
        await moduleApi().dispatch(command, async (url, options) => {
            observed = {url, options}; return {ok: true, json: async () => ({outcome: 'approved'})};
        });
        assert.equal(observed.url, '/api/tech-lead/rework-proposals');
        assert.deepEqual(JSON.parse(observed.options.body), command);
    }
});
test('failed command surfaces failure without reporting success', async () => {
    await assert.rejects(moduleApi().dispatch({proposal_issue_number: 501, decision: 'approve'}, async () => ({ok: false, json: async () => ({detail: 'stale'})})), /stale/);
});
test('retained and unavailable work expose textual ownership without approval controls', () => {
    for (const [status, detail] of [
        ['queued', 'Provider deferred this work; the exact durable request is retained for retry'],
        ['unavailable', 'No active run or applicable durable deferred claim owns this instruction'],
    ]) {
        const html = moduleApi().renderProposal({...proposal, status, detail, can_approve: false, can_decline: false});
        assert.ok(html.includes(`<strong>${status}</strong>`));
        assert.ok(html.includes(detail));
        assert.match(html, /disabled aria-label="Approve rework/);
        assert.match(html, /disabled aria-label="Decline rework/);
        assert.match(html, /<details><summary>Review report<\/summary>/);
    }
});
