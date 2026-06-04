function toggleSelectAllBlocked() {
    const checkboxes = blockedList.querySelectorAll('input[type="checkbox"]');
    const isChecked = blockedSelectAll.checked;
    checkboxes.forEach(cb => cb.checked = isChecked);
    updateBlockedButton();
}

function updateBlockedSelection() {
    const checkboxes = blockedList.querySelectorAll('input[type="checkbox"]');
    const checkedBoxes = blockedList.querySelectorAll('input[type="checkbox"]:checked');
    const allChecked = checkboxes.length > 0 && checkboxes.length === checkedBoxes.length;
    const someChecked = checkedBoxes.length > 0;

    blockedSelectAll.checked = allChecked;
    blockedSelectAll.indeterminate = someChecked && !allChecked;
    blockedSelectAllLabel.textContent = `Select All (${blockedIssuesData.length})`;

    updateBlockedButton();
}

function updateBlockedButton() {
    const checkedBoxes = blockedList.querySelectorAll('input[type="checkbox"]:checked');
    const count = checkedBoxes.length;
    blockedUnblockBtn.disabled = count === 0;
    blockedUnblockBtn.textContent = `Unblock & Retry (${count})`;
    blockedResetBtn.disabled = count === 0;
    blockedResetBtn.textContent = `Reset & Retry (${count})`;
}

async function unblockSelectedIssues() {
    const checkedBoxes = blockedList.querySelectorAll('input[type="checkbox"]:checked');
    const issueNumbers = Array.from(checkedBoxes).map(cb => parseInt(cb.dataset.issue));

    if (issueNumbers.length === 0) return;

    // Confirm
    const needsHumanSelected = Array.from(checkedBoxes).filter(cb => cb.dataset.needsHuman === 'true').length;
    let confirmMsg = `Requeue ${issueNumbers.length} issue${issueNumbers.length > 1 ? 's' : ''}?\n\nThis will REMOVE retry-gating labels (including blocking labels and pr-pending).\n\nIt will not delete local worktrees or remote branches.`;
    if (needsHumanSelected > 0) {
        confirmMsg += `\n\n⚠️ ${needsHumanSelected} issue${needsHumanSelected > 1 ? 's have' : ' has'} 'needs-human' label - make sure you've addressed the concern.`;
    }
    if (!await showConfirm(confirmMsg, blockedUnblockBtn)) return;

    // Disable button during request
    blockedUnblockBtn.disabled = true;
    blockedUnblockBtn.textContent = 'Unblocking...';

    try {
        const req = uiActionContract.buildUnblockRequest(issueNumbers);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json();

        if (data.unblocked && data.unblocked.length > 0) {
            applyOptimisticRequeue(data.unblocked, ['blocked']);
            showToast(`Unblocked ${data.unblocked.length} issue${data.unblocked.length > 1 ? 's' : ''}`);
            closeBlockedModal();
            await refreshViewModel();
        } else if (data.failed && data.failed.length > 0) {
            showToast(`Failed to unblock some issues: ${data.failed.map(f => f.error).join(', ')}`, 'error');
        }
    } catch (err) {
        console.error('Failed to unblock issues:', err);
        showToast('Failed to unblock issues', 'error');
    }

    updateBlockedButton();
}

async function resetSelectedIssues() {
    const checkedBoxes = blockedList.querySelectorAll('input[type="checkbox"]:checked');
    const issueNumbers = Array.from(checkedBoxes).map(cb => parseInt(cb.dataset.issue));

    if (issueNumbers.length === 0) return;

    // Confirm with warning about destructive nature
    const confirmMsg = `Full reset and requeue ${issueNumbers.length} issue${issueNumbers.length > 1 ? 's' : ''}?\n\nThis will DELETE:\n\u2022 Local worktrees\n\u2022 Remote branches\n\u2022 Orchestrator labels\n\nAfter reset, the issues will be requeued for a fresh retry.`;
    if (!await showConfirm(confirmMsg, blockedResetBtn)) return;

    // Disable buttons during request
    blockedResetBtn.disabled = true;
    blockedResetBtn.textContent = 'Resetting...';
    blockedUnblockBtn.disabled = true;

    try {
        const req = uiActionContract.buildResetRetryRequest(issueNumbers, { fromScratch: false });
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json();

        if (data.reset && data.reset.length > 0) {
            applyOptimisticRequeue(data.reset, ['blocked']);
            showToast(`Reset ${data.reset.length} issue${data.reset.length > 1 ? 's' : ''} - ready for fresh retry`);
            closeBlockedModal();
            await refreshViewModel();
        } else if (data.failed && data.failed.length > 0) {
            showToast(`Failed to reset some issues: ${data.failed.map(f => f.error).join(', ')}`, 'error');
        }
    } catch (err) {
        console.error('Failed to reset issues:', err);
        showToast('Failed to reset issues', 'error');
    }

    updateBlockedButton();
}

