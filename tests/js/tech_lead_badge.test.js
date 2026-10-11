// The repo dashboard's "N waiting on you" badge and custody link (#7763).
const test = require('node:test');
const assert = require('node:assert');
const { initTechLeadBadge } = require('../../src/issue_orchestrator/static/js/dashboard/tech_lead_badge.js');
const contractJson = require('../../src/issue_orchestrator/static/js/ui_contract_json.js');

function harness(search) {
    const listeners = {};
    const docListeners = {};
    const posted = [];
    const button = {
        hidden: true, textContent: 'Nothing waiting on you', dataset: {},
        addEventListener: (type, fn) => { button[`on${type}`] = fn; },
    };
    const win = {
        location: { search },
        parent: { postMessage: message => posted.push(message) },
        dashboardData: { repo: 'o/a' },
        addEventListener: (type, fn) => { listeners[type] = fn; },
    };
    const doc = {
        getElementById: id => (id === 'dashboardTechLeadBadge' ? button : null),
        addEventListener: (type, fn) => { docListeners[type] = fn; },
    };
    return { win, doc, button, posted, listeners, docListeners };
}

test('standalone dashboards keep the badge hidden and post nothing', () => {
    const h = harness('');
    assert.equal(initTechLeadBadge(h.win, h.doc, contractJson), false);
    assert.equal(h.button.hidden, true);
    assert.deepEqual(h.posted, []);
});

test('embedded: shown, asks the Control Center for the count, and renders it as text', () => {
    const h = harness('?embedded=1');
    assert.equal(initTechLeadBadge(h.win, h.doc, contractJson), true);
    assert.equal(h.button.hidden, false);
    assert.deepEqual(h.posted, [{ type: 'cc-tech-lead-waiting-request' }]);
    const fromCc = data => ({ source: h.win.parent, data });
    h.listeners.message(fromCc({ type: 'cc-tech-lead-waiting', count: 4, text: '4 waiting on you' }));
    assert.equal(h.button.textContent, '4 waiting on you');
    assert.equal(h.button.dataset.waiting, '4');
    // Off-contract: refused, the badge keeps its last good text.
    h.listeners.message(fromCc({ type: 'cc-tech-lead-waiting', count: -1, text: 'x' }));
    h.listeners.message(fromCc({ type: 'cc-tech-lead-waiting', count: 2 }));
    h.listeners.message(fromCc({ type: 'cc-tech-lead-waiting', count: 2, text: 'y', extra: true }));
    // Another Tech lead message, or none of them: never read as the count.
    h.listeners.message(fromCc({ type: 'cc-tech-lead-waiting-request' }));
    h.listeners.message(fromCc({ type: 'something-else', text: 'z' }));
    assert.equal(h.button.textContent, '4 waiting on you');
    // Not from the embedding Control Center: a forged all-clear is ignored.
    h.listeners.message({ source: {}, data: { type: 'cc-tech-lead-waiting', count: 0, text: 'Nothing waiting on you' } });
    assert.equal(h.button.textContent, '4 waiting on you');
    // An incomplete count arrives worded by the Control Center, never as an all-clear.
    h.listeners.message(fromCc({ type: 'cc-tech-lead-waiting', count: 0, text: '0 waiting on you; 1 repository not reporting' }));
    assert.equal(h.button.textContent, '0 waiting on you; 1 repository not reporting');
});

test('the badge and the custody drawer link open the Tech lead page, never approve', () => {
    const h = harness('?embedded=1');
    initTechLeadBadge(h.win, h.doc, contractJson);
    h.button.onclick();
    const link = { dataset: { techLeadProposal: '700' } };
    h.docListeners.click({ target: { closest: selector => (selector === '[data-tech-lead-proposal]' ? link : null) } });
    assert.deepEqual(h.posted.slice(1), [
        { type: 'cc-open-tech-lead' },
        { type: 'cc-open-tech-lead', repository: 'o/a', number: 700 },
    ]);
});
