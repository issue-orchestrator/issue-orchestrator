// The repo dashboard's "N waiting on you" badge and custody link (#7763).
const test = require('node:test');
const assert = require('node:assert');
const { initTechLeadBadge } = require('../../src/issue_orchestrator/static/js/dashboard/tech_lead_badge.js');

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
    assert.equal(initTechLeadBadge(h.win, h.doc), false);
    assert.equal(h.button.hidden, true);
    assert.deepEqual(h.posted, []);
});

test('embedded: shown, asks the Control Center for the count, and renders it as text', () => {
    const h = harness('?embedded=1');
    assert.equal(initTechLeadBadge(h.win, h.doc), true);
    assert.equal(h.button.hidden, false);
    assert.deepEqual(h.posted, [{ type: 'cc-tech-lead-waiting-request' }]);
    h.listeners.message({ data: { type: 'cc-tech-lead-waiting', count: 4, text: '4 waiting on you' } });
    assert.equal(h.button.textContent, '4 waiting on you');
    assert.equal(h.button.dataset.waiting, '4');
    h.listeners.message({ data: { type: 'cc-tech-lead-waiting', count: -1, text: 'x' } });
    h.listeners.message({ data: { type: 'cc-tech-lead-waiting', count: 2 } });  // no CC wording
    assert.equal(h.button.textContent, '4 waiting on you');
    // An incomplete count arrives worded by the Control Center, never as an all-clear.
    h.listeners.message({ data: { type: 'cc-tech-lead-waiting', count: 0, text: '0 waiting on you; 1 repository not reporting' } });
    assert.equal(h.button.textContent, '0 waiting on you; 1 repository not reporting');
});

test('the badge and the custody drawer link open the Tech lead page, never approve', () => {
    const h = harness('?embedded=1');
    initTechLeadBadge(h.win, h.doc);
    h.button.onclick();
    const link = { dataset: { techLeadProposal: '700' } };
    h.docListeners.click({ target: { closest: selector => (selector === '[data-tech-lead-proposal]' ? link : null) } });
    assert.deepEqual(h.posted.slice(1), [
        { type: 'cc-open-tech-lead' },
        { type: 'cc-open-tech-lead', repository: 'o/a', number: 700 },
    ]);
});