async function resetSelectedIssuesFromScratch() {
    const checkedBoxes = blockedList.querySelectorAll('input[type="checkbox"]:checked');
    const issueNumbers = Array.from(checkedBoxes).map(cb => parseInt(cb.dataset.issue));

    if (issueNumbers.length === 0) return;

    const confirmMsg = `Full reset and requeue ${issueNumbers.length} issue${issueNumbers.length > 1 ? 's' : ''} from scratch?\n\nThis will DELETE:\n• Local worktrees\n• Remote branches\n• Orchestrator labels\n\nThis will also supersede open orchestrator PRs by commenting and closing them.\n\nPrior review approvals and validation artifacts will not be reused. Next launch will force NEW branches from base (main), not prior issue branch history.`;
    if (!await showConfirm(confirmMsg, blockedResetBtn)) return;

    blockedResetBtn.disabled = true;
    blockedResetBtn.textContent = 'Resetting...';
    blockedUnblockBtn.disabled = true;

    try {
        const req = uiActionContract.buildResetRetryRequest(issueNumbers, { fromScratch: true });
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json();

        if (data.reset && data.reset.length > 0) {
            applyOptimisticRequeue(data.reset, ['blocked']);
            showToast(`Reset ${data.reset.length} issue${data.reset.length > 1 ? 's' : ''} from scratch`);
            closeBlockedModal();
            await refreshViewModel();
        } else if (data.failed && data.failed.length > 0) {
            showToast(`Failed to reset some issues: ${data.failed.map(f => f.error).join(', ')}`, 'error');
        }
    } catch (err) {
        console.error('Failed to reset issues from scratch:', err);
        showToast('Failed to reset issues from scratch', 'error');
    }

    updateBlockedButton();
}

// Copy worktree path to clipboard
async function copyWorktreePath(path, event) {
    event.stopPropagation();
    try {
        await navigator.clipboard.writeText(path);
        showToast('Worktree path copied to clipboard');
    } catch (err) {
        // Fallback for older browsers
        const textArea = document.createElement('textarea');
        textArea.value = path;
        document.body.appendChild(textArea);
        textArea.select();
        document.execCommand('copy');
        document.body.removeChild(textArea);
        showToast('Worktree path copied to clipboard');
    }
}

