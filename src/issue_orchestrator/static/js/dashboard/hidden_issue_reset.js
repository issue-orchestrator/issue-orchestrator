let hiddenScratchPreviewedIssueIds = [];
let hiddenScratchEligibleIssueIds = [];

function parseHiddenScratchResetIssueInput(value) {
    const matches = String(value || '').match(/\d+/g) || [];
    return uiActionContract.normalizeIssueNumbers(matches);
}

function openHiddenScratchResetDialog() {
    const menu = document.getElementById('settingsMenu');
    if (menu) menu.classList.remove('visible');
    hiddenScratchPreviewedIssueIds = [];
    hiddenScratchEligibleIssueIds = [];
    openModal('Rerun Hidden Issues From Scratch', `
        <form id="hiddenScratchResetForm" class="hidden-scratch-reset-form">
            <label class="prefs-row hidden-scratch-reset-field" for="hiddenScratchResetIssues">
                Issue numbers
                <textarea id="hiddenScratchResetIssues" class="hidden-scratch-reset-input" rows="6" aria-describedby="hiddenScratchResetHelp" placeholder="6376, 6377"></textarea>
            </label>
            <div class="hidden-scratch-reset-note" id="hiddenScratchResetHelp">
                Closed eligible issues are reopened before reset. Filtered-out and missing-agent issues are skipped without changes.
            </div>
            <div class="hidden-scratch-reset-warning">
                This uses the full reset-from-scratch boundary and a fresh lifecycle rerun: delete local worktrees, delete remote branches, remove orchestrator labels, supersede open orchestrator PRs, force new branches from base, and rerun coding, validation, and review under current conditions.
            </div>
            <div class="hidden-scratch-reset-actions">
                <button type="button" class="issue-action-btn" onclick="closeModal()">Cancel</button>
                <button type="submit" class="issue-action-btn active" id="hiddenScratchResetPreviewBtn">Preview</button>
            </div>
            <div id="hiddenScratchResetResults" class="hidden-scratch-reset-results" role="status" aria-live="polite"></div>
        </form>
    `);
    const form = document.getElementById('hiddenScratchResetForm');
    const input = document.getElementById('hiddenScratchResetIssues');
    if (form) {
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            previewHiddenScratchReset();
        });
    }
    if (input) {
        input.addEventListener('input', () => {
            hiddenScratchPreviewedIssueIds = [];
            hiddenScratchEligibleIssueIds = [];
            const results = document.getElementById('hiddenScratchResetResults');
            if (results) results.replaceChildren();
        });
        setTimeout(() => input.focus(), 0);
    }
}

function setHiddenScratchResetBusy(busy) {
    const previewBtn = document.getElementById('hiddenScratchResetPreviewBtn');
    const confirmBtn = document.getElementById('hiddenScratchResetConfirmBtn');
    if (previewBtn) previewBtn.disabled = busy;
    if (confirmBtn) confirmBtn.disabled = busy || hiddenScratchEligibleIssueIds.length === 0;
}

function renderHiddenScratchResetMessage(message, type = 'info') {
    const results = document.getElementById('hiddenScratchResetResults');
    if (!results) return;
    results.innerHTML = `<div class="hidden-scratch-reset-summary ${escapeAttr(type)}">${escapeHtml(message)}</div>`;
}

function hiddenScratchResetDecisionLabel(decision) {
    if (decision && decision.actionText) return String(decision.actionText);
    if (!decision || !decision.eligible) return 'Skipped';
    if (decision.will_reopen) return 'Will reopen and reset';
    return 'Will reset';
}

function renderHiddenScratchResetDecision(decision) {
    const issue = Number(decision.issue);
    const title = decision.title ? ` ${decision.title}` : '';
    const action = hiddenScratchResetDecisionLabel(decision);
    const statusClass = action === 'Failed' ? 'failed' : (decision.eligible ? 'eligible' : 'skipped');
    const stateText = decision.state ? ` (${decision.state})` : '';
    return `
        <li class="hidden-scratch-reset-row ${statusClass}">
            <span class="hidden-scratch-reset-issue">#${issue}${escapeHtml(title)}${escapeHtml(stateText)}</span>
            <span class="hidden-scratch-reset-action">${escapeHtml(action)}</span>
            <span class="hidden-scratch-reset-reason">${escapeHtml(decision.reason || '')}</span>
        </li>`;
}

