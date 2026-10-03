// "N waiting on you" on the repo dashboard (#7763).
//
// The Control Center owns the cross-repo Tech lead page and its count; inside
// the CC iframe the dashboard hides the CC's own header, so this badge shows
// the same count and opens the page. It also routes the blocked-item custody
// drawer's "Open in the Tech lead page" button there — the drawer never
// approves anything itself. Standalone (no CC), both stay out of the way.

function initTechLeadBadge(win, doc) {
    const button = doc.getElementById('dashboardTechLeadBadge');
    if (!button) return false;
    if (new URLSearchParams(win.location.search).get('embedded') !== '1') return false;
    const post = message => win.parent.postMessage(message, '*');
    button.hidden = false;
    win.addEventListener('message', event => {
        const data = event.data;
        if (!data || data.type !== 'cc-tech-lead-waiting' || !Number.isInteger(data.count) || data.count < 0) return;
        if (typeof data.text !== 'string' || !data.text) return;
        // The CC's own wording: it knows whether every repository reported.
        button.textContent = data.text;
        button.dataset.waiting = String(data.count);
    });
    button.addEventListener('click', () => post({ type: 'cc-open-tech-lead' }));
    doc.addEventListener('click', event => {
        const link = event.target.closest && event.target.closest('[data-tech-lead-proposal]');
        if (!link) return;
        post({
            type: 'cc-open-tech-lead',
            repository: (win.dashboardData && win.dashboardData.repo) || null,
            number: Number(link.dataset.techLeadProposal),
        });
    });
    post({ type: 'cc-tech-lead-waiting-request' });
    return true;
}

if (typeof module === 'object' && module.exports) {
    module.exports = { initTechLeadBadge };
} else if (typeof window !== 'undefined' && typeof document !== 'undefined') {
    initTechLeadBadge(window, document);
}