// Resume processing for a blocked issue with a recorded run completion
async function resumeIssue(issueNumber, runDir, event) {
    event.stopPropagation();
    const btn = event.target;
    const originalText = btn.textContent;
    if (!runDir) {
        showToast('Cannot resume: missing recorded run directory', 'error');
        return;
    }
    btn.disabled = true;
    btn.textContent = 'Resuming...';

    try {
        const req = uiActionContract.buildIssueResumeRequest(issueNumber, runDir);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json();

        if (data.success) {
            showToast(`Issue #${issueNumber} resumed successfully${data.pr_url ? ' - PR created' : ''}`);
            closeBlockedModal();
            // Reload to show updated state
            setTimeout(() => location.reload(), 500);
        } else {
            showToast(`Failed to resume: ${data.error || 'Unknown error'}`, 'error');
            btn.disabled = false;
            btn.textContent = originalText;
        }
    } catch (err) {
        console.error('Failed to resume issue:', err);
        showToast('Failed to resume issue', 'error');
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

// Launch interactive debug session for a blocked issue
async function launchDebugSession(issueNumber, event) {
    event.stopPropagation();
    const btn = event.target;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Launching...';

    try {
        const res = await fetch(`/api/issues/${issueNumber}/debug-session`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await res.json();

        if (data.success) {
            showToast(`Debug session launched for #${issueNumber}. Use 'coding-done --resume' when done.`);
            closeBlockedModal();
            // Reload to show updated state
            setTimeout(() => location.reload(), 500);
        } else {
            showToast(`Failed to launch: ${data.error || 'Unknown error'}`, 'error');
            btn.disabled = false;
            btn.textContent = originalText;
        }
    } catch (err) {
        console.error('Failed to launch debug session:', err);
        showToast('Failed to launch debug session', 'error');
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

// Retry a blocked issue (removes blocked label and re-queues)
async function retryIssue(issueNumber, event) {
    event.stopPropagation();
    const btn = event.target.closest('.issue-action-btn') || event.target;
    const originalHTML = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span aria-hidden="true">⏳</span> Retrying...';

    try {
        const req = uiActionContract.buildIssueRetryRequest(issueNumber);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json();

        if (data.success) {
            applyOptimisticRequeue([issueNumber], ['blocked']);
            showToast(`Issue #${issueNumber} queued for retry`);
            await refreshViewModel();
        } else {
            showToast(`Failed to retry: ${data.error || 'Unknown error'}`, 'error');
            btn.disabled = false;
            btn.innerHTML = originalHTML;
        }
    } catch (err) {
        console.error('Failed to retry issue:', err);
        showToast('Failed to retry issue', 'error');
        btn.disabled = false;
        btn.innerHTML = originalHTML;
    }
}

// Diagnosis functionality
const diagnosisCache = {};  // Cache diagnosis data

async function toggleDiagnosis(issueNumber, event) {
    event.stopPropagation();
    const panel = document.getElementById(`diagnosis-${issueNumber}`);
    const btn = event.target;

    if (panel.classList.contains('visible')) {
        panel.classList.remove('visible');
        btn.textContent = 'Diagnose';
        return;
    }

    // Show loading
    panel.classList.add('visible');
    btn.textContent = 'Hide';
    panel.innerHTML = '<div class="diagnosis-loading">Analyzing session logs...</div>';

    // Fetch diagnosis if not cached
    if (!diagnosisCache[issueNumber]) {
        try {
            const res = await fetch(`/api/failure-diagnosis/${issueNumber}`);
            diagnosisCache[issueNumber] = await res.json();
        } catch (err) {
            panel.innerHTML = `<div class="diagnosis-error">Failed to load diagnosis: ${err.message}</div>`;
            return;
        }
    }

    renderDiagnosis(issueNumber, diagnosisCache[issueNumber]);
}

function renderDiagnosis(issueNumber, data) {
    const panel = document.getElementById(`diagnosis-${issueNumber}`);
    if (!panel) return;

    let html = '<div class="diagnosis-content">';

    // Basic info
    html += `
        <div class="diagnosis-row">
            <span class="diagnosis-label">AI System:</span>
            <span class="diagnosis-value">${data.ai_system || 'unknown'}</span>
        </div>
        <div class="diagnosis-row">
            <span class="diagnosis-label">Permission Mode:</span>
            <span class="diagnosis-value">${data.permission_mode || 'unknown'}</span>
        </div>
    `;

    // Warnings
    if (data.warnings && data.warnings.length > 0) {
        html += '<div class="diagnosis-warnings">';
        for (const warning of data.warnings) {
            html += `<div class="diagnosis-warning">⚠️ ${escapeHtml(warning)}</div>`;
        }
        html += '</div>';
    }

    // Suggestions
    if (data.suggestions && data.suggestions.length > 0) {
        html += '<div class="diagnosis-suggestions">';
        for (const suggestion of data.suggestions) {
            html += `<div class="diagnosis-suggestion">💡 ${escapeHtml(suggestion)}</div>`;
        }
        // Add suggestion to try without -p flag for debugging
        html += `<div class="diagnosis-suggestion">💡 To debug interactively, remove -p flag from agent command to see Claude's UI directly</div>`;
        html += '</div>';
    }

    // Log context preview
    if (data.log_context) {
        html += `
            <div class="diagnosis-row" style="margin-top: 8px;">
                <span class="diagnosis-label">Log Analysis:</span>
            </div>
            <pre style="font-size: 11px; max-height: 200px; overflow: auto; background: var(--bg); padding: 8px; border-radius: 4px; white-space: pre-wrap;">${escapeHtml(data.log_context)}</pre>
        `;
    }

    // View log button - uses sanitized viewer
    html += `
        <button class="open-log-btn" onclick="openSessionManifest(${issueNumber})">
            Open Session Diagnostics
        </button>
    `;

    html += '</div>';
    panel.innerHTML = html;
}

function copyPathToClipboard(path, successMessage = 'Path copied') {
    return navigator.clipboard.writeText(path)
        .then(() => showToast(successMessage))
        .catch(() => showToast('Failed to copy path', 'error'));
}

function handleHostActionResponse(data, label = 'path') {
    if (data.error) {
        showToast(`Could not open ${label}: ${data.error}`, 'error');
        return;
    }
    if (data.action === 'opened') {
        showToast(`Opening ${label}...`);
        return;
    }
    if (data.action === 'copy_path' && data.path) {
        copyPathToClipboard(data.path, `${label[0].toUpperCase()}${label.slice(1)} copied`);
        if (data.message) {
            showToast(data.message);
        }
        return;
    }
    showToast(`Could not open ${label}`, 'error');
}

function openLogFile(path) {
    const req = uiActionContract.buildHostOpenPathRequest(path);
    fetch(req.endpoint, {
        method: req.method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body),
    }).then(res => res.json()).then(data => {
        handleHostActionResponse(data, 'file');
    }).catch(err => {
        showToast(`Failed to open file: ${err.message}`, 'error');
    });
}

function openPath(path) {
    if (!path) {
        showToast('No path available', 'error');
        return;
    }
    openLogFile(path);
}

async function openFilteredOrchestratorLog(issueNumber, runDir = null, errorSurface = 'toast') {
    try {
        clearDiagnosticsActionMessage();
        showToast('Generating issue-scoped orchestrator log...');
        const params = new URLSearchParams();
        if (runDir) params.set('run_dir', runDir);
        const suffix = params.toString() ? `?${params.toString()}` : '';
        const res = await fetch(`/api/session/orchestrator-log/${issueNumber}${suffix}`);
        const data = await res.json();
        if (data.error) {
            reportActionError(data.error, errorSurface);
            return;
        }
        openPath(data.filtered_log_path);
    } catch (err) {
        reportActionError(`Failed to get orchestrator log: ${err.message}`, errorSurface);
    }
}

// Claude Log Viewer
async function viewClaudeLog(issueNumber, runDir = null, errorSurface = 'toast') {
    if (!runDir) {
        reportActionError('Claude log requires run context. Open from a timeline entry.', errorSurface);
        return;
    }
    try {
        clearDiagnosticsActionMessage();
        showToast('Loading Claude log...');
        const params = new URLSearchParams({ limit: '500' });
        params.set('run_dir', runDir);
        const res = await fetch(`/api/session/claude-log/${issueNumber}?${params.toString()}`);
        const data = await res.json();
        if (data.error) {
            reportActionError(data.error, errorSurface);
            document.getElementById('modalTitle').textContent = `Claude Session Log #${issueNumber}`;
            document.getElementById('modalBody').innerHTML = `
                <div class="timeline-empty">
                    ${escapeHtml(data.error)}
                    <div style="margin-top:8px;color:var(--text-muted);font-size:12px;">
                        This run may predate manifest log capture or logs may have been rotated.
                    </div>
                </div>
            `;
            document.getElementById('modalOverlay').classList.add('visible');
            return;
        }
        renderClaudeLogViewer(data);
    } catch (err) {
        showToast(`Failed to load Claude log: ${err.message}`, 'error');
    }
}

function renderClaudeLogViewer(data) {
    const entries = data.entries || [];
    let html = `
        <div class="log-viewer-header">
            <div class="log-viewer-info">${entries.length} entries from ${escapeHtml(data.log_path || 'unknown')}</div>
            <div class="log-viewer-controls">
                <select class="log-viewer-filter" onchange="filterLogEntries(this.value)">
                    <option value="all">All entries</option>
                    <option value="assistant">Assistant only</option>
                    <option value="user">User only</option>
                    <option value="tool">Tool calls only</option>
                </select>
            </div>
        </div>
        <div class="log-entries" id="logEntries">
    `;

    entries.forEach((entry, idx) => {
        const entryType = getEntryType(entry);
        const preview = getEntryPreview(entry);
        html += `
            <div class="log-entry" data-type="${entryType}" onclick="toggleLogEntry(this)">
                <div class="log-entry-summary">
                    <span class="log-entry-type ${entryType}">${entryType}</span>
                    <span class="log-entry-preview">${escapeHtml(preview)}</span>
                </div>
                <div class="log-entry-details">
                    <pre>${syntaxHighlightJson(entry)}</pre>
                </div>
            </div>
        `;
    });

    html += '</div>';

    // Open with larger modal class
    const modal = document.getElementById('modalOverlay');
    const modalEl = modal.querySelector('.modal');
    modalEl.classList.add('log-viewer-modal');
    openModal(`Claude Session Log #${data.issue_number}`, html);
}

function getEntryType(entry) {
    if (entry.type === 'human' || entry.role === 'user') return 'user';
    if (entry.type === 'ai' || entry.role === 'assistant') return 'assistant';
    if (entry.type === 'tool' || entry.tool_use_id) return 'tool_result';
    if (entry.name && entry.input) return 'tool_use';
    return entry.type || 'unknown';
}

function getEntryPreview(entry) {
    // Try to extract meaningful preview text
    if (entry.content) {
        if (typeof entry.content === 'string') {
            return entry.content.substring(0, 150);
        }
        if (Array.isArray(entry.content)) {
            const textBlock = entry.content.find(b => b.type === 'text');
            if (textBlock && textBlock.text) {
                return textBlock.text.substring(0, 150);
            }
            const toolUse = entry.content.find(b => b.type === 'tool_use');
            if (toolUse) {
                return `Tool: ${toolUse.name}`;
            }
        }
    }
    if (entry.name && entry.input) {
        return `Tool: ${entry.name}`;
    }
    if (entry.message) {
        // Handle message being either a string or an object
        const msg = typeof entry.message === 'string' ? entry.message : JSON.stringify(entry.message);
        return msg.substring(0, 150);
    }
    return JSON.stringify(entry).substring(0, 100);
}

function toggleLogEntry(el) {
    el.classList.toggle('expanded');
}

function filterLogEntries(filter) {
    const entries = document.querySelectorAll('.log-entry');
    entries.forEach(entry => {
        const type = entry.dataset.type;
        if (filter === 'all') {
            entry.style.display = '';
        } else if (filter === 'tool') {
            entry.style.display = (type === 'tool_use' || type === 'tool_result') ? '' : 'none';
        } else {
            entry.style.display = type === filter ? '' : 'none';
        }
    });
}

function syntaxHighlightJson(obj) {
    const json = JSON.stringify(obj, null, 2);
    return json
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?)/g, (match) => {
            let cls = 'json-string';
            if (/:$/.test(match)) {
                cls = 'json-key';
                // Remove the colon from the match for proper highlighting
                return `<span class="${cls}">${match.slice(0, -1)}</span>:`;
            }
            return `<span class="${cls}">${match}</span>`;
        })
        .replace(/\b(true|false)\b/g, '<span class="json-boolean">$1</span>')
        .replace(/\bnull\b/g, '<span class="json-null">null</span>')
        .replace(/\b(-?\d+\.?\d*)\b/g, '<span class="json-number">$1</span>');
}

let liveLogPoller = null;
let liveLogIssue = null;

function stopLiveLogPoller() {
    if (liveLogPoller) {
        clearInterval(liveLogPoller);
        liveLogPoller = null;
    }
    liveLogIssue = null;
}