function renderHiddenScratchResetPreflight(payload) {
    const decisions = Array.isArray(payload.decisions) ? payload.decisions : [];
    const eligible = decisions.filter((decision) => decision.eligible).map((decision) => Number(decision.issue));
    const skipped = decisions.filter((decision) => !decision.eligible).map((decision) => Number(decision.issue));
    const reopening = decisions.filter((decision) => decision.will_reopen).length;
    hiddenScratchEligibleIssueIds = eligible;
    const summaryType = skipped.length ? 'warning' : 'success';
    const summary = [
        `${eligible.length} eligible`,
        `${skipped.length} skipped`,
        `${reopening} will reopen`,
    ].join(' | ');
    const confirmDisabled = eligible.length === 0 ? ' disabled' : '';
    const results = document.getElementById('hiddenScratchResetResults');
    if (!results) return;
    results.innerHTML = `
        <div class="hidden-scratch-reset-summary ${summaryType}">${escapeHtml(summary)}</div>
        <ul class="hidden-scratch-reset-list">
            ${decisions.map(renderHiddenScratchResetDecision).join('')}
        </ul>
        <div class="hidden-scratch-reset-actions">
            <button type="button" class="issue-action-btn" onclick="previewHiddenScratchReset()">Refresh Preview</button>
            <button type="button" class="issue-action-btn active" id="hiddenScratchResetConfirmBtn" onclick="executeHiddenScratchReset()"${confirmDisabled}>Reset Eligible Issues From Scratch</button>
        </div>
    `;
}

async function previewHiddenScratchReset() {
    const input = document.getElementById('hiddenScratchResetIssues');
    const issueNumbers = parseHiddenScratchResetIssueInput(input ? input.value : '');
    if (!issueNumbers.length) {
        renderHiddenScratchResetMessage('Enter at least one issue number.', 'warning');
        return;
    }
    hiddenScratchPreviewedIssueIds = issueNumbers;
    setHiddenScratchResetBusy(true);
    renderHiddenScratchResetMessage('Checking issues...', 'info');
    try {
        const req = uiActionContract.buildHiddenScratchResetPreflightRequest(issueNumbers);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            renderHiddenScratchResetMessage(data.error || `Preview failed (${res.status})`, 'error');
            showToast(data.error || `Hidden reset preview failed (${res.status})`, 'error');
            return;
        }
        renderHiddenScratchResetPreflight(data);
    } catch (err) {
        console.error('Hidden scratch reset preview failed:', err);
        renderHiddenScratchResetMessage('Preview failed: network error', 'error');
        showToast('Hidden reset preview failed: network error', 'error');
    } finally {
        setHiddenScratchResetBusy(false);
    }
}

function renderHiddenScratchResetExecution(payload) {
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
    const results = document.getElementById('hiddenScratchResetResults');
    if (!results) return;
    results.innerHTML = `
        <div class="hidden-scratch-reset-summary ${failed.length ? 'error' : 'success'}">
            ${escapeHtml(`${reset.length} reset, ${skipped.length} skipped, ${failed.length} failed`)}
        </div>
        <ul class="hidden-scratch-reset-list">
            ${rows.map(renderHiddenScratchResetDecision).join('')}
        </ul>
    `;
}

async function executeHiddenScratchReset() {
    if (!hiddenScratchPreviewedIssueIds.length) {
        await previewHiddenScratchReset();
    }
    if (!hiddenScratchEligibleIssueIds.length) {
        renderHiddenScratchResetMessage('No eligible issues to reset.', 'warning');
        return;
    }
    const confirmMsg = `Rerun ${hiddenScratchEligibleIssueIds.length} hidden issue(s) from scratch?\n\nThis will DELETE local worktrees, DELETE remote branches, remove orchestrator labels, and supersede open orchestrator PRs.\n\nClosed eligible issues will be reopened first. Filtered-out and missing-agent issues are skipped without changes.\n\nPrior review approvals and validation artifacts will not be reused. Next launch will force NEW branches from base (main), not prior issue branch history, and rerun coding, validation, and review under current repo, orchestrator, and prompt conditions.`;
    const confirmBtn = document.getElementById('hiddenScratchResetConfirmBtn');
    if (!await showConfirm(confirmMsg, confirmBtn)) return;

    setHiddenScratchResetBusy(true);
    renderHiddenScratchResetMessage('Rerunning eligible issues from scratch...', 'info');
    try {
        const req = uiActionContract.buildHiddenScratchResetExecuteRequest(hiddenScratchPreviewedIssueIds);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            renderHiddenScratchResetMessage(data.error || `Reset failed (${res.status})`, 'error');
            showToast(data.error || `Hidden reset failed (${res.status})`, 'error');
            return;
        }
        renderHiddenScratchResetExecution(data);
        const doneCount = Array.isArray(data.reset) ? data.reset.length : 0;
        const skippedCount = Array.isArray(data.skipped) ? data.skipped.length : 0;
        const failedCount = Array.isArray(data.failed) ? data.failed.length : 0;
        const toastType = failedCount > 0 ? 'warning' : 'success';
        showToast(`Hidden rerun: ${doneCount} queued, ${skippedCount} skipped, ${failedCount} failed`, toastType);
        if (doneCount > 0) await refreshViewModel();
    } catch (err) {
        console.error('Hidden scratch reset failed:', err);
        renderHiddenScratchResetMessage('Reset failed: network error', 'error');
        showToast('Hidden reset failed: network error', 'error');
    } finally {
        setHiddenScratchResetBusy(false);
    }
}
