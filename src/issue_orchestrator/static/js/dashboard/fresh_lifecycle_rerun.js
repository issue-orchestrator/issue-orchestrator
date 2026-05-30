let freshLifecycleRerunPreviewedIssueIds = [];
let freshLifecycleRerunEligibleIssueIds = [];

function parseFreshLifecycleRerunIssueInput(value) {
    const matches = String(value || '').match(/\d+/g) || [];
    return uiActionContract.normalizeIssueNumbers(matches);
}

function openFreshLifecycleRerunDialog() {
    const menu = document.getElementById('settingsMenu');
    if (menu) menu.classList.remove('visible');
    freshLifecycleRerunPreviewedIssueIds = [];
    freshLifecycleRerunEligibleIssueIds = [];
    openModal('Fresh Lifecycle Rerun From Scratch', `
        <form id="freshLifecycleRerunForm" class="fresh-lifecycle-rerun-form">
            <label class="prefs-row fresh-lifecycle-rerun-field" for="freshLifecycleRerunIssues">
                Issue numbers
                <textarea id="freshLifecycleRerunIssues" class="fresh-lifecycle-rerun-input" rows="6" aria-describedby="freshLifecycleRerunHelp" placeholder="6376, 6377"></textarea>
            </label>
            <div class="fresh-lifecycle-rerun-note" id="freshLifecycleRerunHelp">
                Enter any issue numbers to rerun. Closed eligible issues are reopened before the rerun starts. Filtered-out and missing-agent issues are skipped without changes.
            </div>
            <div class="fresh-lifecycle-rerun-warning">
                This uses the full reset-from-scratch boundary and a fresh lifecycle rerun: delete local worktrees, delete remote branches, remove orchestrator labels, supersede open orchestrator PRs, force new branches from base, and rerun coding, validation, and review under current conditions.
            </div>
            <div class="fresh-lifecycle-rerun-actions">
                <button type="button" class="issue-action-btn" onclick="closeModal()">Cancel</button>
                <button type="submit" class="issue-action-btn active" id="freshLifecycleRerunPreviewBtn">Preview</button>
            </div>
            <div id="freshLifecycleRerunResults" class="fresh-lifecycle-rerun-results" role="status" aria-live="polite"></div>
        </form>
    `);
    const form = document.getElementById('freshLifecycleRerunForm');
    const input = document.getElementById('freshLifecycleRerunIssues');
    if (form) {
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            previewFreshLifecycleRerun();
        });
    }
    if (input) {
        input.addEventListener('input', () => {
            freshLifecycleRerunPreviewedIssueIds = [];
            freshLifecycleRerunEligibleIssueIds = [];
            const results = document.getElementById('freshLifecycleRerunResults');
            if (results) results.replaceChildren();
        });
        setTimeout(() => input.focus(), 0);
    }
}

function setFreshLifecycleRerunBusy(busy) {
    const previewBtn = document.getElementById('freshLifecycleRerunPreviewBtn');
    const confirmBtn = document.getElementById('freshLifecycleRerunConfirmBtn');
    if (previewBtn) previewBtn.disabled = busy;
    if (confirmBtn) confirmBtn.disabled = busy || freshLifecycleRerunEligibleIssueIds.length === 0;
}

function renderFreshLifecycleRerunMessage(message, type = 'info') {
    const results = document.getElementById('freshLifecycleRerunResults');
    if (!results) return;
    results.innerHTML = `<div class="fresh-lifecycle-rerun-summary ${escapeAttr(type)}">${escapeHtml(message)}</div>`;
}

function freshLifecycleRerunDecisionLabel(decision) {
    if (decision && decision.actionText) return String(decision.actionText);
    if (!decision || !decision.eligible) return 'Skipped';
    if (decision.will_reopen) return 'Will reopen and reset';
    return 'Will reset';
}

function renderFreshLifecycleRerunDecision(decision) {
    const issue = Number(decision.issue);
    const title = decision.title ? ` ${decision.title}` : '';
    const action = freshLifecycleRerunDecisionLabel(decision);
    const statusClass = action === 'Failed' ? 'failed' : (decision.eligible ? 'eligible' : 'skipped');
    const stateText = decision.state ? ` (${decision.state})` : '';
    return `
        <li class="fresh-lifecycle-rerun-row ${statusClass}">
            <span class="fresh-lifecycle-rerun-issue">#${issue}${escapeHtml(title)}${escapeHtml(stateText)}</span>
            <span class="fresh-lifecycle-rerun-action">${escapeHtml(action)}</span>
            <span class="fresh-lifecycle-rerun-reason">${escapeHtml(decision.reason || '')}</span>
        </li>`;
}

function renderFreshLifecycleRerunPreflight(payload) {
    const decisions = Array.isArray(payload.decisions) ? payload.decisions : [];
    const eligible = decisions.filter((decision) => decision.eligible).map((decision) => Number(decision.issue));
    const skipped = decisions.filter((decision) => !decision.eligible).map((decision) => Number(decision.issue));
    const reopening = decisions.filter((decision) => decision.will_reopen).length;
    freshLifecycleRerunEligibleIssueIds = eligible;
    const summaryType = skipped.length ? 'warning' : 'success';
    const summary = [
        `${eligible.length} eligible`,
        `${skipped.length} skipped`,
        `${reopening} will reopen`,
    ].join(' | ');
    const confirmDisabled = eligible.length === 0 ? ' disabled' : '';
    const results = document.getElementById('freshLifecycleRerunResults');
    if (!results) return;
    results.innerHTML = `
        <div class="fresh-lifecycle-rerun-summary ${summaryType}">${escapeHtml(summary)}</div>
        <ul class="fresh-lifecycle-rerun-list">
            ${decisions.map(renderFreshLifecycleRerunDecision).join('')}
        </ul>
        <div class="fresh-lifecycle-rerun-actions">
            <button type="button" class="issue-action-btn" onclick="previewFreshLifecycleRerun()">Refresh Preview</button>
            <button type="button" class="issue-action-btn active" id="freshLifecycleRerunConfirmBtn" onclick="executeFreshLifecycleRerun()"${confirmDisabled}>Rerun Eligible Issues From Scratch</button>
        </div>
    `;
}

async function previewFreshLifecycleRerun() {
    const input = document.getElementById('freshLifecycleRerunIssues');
    const issueNumbers = parseFreshLifecycleRerunIssueInput(input ? input.value : '');
    if (!issueNumbers.length) {
        renderFreshLifecycleRerunMessage('Enter at least one issue number.', 'warning');
        return;
    }
    freshLifecycleRerunPreviewedIssueIds = issueNumbers;
    setFreshLifecycleRerunBusy(true);
    renderFreshLifecycleRerunMessage('Checking issues...', 'info');
    try {
        const req = uiActionContract.buildFreshLifecycleRerunPreflightRequest(issueNumbers);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            renderFreshLifecycleRerunMessage(data.error || `Preview failed (${res.status})`, 'error');
            showToast(data.error || `Fresh lifecycle rerun preview failed (${res.status})`, 'error');
            return;
        }
        renderFreshLifecycleRerunPreflight(data);
    } catch (err) {
        console.error('Fresh lifecycle rerun preview failed:', err);
        renderFreshLifecycleRerunMessage('Preview failed: network error', 'error');
        showToast('Fresh lifecycle rerun preview failed: network error', 'error');
    } finally {
        setFreshLifecycleRerunBusy(false);
    }
}

function renderFreshLifecycleRerunExecution(payload) {
    const reset = Array.isArray(payload.reset) ? payload.reset : [];
    const skipped = Array.isArray(payload.skipped) ? payload.skipped : [];
    const failed = Array.isArray(payload.failed) ? payload.failed : [];
    const rows = [
        ...reset.map((item) => ({
            issue: item.issue,
            eligible: true,
            actionText: item.reopened ? 'Reopened and reset' : 'Reset',
            title: null,
            state: item.reopened ? 'reopened' : 'open',
            will_reopen: Boolean(item.reopened),
            reason: 'Reset from scratch and queued',
        })),
        ...skipped,
        ...failed.map((item) => ({
            issue: item.issue,
            eligible: false,
            actionText: 'Failed',
            title: null,
            state: null,
            will_reopen: false,
            reason: item.error || 'Reset failed',
        })),
    ];
    const results = document.getElementById('freshLifecycleRerunResults');
    if (!results) return;
    results.innerHTML = `
        <div class="fresh-lifecycle-rerun-summary ${failed.length ? 'error' : 'success'}">
            ${escapeHtml(`${reset.length} reset, ${skipped.length} skipped, ${failed.length} failed`)}
        </div>
        <ul class="fresh-lifecycle-rerun-list">
            ${rows.map(renderFreshLifecycleRerunDecision).join('')}
        </ul>
    `;
}

async function executeFreshLifecycleRerun() {
    if (!freshLifecycleRerunPreviewedIssueIds.length) {
        await previewFreshLifecycleRerun();
    }
    if (!freshLifecycleRerunEligibleIssueIds.length) {
        renderFreshLifecycleRerunMessage('No eligible issues to reset.', 'warning');
        return;
    }
    const confirmMsg = `Rerun ${freshLifecycleRerunEligibleIssueIds.length} eligible issue(s) from scratch with a fresh lifecycle?\n\nThis will DELETE local worktrees, DELETE remote branches, remove orchestrator labels, and supersede open orchestrator PRs.\n\nClosed eligible issues will be reopened first. Filtered-out and missing-agent issues are skipped without changes.\n\nPrior review approvals and validation artifacts will not be reused. Next launch will force NEW branches from base (main), not prior issue branch history, and rerun coding, validation, and review under current repo, orchestrator, and prompt conditions.`;
    const confirmBtn = document.getElementById('freshLifecycleRerunConfirmBtn');
    if (!await showConfirm(confirmMsg, confirmBtn)) return;

    setFreshLifecycleRerunBusy(true);
    renderFreshLifecycleRerunMessage('Rerunning eligible issues from scratch...', 'info');
    try {
        const req = uiActionContract.buildFreshLifecycleRerunExecuteRequest(freshLifecycleRerunPreviewedIssueIds);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            renderFreshLifecycleRerunMessage(data.error || `Reset failed (${res.status})`, 'error');
            showToast(data.error || `Fresh lifecycle rerun failed (${res.status})`, 'error');
            return;
        }
        renderFreshLifecycleRerunExecution(data);
        const doneCount = Array.isArray(data.reset) ? data.reset.length : 0;
        const skippedCount = Array.isArray(data.skipped) ? data.skipped.length : 0;
        const failedCount = Array.isArray(data.failed) ? data.failed.length : 0;
        const toastType = failedCount > 0 ? 'warning' : 'success';
        showToast(`Fresh lifecycle rerun: ${doneCount} queued, ${skippedCount} skipped, ${failedCount} failed`, toastType);
        if (doneCount > 0) await refreshViewModel();
    } catch (err) {
        console.error('Fresh lifecycle rerun failed:', err);
        renderFreshLifecycleRerunMessage('Reset failed: network error', 'error');
        showToast('Fresh lifecycle rerun failed: network error', 'error');
    } finally {
        setFreshLifecycleRerunBusy(false);
    }
}
