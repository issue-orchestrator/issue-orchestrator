// Helper to hide settings menu (used by multiple functions)
function hideSettingsMenu() {
    const menu = document.getElementById('settingsMenu');
    if (menu) menu.classList.remove('visible');
}

let terminalBackend = 'tmux';
let currentCommitSha = null;
let viewModel = null;
fetch('/api/info')
    .then(res => res.json())
    .then(data => {
        if (data.terminal_backend) {
            terminalBackend = data.terminal_backend;
        }
        if (data.commit_short) {
            currentCommitSha = data.commit_sha;
            const commitEl = document.getElementById('menuCommitSha');
            if (commitEl) {
                commitEl.textContent = data.commit_short;
            }
        }
        updateActionHints();
    })
    .catch(() => {});

function updateActionHints() {
    const rows = document.querySelectorAll('.issue-row');
    rows.forEach((row) => {
        const action = row.dataset.action;
        const issueAction = row.querySelector('.issue-action');
        let hint = '';
        if (action === 'focus') {
            hint = terminalBackend === 'subprocess'
                ? 'View agent log'
                : 'Focus terminal session';
        } else if (row.dataset.url) {
            hint = 'Open issue on GitHub';
        }
        if (issueAction) {
            issueAction.title = hint;
        }
    });
}

function copyCommitSha() {
    if (!currentCommitSha) {
        showToast('Commit SHA not available', 'error');
        return;
    }
    navigator.clipboard.writeText(currentCommitSha)
        .then(() => showToast('Commit SHA copied'))
        .catch(() => showToast('Failed to copy commit SHA', 'error'));
}

function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === 'function') {
        return window.CSS.escape(value);
    }
    return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
}

function updateStatusBadgeFromViewModel(vm) {
    const badge = document.querySelector('.status-badge');
    if (!badge || !vm) return;

    const startupComplete = vm.startup_status === 'complete';
    const shutdownRequested = vm.shutdown_requested;
    const paused = vm.paused;
    const activeCount = vm.active_session_count || 0;

    let text = 'Running';
    let className = 'status-badge status-running';

    if (!startupComplete) {
        text = 'Starting...';
        className = 'status-badge status-starting';
    } else if (shutdownRequested && activeCount > 0) {
        text = `Shutting down... (${activeCount})`;
        className = 'status-badge status-paused';
    } else if (shutdownRequested) {
        text = 'Stopped';
        className = 'status-badge status-paused';
    } else if (paused && activeCount > 0) {
        text = `Pausing... (${activeCount})`;
        className = 'status-badge status-paused';
    } else if (paused) {
        text = 'Paused';
        className = 'status-badge status-paused';
    }

    badge.textContent = text;
    badge.className = className;
}

function updatePauseMenuFromViewModel(vm) {
    const menuItem = document.getElementById('pauseResumeItem');
    if (!menuItem || !vm) return;
    if (vm.paused) {
        menuItem.innerHTML = '<span aria-hidden="true">▶</span> Resume';
    } else {
        menuItem.innerHTML = '<span aria-hidden="true">⏸</span> Pause';
    }
}

function buildEmptyStateHtml(vm) {
    if (!vm) {
        return 'No issues in queue';
    }
    if (vm.active_tab === 'history') {
        return 'No session history yet';
    }
    if (vm.active_tab === 'blocked') {
        return 'Nothing needs attention - all systems running smoothly!';
    }
    if (vm.active_tab === 'e2e') {
        return '<div class=\"e2e-empty-state\">' +
            '<p>No E2E test activity</p>' +
            '<button class=\"issue-action-btn start-btn\" onclick=\"startE2E()\">' +
            '<span aria-hidden=\"true\">▶</span> Start E2E Tests' +
            '</button>' +
            '</div>';
    }
    return 'No issues in queue';
}

function ensureEmptyState(vm, hasRows) {
    const list = document.getElementById('issueList');
    if (!list) return;

    let emptyState = list.querySelector('.empty-state');
    if (!emptyState) {
        emptyState = document.createElement('div');
        emptyState.className = 'empty-state';
        list.appendChild(emptyState);
    }

    if (hasRows) {
        emptyState.style.display = 'none';
        return;
    }

    emptyState.style.display = 'block';
    emptyState.innerHTML = buildEmptyStateHtml(vm);
}

async function refreshIssueRows(vm) {
    const list = document.getElementById('issueList');
    if (!list) return;

    const url = new URL('/api/issue-rows', window.location.origin);
    const params = new URL(window.location.href).searchParams;
    if (params.get('tab')) url.searchParams.set('tab', params.get('tab'));
    if (params.get('page')) url.searchParams.set('page', params.get('page'));
    if (params.get('e2e_page')) url.searchParams.set('e2e_page', params.get('e2e_page'));

    const res = await fetch(url.toString());
    if (!res.ok) return;
    const data = await res.json();
    const rows = data.rows || [];

    const nextIds = new Set(rows.map(row => String(row.issue_number)));
    const existingGroups = Array.from(list.querySelectorAll('.issue-row-group[data-issue]'));
    existingGroups.forEach(group => {
        if (!nextIds.has(group.dataset.issue)) {
            group.remove();
        }
    });

    const header = list.querySelector('.issue-header');
    let insertAfter = header;
    rows.forEach(row => {
        const id = String(row.issue_number);
        const selector = `.issue-row-group[data-issue=\"${cssEscape(id)}\"]`;
        const existing = list.querySelector(selector);
        const wrapper = document.createElement('div');
        wrapper.innerHTML = row.html.trim();
        const newNode = wrapper.firstElementChild;
        if (!newNode) {
            return;
        }
        if (existing) {
            existing.replaceWith(newNode);
        } else {
            insertAfter.after(newNode);
        }
        insertAfter = newNode;
    });

    ensureEmptyState(vm, rows.length > 0);
    updateActionHints();
}

async function refreshViewModel({ reloadOnListChange = true } = {}) {
    try {
        const url = new URL('/api/view-model', window.location.origin);
        const params = new URL(window.location.href).searchParams;
        if (params.get('tab')) url.searchParams.set('tab', params.get('tab'));
        if (params.get('page')) url.searchParams.set('page', params.get('page'));
        if (params.get('e2e_page')) url.searchParams.set('e2e_page', params.get('e2e_page'));

        const res = await fetch(url.toString());
        if (!res.ok) return;
        viewModel = await res.json();
        window.dashboardData = viewModel.dashboard_data || window.dashboardData;
        isPaused = !!viewModel.paused;
        updateStatusBadgeFromViewModel(viewModel);
        updatePauseMenuFromViewModel(viewModel);

        if (reloadOnListChange && viewModel.startup_status === 'complete') {
            await refreshIssueRows(viewModel);
        }
    } catch (e) {
        console.error('Failed to refresh view-model:', e);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    updateActionHints();
    refreshViewModel({ reloadOnListChange: false });
    const nextRun = document.getElementById('e2eNextRun');
    if (nextRun && nextRun.dataset.nextRunReason) {
        const nextInfo = {
            next_run_at: nextRun.dataset.nextRunAt,
            next_run_reason: nextRun.dataset.nextRunReason,
        };
        const formatted = formatNextRun(nextInfo);
        if (formatted) {
            nextRun.textContent = formatted;
        }
    }
});

let logPoller = null;
let logFollow = true;
let logIssue = null;

function isNearBottom(element, threshold = 24) {
    return element.scrollTop + element.clientHeight >= element.scrollHeight - threshold;
}

async function refreshAgentLog(issueNumber, forceScroll = false) {
    const res = await fetch(`/api/log/local/${issueNumber}`);
    const data = await res.json();

    if (data.error) {
        const msg = data.error + (data.hint ? '\n\n' + data.hint : '');
        document.getElementById('logStatus').textContent = msg;
        return;
    }

    const logPre = document.getElementById('logPre');
    const logScroll = document.getElementById('logScroll');
    if (!logPre || !logScroll) {
        return;
    }

    const wasNearBottom = isNearBottom(logScroll);
    const lines = data.lines || [];
    logPre.textContent = lines.join('\n');
    document.getElementById('logPath').textContent = data.log_path || '';
    document.getElementById('logStatus').textContent = data.truncated
        ? `Showing last ${lines.length} of ${data.total_lines} lines`
        : `Lines: ${data.total_lines}`;

    if (forceScroll || (logFollow && wasNearBottom)) {
        logScroll.scrollTop = logScroll.scrollHeight;
    }
}

async function openAgentLog(issueNumber) {
    logIssue = issueNumber;
    const logContent = `
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#8b949e;">
                <input type="checkbox" id="logFollowToggle" checked>
                Follow
            </label>
            <button class="btn-secondary" style="font-size:11px;padding:4px 8px;" onclick="refreshAgentLog(${issueNumber}, true)">Refresh</button>
            <span id="logStatus" style="font-size:11px;color:#8b949e;"></span>
        </div>
        <div id="logScroll" style="max-height:420px;overflow:auto;background:#161b22;padding:10px;border-radius:4px;">
            <pre id="logPre" style="font-size:11px;white-space:pre-wrap;margin:0;"></pre>
        </div>
        <div style="color:#8b949e;font-size:11px;margin-top:10px;">Log: <span id="logPath"></span></div>
    `;

    document.getElementById('modalTitle').textContent = `Agent UI Log #${issueNumber}`;
    document.getElementById('modalBody').innerHTML = logContent;
    document.getElementById('modalOverlay').classList.add('visible');

    const toggle = document.getElementById('logFollowToggle');
    if (toggle) {
        toggle.addEventListener('change', (e) => {
            logFollow = e.target.checked;
        });
    }

    await refreshAgentLog(issueNumber, true);
    if (logPoller) {
        clearInterval(logPoller);
    }
    logPoller = setInterval(() => {
        refreshAgentLog(issueNumber, false);
    }, 2000);
}

async function openSessionManifest(issueNumber) {
    const res = await fetch(`/api/dialog/session-diagnostics/${issueNumber}`);
    const data = await res.json();
    if (data.error) {
        showToast(data.error, 'error');
        return;
    }

    let html = '<div class="info-grid">';
    for (const row of data.rows || []) {
        html += `<div class="info-row"><span class="info-label">${escapeHtml(row.label)}</span><span class="info-value">${escapeHtml(String(row.value))}</span></div>`;
    }
    html += '</div>';

    if (data.actions && data.actions.length > 0) {
        html += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;">';
        for (const action of data.actions) {
            html += renderDialogAction(action);
        }
        html += '</div>';
    }

    openModal(data.title || `Session Diagnostics #${issueNumber}`, html);
}

function renderDialogAction(action) {
    if (!action) return '';
    const label = escapeHtml(action.label || 'Action');
    if (action.type === 'open_path') {
        return `<button class="btn-secondary" onclick="openPath('${escapeHtml(action.path)}')">${label}</button>`;
    }
    if (action.type === 'open_agent_log') {
        return `<button class="btn-secondary" onclick="openAgentLog(${action.issue_number})">${label}</button>`;
    }
    if (action.type === 'view_claude_log') {
        return `<button class="btn-secondary" onclick="viewClaudeLog(${action.issue_number})">${label}</button>`;
    }
    if (action.type === 'open_orchestrator_log') {
        return `<button class="btn-secondary" onclick="openFilteredOrchestratorLog(${action.issue_number})">${label}</button>`;
    }
    return '';
}

async function sendAgentInput(issueNumber) {
    const input = document.getElementById('agentInput');
    if (!input) {
        showToast('Input field not found', 'error');
        return;
    }
    const text = input.value.trim();
    if (!text) {
        showToast('Please enter a message', 'error');
        return;
    }
    try {
        const res = await fetch(`/api/send/${issueNumber}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
        });
        const data = await res.json();
        if (data.error) {
            showToast(data.error, 'error');
            return;
        }
        showToast(`Sent input to #${issueNumber}`);
        closeModal();
    } catch (err) {
        showToast(`Failed to send input: ${err.message}`, 'error');
    }
}

// Row click handler
async function handleClick(row) {
    const action = row.dataset.action;
    const issueNumber = row.dataset.issue;
    const url = row.dataset.url;
    const e2eRunId = row.dataset.e2eRunId;
    const isE2e = row.dataset.isE2e === 'true';

    // E2E runs open the unified run view
    if (isE2e && e2eRunId) {
        showUnifiedRunView(parseInt(e2eRunId, 10));
        return;
    }

    if (action === 'focus') {
        if (terminalBackend === 'subprocess') {
            try {
                await openAgentLog(issueNumber);
            } catch (err) {
                showToast('Failed to open agent log', 'error');
            }
            return;
        }
        try {
            const res = await fetch(`/api/focus/${issueNumber}`, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'focused') {
                showToast(`Focused session #${issueNumber}`);
            } else if (data.error) {
                showToast(`Could not focus: ${data.error}`, 'error');
            }
        } catch (err) {
            showToast('Failed to focus session', 'error');
        }
    } else if (url) {
        window.open(url, '_blank');
    }
}

// Kill session handler (inline button)
async function killSession(issueNumber, event) {
    event.stopPropagation();
    if (!confirm(`Force kill session #${issueNumber}?\n\nThis will terminate the Claude agent immediately.`)) return;
    try {
        const res = await fetch(`/api/kill/${issueNumber}`, { method: 'POST' });
        const data = await res.json();
        if (data.status === 'killed') {
            showToast(`Killed session #${issueNumber}`);
            location.reload();
        } else {
            showToast(data.error || 'Failed to kill session', true);
        }
    } catch (e) {
        showToast('Failed to kill session: ' + e.message, true);
    }
}

// Auto-refresh during startup
if (!window.dashboardData.startupComplete) {
setTimeout(() => location.reload(), 1000);  // Refresh every 1s during startup
}

// Controls
let isPaused = window.dashboardData.paused;

async function togglePause() {
    const badge = document.querySelector('.status-badge');
    const menuItem = document.getElementById('pauseResumeItem');
    const menu = document.getElementById('settingsMenu');

    // Close the menu
    menu.classList.remove('show');

    if (isPaused) {
        // Resume
        badge.textContent = 'Resuming...';
        badge.classList.remove('status-paused');
        badge.classList.add('status-running');

        await fetch('/api/resume', { method: 'POST' });
        await refreshViewModel({ reloadOnListChange: false });
    } else {
        // Pause
        badge.textContent = 'Pausing...';
        badge.classList.remove('status-running');
        badge.classList.add('status-paused');

        await fetch('/api/pause', { method: 'POST' });
        await refreshViewModel({ reloadOnListChange: false });
    }
}
async function refreshFromGitHub() {
    hideSettingsMenu();
    try {
        const res = await fetch('/api/refresh', { method: 'POST' });
        if (res.ok) {
            showToast('Refreshed from GitHub');
        } else {
            showToast('Refresh failed', true);
        }
    } catch (e) {
        console.error('Refresh failed:', e);
        showToast('Refresh failed', true);
    }
}

// Shutdown state - used to cancel polling when "Shutdown now" is clicked
let shutdownInProgress = false;

async function shutdown() {
    // First, check if there are active sessions
    const statusRes = await fetch('/api/status');
    const status = await statusRes.json();
    const activeSessions = status.active_sessions || [];

    if (activeSessions.length > 0) {
        // Show modal with options
        showShutdownModal(activeSessions);
    } else {
        if (!confirm('Shutdown the orchestrator?')) return;
        await executeShutdown();
    }
}

function showShutdownModal(activeSessions) {
    const sessionList = activeSessions.map(s => `<li>#${s.issue_number}: ${escapeHtml(s.title || 'Untitled')}</li>`).join('');

    const modal = document.createElement('div');
    modal.id = 'shutdownModal';
    modal.className = 'modal-overlay visible';
    modal.innerHTML = `
        <div class="modal-content" style="max-width: 500px;">
            <div class="modal-header">
                <h2>Shutdown Orchestrator</h2>
                <button class="modal-close" onclick="closeShutdownModal()">&times;</button>
            </div>
            <div class="modal-body" id="shutdownModalBody">
                <p><strong>${activeSessions.length} agent(s) currently working:</strong></p>
                <ul style="margin: 12px 0; padding-left: 20px; color: #c9d1d9;">${sessionList}</ul>
                <p style="color: #8b949e; font-size: 0.9em;">
                    "Wait" will stop new work and shutdown when agents finish.<br>
                    "Shutdown now" will interrupt agents immediately.
                </p>
            </div>
            <div class="modal-footer" id="shutdownModalFooter">
                <button class="btn-secondary" onclick="closeShutdownModal()">Cancel</button>
                <button class="btn-secondary" onclick="shutdownWait()">Wait for completion</button>
                <button class="btn-primary" onclick="shutdownNow()">Shutdown now</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

function closeShutdownModal() {
    const modal = document.getElementById('shutdownModal');
    if (modal) modal.remove();
}

async function shutdownWait() {
    // Set shutdown flag (stops new work)
    await fetch('/api/shutdown', { method: 'POST' });

    // Update modal to show waiting state
    const body = document.getElementById('shutdownModalBody');
    const footer = document.getElementById('shutdownModalFooter');

    body.innerHTML = `
        <div style="text-align: center; padding: 20px;">
            <div class="loading-spinner" style="margin: 0 auto 16px;"></div>
            <p id="shutdownWaitStatus">Waiting for sessions to complete...</p>
            <p style="color: #8b949e; font-size: 0.9em; margin-top: 12px;">
                No new work will be started. Shutdown will happen automatically when all agents finish.
            </p>
        </div>
    `;
    footer.innerHTML = `
        <button class="btn-primary" onclick="shutdownNow()">Shutdown now</button>
    `;

    // Poll for session completion
    pollForShutdown();
}

async function pollForShutdown() {
    const statusEl = document.getElementById('shutdownWaitStatus');

    const poll = async () => {
        // Check if shutdown was triggered manually
        if (shutdownInProgress) {
            return;
        }

        try {
            const res = await fetch('/api/status');
            const status = await res.json();
            const activeSessions = status.active_sessions || [];

            if (activeSessions.length === 0) {
                if (statusEl) statusEl.textContent = 'All sessions complete. Shutting down...';
                shutdownInProgress = true;
                await executeShutdown();
                return;
            }

            if (statusEl) {
                statusEl.textContent = `Waiting for ${activeSessions.length} session(s) to complete...`;
            }

            // Poll again in 3 seconds (only if not shutting down)
            if (!shutdownInProgress) {
                setTimeout(poll, 3000);
            }
        } catch (err) {
            // Server might already be down
            if (statusEl) statusEl.textContent = 'Server connection lost.';
        }
    };

    poll();
}

async function shutdownNow() {
    shutdownInProgress = true;  // Cancel any polling
    closeShutdownModal();
    await executeShutdown();
}

async function executeShutdown() {
    try {
        await fetch('/control/shutdown', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ stop_orchestrators: true })
        });
    } catch (err) {
        // Expected - server dies before responding
    }
    document.body.innerHTML = '<div style="display:flex;justify-content:center;align-items:center;height:100vh;color:#8b949e;flex-direction:column;gap:12px;"><span>Orchestrator stopped.</span><span style="font-size:0.9em;">You can close this tab.</span></div>';
}

// Tab switching
function switchTab(tab) {
    const url = new URL(window.location.href);
    url.searchParams.set('tab', tab);
    url.searchParams.set('page', '1');  // Reset to page 1 when switching tabs
    window.location.href = url.toString();
}

// Keyboard navigation for tabs (accessibility)
const tabOrder = ['active', 'queue', 'blocked', 'history', 'e2e'];
document.querySelectorAll('.tab-bar .tab').forEach(tabBtn => {
    tabBtn.addEventListener('keydown', (e) => {
        const currentTab = tabBtn.id.replace('tab-', '');
        const currentIndex = tabOrder.indexOf(currentTab);
        let newIndex = currentIndex;

        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
            e.preventDefault();
            newIndex = (currentIndex + 1) % tabOrder.length;
        } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
            e.preventDefault();
            newIndex = (currentIndex - 1 + tabOrder.length) % tabOrder.length;
        } else if (e.key === 'Home') {
            e.preventDefault();
            newIndex = 0;
        } else if (e.key === 'End') {
            e.preventDefault();
            newIndex = tabOrder.length - 1;
        }

        if (newIndex !== currentIndex) {
            const newTabBtn = document.getElementById('tab-' + tabOrder[newIndex]);
            if (newTabBtn) {
                newTabBtn.focus();
            }
        }

        // Enter or Space activates the tab
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            switchTab(currentTab);
        }
    });
});

// Pagination (preserves current tab)
function goToPage(page) {
    const url = new URL(window.location.href);
    url.searchParams.set('page', page);
    window.location.href = url.toString();
}

// E2E pagination (preserves current tab)
function goToE2EPage(page) {
    const url = new URL(window.location.href);
    url.searchParams.set('e2e_page', page);
    window.location.href = url.toString();
}

// Auto-refresh (preserves page param) - uses queue_refresh_seconds from config
const queueRefreshSeconds = window.dashboardData.queueRefreshSeconds;
if (queueRefreshSeconds > 0) {
    setTimeout(() => {
        window.location.reload();
    }, queueRefreshSeconds * 1000);
}

// Manual refresh function
function refreshPage() {
    window.location.reload();
}

// Dependency problems tracking
let dependencyProblems = {};  // issue_number -> problem info

function updateDependencyWarning(issueNumber, problem) {
    const warningIcon = document.getElementById('dep-warning-' + issueNumber);
    if (warningIcon) {
        if (problem) {
            warningIcon.style.display = 'inline';
            warningIcon.title = problem.summary || 'Dependency problem';
            // Store for context menu
            warningIcon.dataset.problemSummary = problem.summary;
        } else {
            warningIcon.style.display = 'none';
            warningIcon.title = '';
        }
    }
}

function loadDependencyProblems() {
    fetch('/api/dependency-problems')
        .then(response => response.json())
        .then(data => {
            if (data.problems) {
                dependencyProblems = data.problems;
                console.log('[deps] Loaded', Object.keys(dependencyProblems).length, 'dependency problems');
                // Update warning icons for all problems
                for (const [issueNum, problem] of Object.entries(dependencyProblems)) {
                    updateDependencyWarning(issueNum, problem);
                }
            }
        })
        .catch(err => console.error('[deps] Failed to load dependency problems:', err));
}

// Stale in-progress tracking
let staleIssues = {};  // issue_number -> stale info

function updateStaleWarning(issueNumber, staleInfo) {
    const warningIcon = document.getElementById('stale-warning-' + issueNumber);
    if (warningIcon) {
        if (staleInfo) {
            warningIcon.style.display = 'inline';
            const ticks = staleInfo.consecutive_ticks || 1;
            const persistent = staleInfo.persistent;
            warningIcon.title = persistent
                ? `Persistent stale: no session for ${ticks} cycles (needs investigation)`
                : `Stale in-progress: no session running (${ticks} cycle${ticks > 1 ? 's' : ''})`;
            // Add/remove persistent class for red color
            if (persistent) {
                warningIcon.classList.add('persistent');
            } else {
                warningIcon.classList.remove('persistent');
            }
        } else {
            warningIcon.style.display = 'none';
            warningIcon.title = '';
            warningIcon.classList.remove('persistent');
        }
    }
}

function loadStaleIssues() {
    fetch('/api/stale-issues')
        .then(response => response.json())
        .then(data => {
            if (data.stale) {
                staleIssues = data.stale;
                console.log('[stale] Loaded', Object.keys(staleIssues).length, 'stale issues');
                // Update warning icons for all stale issues
                for (const [issueNum, staleInfo] of Object.entries(staleIssues)) {
                    updateStaleWarning(issueNum, staleInfo);
                }
            }
        })
        .catch(err => console.error('[stale] Failed to load stale issues:', err));
}

let excludedLoaded = false;

function renderFlowStepper(steps, activeKey, blockedSummary) {
    if (!steps || steps.length === 0) return '';
    const stepHtml = steps.map(step => {
        const active = step.key === activeKey ? 'active' : '';
        return `<span class="flow-step ${active}" tabindex="0">${escapeHtml(step.label)}</span>`;
    }).join('');
    const blockedBadge = blockedSummary
        ? `<span class="blocked-badge" title="${escapeHtml(blockedSummary)}">Blocked</span>`
        : '';
    const blockedClass = blockedSummary ? 'blocked' : '';
    return `<span class="flow-stepper ${blockedClass}">${stepHtml}${blockedBadge}</span>`;
}

function renderExcludedList(items) {
    const list = document.getElementById('excludedList');
    if (!items || items.length === 0) {
        list.innerHTML = '<div class="empty-state">No excluded issues found</div>';
        return;
    }
    list.innerHTML = items.map(item => `
        <div class="excluded-row">
            <div class="excluded-meta">
                <a href="${item.issue_url}" target="_blank">#${item.issue_number}</a>
                <span class="excluded-reason">${escapeHtml(item.excluded_reason || 'not eligible')}</span>
            </div>
            <div class="issue-title">${escapeHtml(item.title)}</div>
            ${renderFlowStepper(item.flow_steps, item.flow_stage, item.blocked_summary)}
        </div>
    `).join('');
}

async function toggleExcluded() {
    const panel = document.getElementById('excludedPanel');
    const toggle = document.getElementById('excludedToggle');
    const opening = panel.style.display === 'none';
    panel.style.display = opening ? 'block' : 'none';
    toggle.classList.toggle('active', opening);

    if (!opening) return;
    if (!excludedLoaded) {
        try {
            const res = await fetch('/api/excluded-issues');
            const data = await res.json();
            const items = data.excluded || [];
            renderExcludedList(items);
            toggle.textContent = `Excluded (${items.length})`;
            excludedLoaded = true;
        } catch (err) {
            console.error('Failed to fetch excluded issues:', err);
            document.getElementById('excludedList').innerHTML =
                '<div class="empty-state">Failed to load excluded issues</div>';
        }
    }
}

// Server-Sent Events for real-time updates
// Always connect - even during startup - so we can receive startup_complete
// IMPORTANT: Connect first, then fetch initial state on open to avoid race conditions
(function() {
    const startupComplete = window.dashboardData.startupComplete;
    const evtSource = new EventSource('/api/events');
    let reconnectAttempts = 0;
    const maxReconnectAttempts = 5;

    evtSource.onopen = function() {
        console.log('[SSE] Connected to event stream (startup_complete=' + startupComplete + ')');
        reconnectAttempts = 0;
        // Fetch initial state AFTER SSE connected - events arriving after this fetch will layer on top
        loadDependencyProblems();
        loadStaleIssues();
        refreshViewModel({ reloadOnListChange: false });
    };

    // Listen for specific events that should trigger refresh
    // Events use canonical names from events/catalog.py (dot notation)
    // Note: startup_complete is broadcast directly (not via TraceEvent) so keeps underscore
    const refreshEvents = [
        'session.started',     // EventName.SESSION_STARTED
        'session.completed',   // EventName.SESSION_COMPLETED
        'orchestrator.paused', // EventName.ORCHESTRATOR_PAUSED
        'orchestrator.resumed', // EventName.ORCHESTRATOR_RESUMED
        'startup_complete',    // Broadcast directly from web.py (not via TraceEvent)
    ];
    refreshEvents.forEach(eventType => {
        evtSource.addEventListener(eventType, function(e) {
            console.log('[SSE] Received event:', eventType, e.data);
            // Slight delay to let server state settle
            setTimeout(() => refreshViewModel({ reloadOnListChange: true }), 200);
        });
    });

    // Listen for shutdown event (show shutdown message instead of reload)
    evtSource.addEventListener('shutdown_requested', function(e) {
        console.log('[SSE] Shutdown requested:', e.data);
        // Update status badge to show stopping
        const badge = document.querySelector('.status-badge');
        if (badge) {
            badge.textContent = 'Stopping...';
            badge.classList.remove('status-running', 'status-starting');
            badge.classList.add('status-paused');
        }
        // After a brief delay, show shutdown message
        setTimeout(() => {
            document.body.innerHTML = '<div style="display:flex;justify-content:center;align-items:center;height:100vh;flex-direction:column;gap:16px;color:#8b949e;"><div style="font-size:48px;">👋</div><h2 style="color:#fff;">Orchestrator Stopped</h2><p>You can close this tab or wait for it to restart.</p></div>';
        }, 500);
    });

    // Listen for queue changes (reload to update issue list)
    evtSource.addEventListener('queue.changed', function(e) {
        try {
            const data = JSON.parse(e.data);
            console.log('[SSE] Queue changed:', data.added.length, 'added,', data.removed.length, 'removed');
            // Refresh view-model and update list if needed
            setTimeout(() => refreshViewModel({ reloadOnListChange: true }), 200);
        } catch (err) {
            console.error('[SSE] Failed to parse queue.changed:', err);
        }
    });

    // Listen for dependency events (update in-place, no reload)
    evtSource.addEventListener('dependency.blocked', function(e) {
        try {
            const data = JSON.parse(e.data);
            console.log('[SSE] Dependency blocked:', data);
            dependencyProblems[data.issue_number] = data;
            updateDependencyWarning(data.issue_number, data);
        } catch (err) {
            console.error('[SSE] Failed to parse dependency.blocked:', err);
        }
    });

    evtSource.addEventListener('dependency.unblocked', function(e) {
        try {
            const data = JSON.parse(e.data);
            console.log('[SSE] Dependency unblocked:', data);
            delete dependencyProblems[data.issue_number];
            updateDependencyWarning(data.issue_number, null);
        } catch (err) {
            console.error('[SSE] Failed to parse dependency.unblocked:', err);
        }
    });

    // Listen for stale in-progress events
    evtSource.addEventListener('stale.in_progress_detected', function(e) {
        try {
            const data = JSON.parse(e.data);
            console.log('[SSE] Stale in-progress detected:', data);
            // Update or add to stale tracking
            staleIssues[data.issue_number] = {
                issue_number: data.issue_number,
                consecutive_ticks: 1,
                persistent: false,
            };
            updateStaleWarning(data.issue_number, staleIssues[data.issue_number]);
        } catch (err) {
            console.error('[SSE] Failed to parse stale.in_progress_detected:', err);
        }
    });

    evtSource.addEventListener('stale.in_progress_cleared', function(e) {
        try {
            const data = JSON.parse(e.data);
            console.log('[SSE] Stale in-progress cleared:', data);
            delete staleIssues[data.issue_number];
            updateStaleWarning(data.issue_number, null);
        } catch (err) {
            console.error('[SSE] Failed to parse stale.in_progress_cleared:', err);
        }
    });

    evtSource.addEventListener('stale.persistent_detected', function(e) {
        try {
            const data = JSON.parse(e.data);
            console.log('[SSE] Persistent stale detected:', data);
            // Update to persistent state
            staleIssues[data.issue_number] = {
                issue_number: data.issue_number,
                consecutive_ticks: data.consecutive_ticks,
                persistent: true,
                threshold: data.threshold,
            };
            updateStaleWarning(data.issue_number, staleIssues[data.issue_number]);
        } catch (err) {
            console.error('[SSE] Failed to parse stale.persistent_detected:', err);
        }
    });

    // NOTE: E2E progress is currently polled via updateE2EProgress().
    // When the backend emits E2E SSE events (e2e.started, e2e.progress, etc.),
    // add listeners here to reduce polling frequency.

    evtSource.onerror = function(e) {
        console.log('[SSE] Connection error, will reconnect automatically');
        reconnectAttempts++;
        if (reconnectAttempts >= maxReconnectAttempts) {
            console.log('[SSE] Max reconnect attempts reached, closing');
            evtSource.close();
        }
    };
})();

// Helper to add keyboard support to menu items
function addKeyboardSupport(element) {
    element.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            element.click();
        }
    });
}

// Context menu
const contextMenu = document.getElementById('contextMenu');
const menuFocus = document.getElementById('menuFocus');
const menuFinder = document.getElementById('menuFinder');
const menuLog = document.getElementById('menuLog');
const menuAgentLog = document.getElementById('menuAgentLog');
const menuInput = document.getElementById('menuInput');
const menuPrompt = document.getElementById('menuPrompt');
const menuKill = document.getElementById('menuKill');
const menuIssue = document.getElementById('menuIssue');
const menuPR = document.getElementById('menuPR');
const menuRetry = document.getElementById('menuRetry');
const menuDismiss = document.getElementById('menuDismiss');
const menuHistoryDivider = document.getElementById('menuHistoryDivider');
const menuDepsDivider = document.getElementById('menuDepsDivider');
const menuDepsLabel = document.getElementById('menuDepsLabel');
const menuDepsContainer = document.getElementById('menuDepsContainer');
let currentRow = null;

// Add keyboard support to all context menu items
[menuFocus, menuFinder, menuLog, menuAgentLog, menuInput, menuPrompt, menuKill, menuIssue, menuPR, menuRetry, menuDismiss].forEach(addKeyboardSupport);

function showContextMenu(e, row) {
    e.preventDefault();
    currentRow = row;

    const issueNumber = row.dataset.issue;
    const action = row.dataset.action;
    const prUrl = row.dataset.prUrl;
    const status = row.dataset.status;
    const hasDeps = row.dataset.hasDependencies === 'true';

    // Enable/disable menu items based on context
    // Active sessions (action === 'focus') have terminal session and worktree
    if (action === 'focus' && terminalBackend !== 'subprocess') {
        menuFocus.classList.remove('disabled');
        menuFinder.classList.remove('disabled');
    } else {
        menuFocus.classList.add('disabled');
        menuFinder.classList.add('disabled');
    }

    if (prUrl) {
        menuPR.classList.remove('disabled');
    } else {
        menuPR.classList.add('disabled');
    }

    // Agent prompt is always available if we have an agent type
    const agentType = row.dataset.agent;
    if (agentType) {
        menuPrompt.classList.remove('disabled');
    } else {
        menuPrompt.classList.add('disabled');
    }

    // Show Kill option for any session with a terminal (active sessions)
    const hasTerminal = row.dataset.hasTerminal === 'true';
    if (hasTerminal) {
        menuKill.style.display = 'flex';
        menuInput.classList.remove('disabled');
    } else {
        menuKill.style.display = 'none';
        menuInput.classList.add('disabled');
    }

    // Show dependencies section if this issue has dependencies
    menuDepsContainer.innerHTML = '';  // Clear previous
    if (hasDeps) {
        try {
            const deps = JSON.parse(row.dataset.dependencies || '[]');
            if (deps.length > 0) {
                menuDepsDivider.style.display = 'block';
                menuDepsLabel.style.display = 'flex';
                deps.forEach(dep => {
                    const item = document.createElement('div');
                    item.className = 'context-menu-item dep-item';
                    item.innerHTML = `<span class="dep-num">#${dep.number}</span><span class="dep-title" title="${dep.title}">${dep.title}</span>`;
                    item.onclick = (e) => {
                        e.stopPropagation();
                        contextMenu.classList.remove('visible');
                        window.open(`https://github.com/${window.dashboardData.repo}/issues/${dep.number}`, '_blank');
                    };
                    menuDepsContainer.appendChild(item);
                });
            } else {
                menuDepsDivider.style.display = 'none';
                menuDepsLabel.style.display = 'none';
            }
        } catch (err) {
            console.error('Failed to parse dependencies:', err);
            menuDepsDivider.style.display = 'none';
            menuDepsLabel.style.display = 'none';
        }
    } else {
        menuDepsDivider.style.display = 'none';
        menuDepsLabel.style.display = 'none';
    }

    // Show Retry/Dismiss for history items (failed, blocked, completed, timed_out)
    const historyStatuses = ['failed', 'blocked', 'completed', 'timed_out', 'needs_human'];
    if (historyStatuses.includes(status)) {
        menuHistoryDivider.style.display = 'block';
        menuRetry.style.display = 'flex';
        menuDismiss.style.display = 'flex';
    } else {
        menuHistoryDivider.style.display = 'none';
        menuRetry.style.display = 'none';
        menuDismiss.style.display = 'none';
    }

    // Position menu
    contextMenu.style.left = e.pageX + 'px';
    contextMenu.style.top = e.pageY + 'px';
    contextMenu.classList.add('visible');
}

// Hide context menu on click elsewhere
document.addEventListener('click', () => {
    contextMenu.classList.remove('visible');
});

// Menu actions - must stopPropagation to prevent document click handler from interfering
menuFocus.addEventListener('click', async (e) => {
    e.stopPropagation();
    contextMenu.classList.remove('visible');
    if (currentRow && !menuFocus.classList.contains('disabled')) {
        await fetch(`/api/focus/${currentRow.dataset.issue}`, { method: 'POST' });
    }
});

menuFinder.addEventListener('click', async (e) => {
    e.stopPropagation();
    contextMenu.classList.remove('visible');
    if (currentRow && !menuFinder.classList.contains('disabled')) {
        const res = await fetch(`/api/finder/${currentRow.dataset.issue}`, { method: 'POST' });
        const data = await res.json();
        console.log('Finder response:', data);
    }
});

menuLog.addEventListener('click', async (e) => {
    e.stopPropagation();
    contextMenu.classList.remove('visible');
    if (currentRow) {
        const issueNumber = currentRow.dataset.issue;
        const res = await fetch(`/api/log/${issueNumber}`);
        const data = await res.json();

        if (data.error) {
            alert(data.error + (data.hint ? '\n\n' + data.hint : ''));
            return;
        }

        // Format log content for display
        let logContent = '';
        if (data.truncated) {
            logContent += `<p style="color:#8b949e;margin-bottom:10px;">Showing last ${data.lines.length} of ${data.total_lines} entries...</p>`;
        }
        logContent += '<pre style="font-size:11px;max-height:400px;overflow:auto;background:#161b22;padding:10px;border-radius:4px;">';
        for (const line of data.lines) {
            try {
                const entry = JSON.parse(line);
                // Show role and truncated content
                const role = entry.type || entry.role || 'unknown';
                // Safely extract content - may be string or object
                let rawContent = entry.message?.content || entry.content || entry;
                const content = (typeof rawContent === 'string' ? rawContent : JSON.stringify(rawContent)).substring(0, 200);
                logContent += `<span style="color:#58a6ff;">[${role}]</span> ${content.replace(/</g, '&lt;').replace(/>/g, '&gt;')}...\n`;
            } catch {
                logContent += line.substring(0, 200).replace(/</g, '&lt;').replace(/>/g, '&gt;') + '\n';
            }
        }
        logContent += '</pre>';
        logContent += `<p style="color:#8b949e;font-size:11px;margin-top:10px;">Log: ${data.log_path}</p>`;

        // Show in modal
        document.getElementById('modalTitle').textContent = `Session Log #${issueNumber}`;
        document.getElementById('modalBody').innerHTML = logContent;
        document.getElementById('modalOverlay').classList.add('visible');
    }
});

menuAgentLog.addEventListener('click', async (e) => {
    e.stopPropagation();
    contextMenu.classList.remove('visible');
    if (currentRow) {
        const issueNumber = currentRow.dataset.issue;
        await openAgentLog(issueNumber);
    }
});

menuInput.addEventListener('click', async (e) => {
    e.stopPropagation();
    contextMenu.classList.remove('visible');
    if (currentRow && !menuInput.classList.contains('disabled')) {
        const issueNumber = currentRow.dataset.issue;
        openModal(
            `Send Input #${issueNumber}`,
            `
                <div style="display:flex;flex-direction:column;gap:12px;">
                    <label for="agentInput" class="form-label">Input</label>
                    <textarea id="agentInput" class="form-textarea" rows="6" placeholder="Type a message or command (e.g., /exit)"></textarea>
                    <div style="display:flex;justify-content:flex-end;gap:8px;">
                        <button class="btn-secondary" onclick="closeModal()">Cancel</button>
                        <button class="btn-primary" onclick="sendAgentInput(${issueNumber})">Send</button>
                    </div>
                </div>
            `
        );
    }
});

menuPrompt.addEventListener('click', async (e) => {
    e.stopPropagation();
    contextMenu.classList.remove('visible');
    if (currentRow && !menuPrompt.classList.contains('disabled')) {
        const agentType = currentRow.dataset.agent;
        const res = await fetch(`/api/prompt/${encodeURIComponent(agentType)}`, { method: 'POST' });
        const data = await res.json();
        console.log('Prompt response:', data);
    }
});

menuKill.addEventListener('click', async (e) => {
    e.stopPropagation();
    contextMenu.classList.remove('visible');
    if (currentRow) {
        const issueNumber = currentRow.dataset.issue;
        const title = currentRow.dataset.title;
        if (confirm(`Kill session #${issueNumber}: ${title}?\n\nThis will terminate the Claude agent immediately.`)) {
            const res = await fetch(`/api/kill/${issueNumber}`, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'killed') {
                location.reload();
            } else {
                alert(data.error || 'Failed to kill session');
            }
        }
    }
});

menuIssue.addEventListener('click', (e) => {
    e.stopPropagation();
    contextMenu.classList.remove('visible');
    if (currentRow) {
        window.open(currentRow.dataset.issueUrl, '_blank');
    }
});

menuPR.addEventListener('click', (e) => {
    e.stopPropagation();
    contextMenu.classList.remove('visible');
    if (currentRow && currentRow.dataset.prUrl) {
        window.open(currentRow.dataset.prUrl, '_blank');
    }
});

menuRetry.addEventListener('click', async (e) => {
    e.stopPropagation();
    contextMenu.classList.remove('visible');
    if (currentRow) {
        const issueNumber = currentRow.dataset.issue;
        if (confirm(`Retry issue #${issueNumber}? It will be picked up on the next cycle.`)) {
            const res = await fetch(`/api/retry/${issueNumber}`, { method: 'POST' });
            const data = await res.json();
            if (data.retrying) {
                location.reload();
            } else {
                alert(data.error || 'Failed to retry issue');
            }
        }
    }
});

menuDismiss.addEventListener('click', async (e) => {
    e.stopPropagation();
    contextMenu.classList.remove('visible');
    if (currentRow) {
        const issueNumber = currentRow.dataset.issue;
        const res = await fetch(`/api/history/dismiss/${issueNumber}`, { method: 'POST' });
        const data = await res.json();
        if (data.dismissed) {
            location.reload();
        } else {
            alert(data.error || 'Failed to dismiss');
        }
    }
});

// Settings menu
const settingsMenu = document.getElementById('settingsMenu');

function toggleSettingsMenu(e) {
    e.stopPropagation();
    settingsMenu.classList.toggle('visible');
}

document.addEventListener('click', () => {
    settingsMenu.classList.remove('visible');
});

// Modal
const modalOverlay = document.getElementById('modalOverlay');
const modalTitle = document.getElementById('modalTitle');
const modalBody = document.getElementById('modalBody');

function openModal(title, content) {
    modalTitle.textContent = title;
    modalBody.innerHTML = content;
    modalOverlay.classList.add('visible');
}

function closeModal(e) {
    if (!e || e.target === modalOverlay) {
        modalOverlay.classList.remove('visible');
        // Reset modal classes for viewers
        const modalEl = modalOverlay.querySelector('.modal');
        modalEl.classList.remove('log-viewer-modal', 'live-log-modal');
        if (logPoller) {
            clearInterval(logPoller);
            logPoller = null;
        }
        // Stop live log poller if running
        stopLiveLogPoller();
    }
}

// Blocked Issues Modal Functions
let blockedIssuesData = [];
const blockedModal = document.getElementById('blockedModal');
const blockedList = document.getElementById('blockedList');
const blockedSelectAll = document.getElementById('blockedSelectAll');
const blockedSelectAllLabel = document.getElementById('blockedSelectAllLabel');
const blockedWarning = document.getElementById('blockedWarning');
const blockedWarningText = document.getElementById('blockedWarningText');
const blockedUnblockBtn = document.getElementById('blockedUnblockBtn');
const blockedResetBtn = document.getElementById('blockedResetBtn');

async function openBlockedModal() {
    // Fetch blocked issues
    try {
        const res = await fetch('/api/dialog/blocked-issues');
        const data = await res.json();
        blockedIssuesData = data.blocked_issues || [];
    } catch (err) {
        console.error('Failed to fetch blocked issues:', err);
        blockedIssuesData = [];
    }

    renderBlockedList();
    blockedModal.classList.add('visible');
}

function closeBlockedModal(e) {
    if (!e || e.target === blockedModal) {
        blockedModal.classList.remove('visible');
    }
}

// Phase Info Modal
const phaseModal = document.getElementById('phaseModal');
let currentPhaseData = null;
let currentPhaseIssue = null;

async function openPhaseModal(issueNumber, flowStepKey) {
    currentPhaseIssue = issueNumber;
    try {
        const res = await fetch(`/api/dialog/phase/${issueNumber}?phase=${encodeURIComponent(flowStepKey)}`);
        const data = await res.json();

        if (data.error) {
            console.error('Failed to fetch phases:', data.error);
            return;
        }

        const phase = data.phase;

        if (!phase) {
            // No phases yet, show a simple message
            document.getElementById('phaseModalTitle').textContent = flowStepKey.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
            document.getElementById('phaseStatusIcon').textContent = '○';
            document.getElementById('phaseStatusIcon').className = 'phase-status-icon';
            document.getElementById('phaseStatusLabel').textContent = 'Not started';
            document.getElementById('phaseDuration').textContent = '-';
            document.getElementById('phaseAgent').textContent = '-';
            document.getElementById('phaseValidationRow').style.display = 'none';
            document.getElementById('phaseDetailsBtn').style.display = 'none';
            phaseModal.classList.add('visible');
            return;
        }

        currentPhaseData = phase;

        // Update modal content
        document.getElementById('phaseModalTitle').textContent = phase.display_name;

        const iconEl = document.getElementById('phaseStatusIcon');
        const labelEl = document.getElementById('phaseStatusLabel');

        iconEl.textContent = phase.status_icon;
        iconEl.className = 'phase-status-icon ' + getStatusClass(phase.status);
        labelEl.textContent = formatStatus(phase.status);

        // Duration
        const duration = calculateDuration(phase.started_at, phase.ended_at);
        document.getElementById('phaseDuration').textContent = duration || '-';

        // Agent
        document.getElementById('phaseAgent').textContent = phase.agent_label || '-';

        // Validation
        const validationRow = document.getElementById('phaseValidationRow');
        if (phase.validation_passed !== null && phase.validation_passed !== undefined) {
            validationRow.style.display = 'flex';
            document.getElementById('phaseValidation').textContent =
                phase.validation_passed ? 'Passed' : 'Failed';
            document.getElementById('phaseValidation').style.color =
                phase.validation_passed ? '#3fb950' : '#f85149';
        } else {
            validationRow.style.display = 'none';
        }

        // Show Details button
        document.getElementById('phaseDetailsBtn').style.display = 'block';

        phaseModal.classList.add('visible');
    } catch (err) {
        console.error('Error fetching phase data:', err);
    }
}

function closePhaseModal(e) {
    if (!e || e.target === phaseModal) {
        phaseModal.classList.remove('visible');
        currentPhaseData = null;
    }
}

const timelineModal = document.getElementById('timelineModal');

async function openTimelineModal(issueNumber) {
    if (!timelineModal) return;
    timelineModal.classList.add('visible');
    document.getElementById('timelineModalTitle').textContent = `Timeline #${issueNumber}`;
    const content = document.getElementById('timelineModalContent');
    content.innerHTML = '<div class="timeline-loading">Loading timeline...</div>';

    try {
        const res = await fetch(`/api/timeline/${issueNumber}`);
        if (!res.ok) {
            content.innerHTML = '<div class="timeline-empty">No timeline data found.</div>';
            return;
        }
        const data = await res.json();
        renderTimeline(content, data.events || []);
    } catch (err) {
        console.error('Failed to load timeline:', err);
        content.innerHTML = '<div class="timeline-empty">Failed to load timeline.</div>';
    }
}

function closeTimelineModal(e) {
    if (!e || e.target === timelineModal) {
        timelineModal.classList.remove('visible');
    }
}

function renderTimeline(container, events) {
    if (!events || events.length === 0) {
        container.innerHTML = '<div class="timeline-empty">No timeline events recorded yet.</div>';
        return;
    }

    const groups = [];
    for (const event of events) {
        const phase = event.phase || 'system';
        let group = groups[groups.length - 1];
        if (!group || group.phase !== phase) {
            group = { phase, events: [] };
            groups.push(group);
        }
        group.events.push(event);
    }

    const html = groups.map(group => {
        const phaseLabel = formatPhaseLabel(group.phase);
        const items = group.events.map(evt => {
            const stepLabel = formatStepLabel(evt.step);
            const summary = evt.summary ? `<div class="timeline-summary">${escapeHtml(evt.summary)}</div>` : '';
            const time = evt.timestamp ? `<div class="timeline-time">${formatTimestamp(evt.timestamp)}</div>` : '';
            const artifacts = renderTimelineArtifacts(evt.artifacts || []);
            return `
                <div class="timeline-event ${evt.status || ''}">
                    <div class="timeline-event-header">
                        <span class="timeline-step">${escapeHtml(stepLabel)}</span>
                        <span class="timeline-status">${formatStatus(evt.status)}</span>
                    </div>
                    ${time}
                    ${summary}
                    ${artifacts}
                </div>
            `;
        }).join('');
        return `
            <div class="timeline-group">
                <div class="timeline-group-header">${escapeHtml(phaseLabel)}</div>
                <div class="timeline-group-body">${items}</div>
            </div>
        `;
    }).join('');

    container.innerHTML = html;
}

function renderTimelineArtifacts(artifacts) {
    if (!artifacts || artifacts.length === 0) return '';
    const items = artifacts.map(artifact => {
        const label = escapeHtml(artifact.label || artifact.type || 'Artifact');
        const value = artifact.value || '';
        if (value.startsWith('http://') || value.startsWith('https://')) {
            return `<a class="timeline-artifact" href="${escapeAttr(value)}" target="_blank">${label}</a>`;
        }
        return `<button class="timeline-artifact" onclick="openPath('${escapeHtml(value)}')">${label}</button>`;
    }).join('');
    return `<div class="timeline-artifacts">${items}</div>`;
}

function formatPhaseLabel(phase) {
    return phase.replace(/_/g, ' ').replace(/\\b\\w/g, c => c.toUpperCase());
}

function formatStepLabel(step) {
    return step.replace(/_/g, ' ').replace(/\\b\\w/g, c => c.toUpperCase());
}

function formatTimestamp(timestamp) {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return timestamp;
    return date.toLocaleString();
}

function openPhaseDetails() {
    if (currentPhaseData && currentPhaseData.run_dir) {
        // Open the session manifest modal with this specific run
        openSessionManifest(currentPhaseIssue);
        closePhaseModal();
    }
}

function getStatusClass(status) {
    if (status === 'completed') return 'success';
    if (status === 'in_progress') return 'in-progress';
    if (['validation_failed', 'blocked', 'timeout'].includes(status)) return 'failed';
    return '';
}

function formatStatus(status) {
    const labels = {
        'completed': 'Completed',
        'in_progress': 'In Progress',
        'validation_failed': 'Validation Failed',
        'blocked': 'Blocked',
        'timeout': 'Timed Out',
    };
    return labels[status] || status.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function calculateDuration(startedAt, endedAt) {
    if (!startedAt) return null;
    const start = new Date(startedAt);
    const end = endedAt ? new Date(endedAt) : new Date();
    const diffMs = end - start;
    const diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 60) return `${diffMins}m`;
    const hours = Math.floor(diffMins / 60);
    const mins = diffMins % 60;
    return `${hours}h ${mins}m`;
}

function handlePhaseClick(e, issueNumber, phaseName) {
    e.stopPropagation();
    openPhaseModal(issueNumber, phaseName);
}

function renderBlockedList() {
    if (blockedIssuesData.length === 0) {
        blockedList.innerHTML = '<div class="blocked-empty">No blocked issues found</div>';
        blockedSelectAll.checked = false;
        blockedSelectAll.disabled = true;
        updateBlockedButton();
        return;
    }

    blockedSelectAll.disabled = false;
    const needsHumanCount = blockedIssuesData.filter(i => i.needs_human).length;

    let html = '';
    for (const issue of blockedIssuesData) {
        const labelClass = issue.needs_human ? 'needs-human' : '';
        const reason = issue.failure_reason || issue.blocking_label;
        const hasWorktree = !!issue.worktree_path;
        const hasCompletion = issue.has_completion;
        html += `
            <div class="blocked-item-container" id="blocked-container-${issue.issue_number}">
                <div class="blocked-item">
                    <input type="checkbox"
                           id="blocked-${issue.issue_number}"
                           data-issue="${issue.issue_number}"
                           data-needs-human="${issue.needs_human}"
                           ${issue.needs_human ? '' : 'checked'}
                           onchange="updateBlockedSelection()">
                    <div class="blocked-item-content">
                        <div class="blocked-item-header">
                            <a href="${issue.issue_url}" target="_blank" class="blocked-item-num">#${issue.issue_number}</a>
                            <span class="blocked-item-label ${labelClass}">${issue.blocking_label}</span>
                        </div>
                        <div class="blocked-item-title">${escapeHtml(issue.title)}</div>
                        ${reason ? `<div class="blocked-item-reason">${escapeHtml(reason)}</div>` : ''}
                        ${hasWorktree ? `<div class="blocked-item-worktree">${escapeHtml(issue.worktree_path)}</div>` : ''}
                    </div>
                    <div class="blocked-item-actions">
                        ${hasWorktree ? `<button class="copy-path-btn" onclick="copyWorktreePath('${escapeHtml(issue.worktree_path)}', event)" title="Copy worktree path">Copy Path</button>` : ''}
                        ${hasWorktree ? `<button class="debug-btn" onclick="launchDebugSession(${issue.issue_number}, event)" title="Launch interactive debug session">Launch Debug</button>` : ''}
                        ${hasCompletion ? `<button class="resume-btn" onclick="resumeIssue(${issue.issue_number}, event)" title="Process completion and continue flow">Resume</button>` : ''}
                        <button class="diagnose-btn" onclick="toggleDiagnosis(${issue.issue_number}, event)">Diagnose</button>
                    </div>
                </div>
                <div class="diagnosis-panel" id="diagnosis-${issue.issue_number}">
                    <div class="diagnosis-loading">Loading diagnosis...</div>
                </div>
            </div>
        `;
    }
    blockedList.innerHTML = html;

    // Show warning if there are needs-human issues
    if (needsHumanCount > 0) {
        blockedWarning.style.display = 'flex';
        blockedWarningText.textContent = `${needsHumanCount} issue${needsHumanCount > 1 ? 's' : ''} marked 'needs-human' - review before retrying`;
    } else {
        blockedWarning.style.display = 'none';
    }

    updateBlockedSelection();
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

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
    let confirmMsg = `Unblock and retry ${issueNumbers.length} issue${issueNumbers.length > 1 ? 's' : ''}?`;
    if (needsHumanSelected > 0) {
        confirmMsg += `\n\n⚠️ ${needsHumanSelected} issue${needsHumanSelected > 1 ? 's have' : ' has'} 'needs-human' label - make sure you've addressed the concern.`;
    }
    if (!confirm(confirmMsg)) return;

    // Disable button during request
    blockedUnblockBtn.disabled = true;
    blockedUnblockBtn.textContent = 'Unblocking...';

    try {
        const res = await fetch('/api/unblock-retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ issues: issueNumbers }),
        });
        const data = await res.json();

        if (data.unblocked && data.unblocked.length > 0) {
            showToast(`Unblocked ${data.unblocked.length} issue${data.unblocked.length > 1 ? 's' : ''}`);
            closeBlockedModal();
            // Reload to show updated state
            setTimeout(() => location.reload(), 500);
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
    const confirmMsg = `⚠️ Reset and retry ${issueNumbers.length} issue${issueNumbers.length > 1 ? 's' : ''}?\n\nThis will DELETE:\n• Local worktrees\n• Remote branches\n• Blocking labels\n\nIssues will return to available state for a fresh retry.`;
    if (!confirm(confirmMsg)) return;

    // Disable buttons during request
    blockedResetBtn.disabled = true;
    blockedResetBtn.textContent = 'Resetting...';
    blockedUnblockBtn.disabled = true;

    try {
        const res = await fetch('/api/reset-retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ issues: issueNumbers }),
        });
        const data = await res.json();

        if (data.reset && data.reset.length > 0) {
            showToast(`Reset ${data.reset.length} issue${data.reset.length > 1 ? 's' : ''} - ready for fresh retry`);
            closeBlockedModal();
            // Reload to show updated state
            setTimeout(() => location.reload(), 500);
        } else if (data.failed && data.failed.length > 0) {
            showToast(`Failed to reset some issues: ${data.failed.map(f => f.error).join(', ')}`, 'error');
        }
    } catch (err) {
        console.error('Failed to reset issues:', err);
        showToast('Failed to reset issues', 'error');
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

// Resume processing for a blocked issue with completion.json
async function resumeIssue(issueNumber, event) {
    event.stopPropagation();
    const btn = event.target;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Resuming...';

    try {
        const res = await fetch(`/api/issues/${issueNumber}/resume`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
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
            showToast(`Debug session launched for #${issueNumber}. Use 'agent-done --resume' when done.`);
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
        const res = await fetch(`/api/issues/${issueNumber}/retry`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await res.json();

        if (data.success) {
            showToast(`Issue #${issueNumber} queued for retry`);
            setTimeout(() => location.reload(), 500);
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

// Dismiss a blocked issue (removes from blocked list without retrying)
async function dismissIssue(issueNumber, event) {
    event.stopPropagation();
    if (!confirm(`Dismiss issue #${issueNumber}? This will remove the blocked label but not retry.`)) {
        return;
    }

    const btn = event.target.closest('.issue-action-btn') || event.target;
    const originalHTML = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '⏳';

    try {
        const res = await fetch(`/api/issues/${issueNumber}/dismiss`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await res.json();

        if (data.success) {
            showToast(`Issue #${issueNumber} dismissed`);
            setTimeout(() => location.reload(), 500);
        } else {
            showToast(`Failed to dismiss: ${data.error || 'Unknown error'}`, 'error');
            btn.disabled = false;
            btn.innerHTML = originalHTML;
        }
    } catch (err) {
        console.error('Failed to dismiss issue:', err);
        showToast('Failed to dismiss issue', 'error');
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
        <button class="open-log-btn" onclick="openAgentLog(${issueNumber})">
            View Session Log
        </button>
    `;

    html += '</div>';
    panel.innerHTML = html;
}

function openLogFile(path) {
    // Use macOS 'open' command via a simple API call
    fetch('/api/open-file', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path }),
    }).then(res => res.json()).then(data => {
        if (data.error) {
            showToast(`Could not open file: ${data.error}`, 'error');
        } else {
            showToast('Opening log file...');
        }
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

async function openFilteredOrchestratorLog(issueNumber) {
    try {
        showToast('Generating filtered log...');
        const res = await fetch(`/api/session/orchestrator-log/${issueNumber}`);
        const data = await res.json();
        if (data.error) {
            // Fall back to full log if available
            if (data.full_log_path) {
                showToast('Could not filter log, opening full log', 'error');
                openPath(data.full_log_path);
            } else {
                showToast(data.error, 'error');
            }
            return;
        }
        openPath(data.filtered_log_path);
    } catch (err) {
        showToast(`Failed to get orchestrator log: ${err.message}`, 'error');
    }
}

// Claude Log Viewer
async function viewClaudeLog(issueNumber) {
    try {
        showToast('Loading Claude log...');
        const res = await fetch(`/api/session/claude-log/${issueNumber}?limit=500`);
        const data = await res.json();
        if (data.error) {
            showToast(data.error, 'error');
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
    openModal(`Claude Log #${data.issue_number}`, html);
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

function stopLiveLogPoller() {
    if (liveLogPoller) {
        clearInterval(liveLogPoller);
        liveLogPoller = null;
    }
    liveLogIssue = null;
}

async function showInfo() {
    settingsMenu.classList.remove('visible');
    const res = await fetch('/api/dialog/info');
    const data = await res.json();
    const html = (data.rows || []).map(row => `
        <div class="info-row"><span class="info-label">${escapeHtml(row.label)}</span><span class="info-value">${escapeHtml(String(row.value))}</span></div>
    `).join('');
    openModal(data.title || 'About Issue Orchestrator', html);
}

async function showConfig() {
    settingsMenu.classList.remove('visible');
    const res = await fetch('/api/dialog/config');
    const data = await res.json();
    openModal(data.title || 'Configuration', `<pre>${escapeHtml(data.config_text || '')}</pre>`);
}

async function showDebug() {
    settingsMenu.classList.remove('visible');
    const res = await fetch('/api/dialog/debug');
    const data = await res.json();
    let html = '';
    for (const section of data.sections || []) {
        html += `<h3 style="margin: 0 0 8px; font-size: 14px;">${escapeHtml(section.title)}</h3>`;
        html += (section.rows || []).map(row => `
            <div class="info-row"><span class="info-label">${escapeHtml(row.label)}</span><span class="info-value">${escapeHtml(String(row.value))}</span></div>
        `).join('');
        html += '<div style="height: 12px;"></div>';
    }
    openModal(data.title || 'Debug Info', html);
}

async function showDoctor() {
    settingsMenu.classList.remove('visible');
    const res = await fetch('/api/dialog/doctor');
    const data = await res.json();

    const statusIcon = {
        'ok': '✅',
        'warning': '⚠️',
        'error': '❌'
    };

    const html = `
        <div style="margin-bottom: 12px;">
            <span style="font-size: 18px;">${statusIcon[data.overall]}</span>
            <span style="font-weight: 600; margin-left: 8px;">Overall: ${String(data.overall || '').toUpperCase()}</span>
        </div>
        ${(data.checks || []).map(c => `
            <div class="info-row">
                <span class="info-label">${statusIcon[c.status]} ${escapeHtml(c.name || '')}</span>
                <span class="info-value" style="font-size: 12px;">${escapeHtml(c.detail || '')}</span>
            </div>
        `).join('')}
    `;
    openModal(data.title || 'Doctor', html);
}

function openRepo() {
    settingsMenu.classList.remove('visible');
    window.open(`https://github.com/${window.dashboardData.repo}`, '_blank');
}

function formatRelativeMillis(deltaMs) {
    if (deltaMs <= 0) return 'now';
    const totalMinutes = Math.round(deltaMs / 60000);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours > 0 && minutes > 0) return `${hours}h ${minutes}m`;
    if (hours > 0) return `${hours}h`;
    return `${minutes}m`;
}

function formatNextRun(nextInfo) {
    if (!nextInfo) return '';
    const reason = nextInfo.next_run_reason;
    if (reason === 'interval' && nextInfo.next_run_at) {
        const when = new Date(nextInfo.next_run_at);
        const deltaMs = when.getTime() - Date.now();
        const timeStr = when.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
        return `Next: in ${formatRelativeMillis(deltaMs)} (at ${timeStr})`;
    }
    if (reason === 'main_unchanged') return 'Next: waiting for main change';
    if (reason === 'ready') return 'Next: pending';
    if (reason === 'auto_disabled') return 'Auto: off';
    if (reason === 'running') return '';
    return '';
}

async function clearHistory() {
    settingsMenu.classList.remove('visible');
    if (!confirm('Clear all session history (completed, failed, etc.)?')) return;
    const res = await fetch('/api/history/clear', { method: 'POST' });
    const data = await res.json();
    if (data.cleared !== undefined) {
        openModal('History Cleared', `<p>Cleared ${data.cleared} history entries.</p>`);
        setTimeout(() => location.reload(), 1500);
    } else {
        alert(data.error || 'Failed to clear history');
    }
}

// Toast notification
function showToast(message, isError = false) {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle('error', isError);
    toast.classList.add('visible');
    setTimeout(() => toast.classList.remove('visible'), 2500);
}

// Create Issue Modal Functions
async function loadMilestones() {
    const select = document.getElementById('issueMilestone');
    if (!select) return;

    try {
        const res = await fetch('/api/milestones');
        const data = await res.json();

        if (data.error) {
            console.error('[milestones] Error:', data.error);
            select.innerHTML = '<option value="">No milestone</option>';
            return;
        }

        // Clear and add default option
        select.innerHTML = '<option value="">No milestone</option>';

        // Separate included and excluded milestones
        const included = (data.milestones || []).filter(m => m.included);
        const excluded = (data.milestones || []).filter(m => !m.included);

        // Add included milestones first
        for (const m of included) {
            const option = document.createElement('option');
            option.value = m.number;
            option.textContent = m.title;
            select.appendChild(option);
        }

        // Add excluded milestones with clear warning
        if (excluded.length > 0 && data.filter_active) {
            const separator = document.createElement('option');
            separator.disabled = true;
            separator.textContent = '── Won\'t be picked up by this orchestrator ──';
            select.appendChild(separator);

            for (const m of excluded) {
                const option = document.createElement('option');
                option.value = m.number;
                option.textContent = `${m.title}`;
                option.style.color = '#8b949e';
                select.appendChild(option);
            }
        }

        // Update hint
        const hint = document.getElementById('milestoneHint');
        if (hint) {
            if (data.filter_active) {
                hint.textContent = `Orchestrator filters to: ${data.filter_milestones.join(', ')}`;
            } else {
                hint.textContent = '';
            }
        }
    } catch (err) {
        console.error('[milestones] Failed to load:', err);
        select.innerHTML = '<option value="">Failed to load milestones</option>';
    }
}

async function createIssue() {
    const title = document.getElementById('issueTitle').value.trim();
    const body = document.getElementById('issueBody').value.trim();
    const agent = document.getElementById('issueAgent').value;
    const priority = document.getElementById('issuePriority').value;
    const milestone = document.getElementById('issueMilestone').value;
    const refreshAfter = document.getElementById('refreshAfterCreate').checked;

    if (!title) {
        showToast('Title is required', true);
        return;
    }

    if (!agent) {
        showToast('Please select an agent', true);
        return;
    }

    showToast('Creating issue...');

    try {
        const res = await fetch('/api/issues', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title: title,
                body: body,
                agent: agent,
                priority: priority || undefined,
                milestone: milestone ? parseInt(milestone) : undefined,
            }),
        });
        const data = await res.json();

        if (data.error) {
            showToast(data.error, true);
            return;
        }

        showToast(`Issue #${data.issue_number} created!`);

        // Clear the form
        document.getElementById('issueTitle').value = '';
        document.getElementById('issueBody').value = '';
        document.getElementById('issueAgent').selectedIndex = 0;
        document.getElementById('issuePriority').selectedIndex = 0;
        document.getElementById('issueMilestone').selectedIndex = 0;

        // Trigger refresh if requested
        if (refreshAfter) {
            try {
                await fetch('/api/refresh', { method: 'POST' });
                showToast('Queue refreshed');
            } catch (err) {
                // Ignore refresh errors
            }
        }

        // Open the issue in a new tab
        if (data.url) {
            window.open(data.url, '_blank');
        }

    } catch (err) {
        showToast('Failed to create issue', true);
    }
}

// Create Issue Modal Functions
function openCreateIssueModal() {
    document.getElementById('createIssueModal').classList.add('visible');
    loadMilestones();
}

function closeCreateIssueModal() {
    document.getElementById('createIssueModal').classList.remove('visible');
}

// E2E Test Functions
const REPO_ROOT = window.dashboardData.repoRoot;

// Mutable state for E2E - updated by polling
let e2eLastRun = window.dashboardData.e2eLastRun;

// E2E Progress Polling - polls while E2E is running or E2E tab is active
let e2ePollingInterval = null;
let e2eLastProgressState = null;

function startE2EPolling() {
    if (!e2ePollingInterval) {
        e2ePollingInterval = setInterval(updateE2EProgress, 5000);
        updateE2EProgress();
    }
}

function stopE2EPolling() {
    if (e2ePollingInterval) {
        clearInterval(e2ePollingInterval);
        e2ePollingInterval = null;
    }
}

// Event delegation for triage modal and quarantine actions
document.addEventListener('click', function(e) {
    const target = e.target.closest('[data-action]');
    if (!target || !target.dataset.nodeid) return;

    const action = target.dataset.action;
    const nodeid = target.dataset.nodeid;

    if (action === 'open-test-detail') {
        openTestFailureDetail(nodeid);
    }
});

// Event delegation for quarantine checkbox changes
document.addEventListener('change', function(e) {
    const target = e.target.closest('[data-action]');
    if (!target || !target.dataset.nodeid) return;

    const action = target.dataset.action;
    const nodeid = target.dataset.nodeid;

    if (action === 'quarantine-add') {
        toggleQuarantineAdd(nodeid);
    } else if (action === 'quarantine-remove') {
        toggleQuarantineRemove(nodeid);
    }
});

async function updateE2EProgress() {
    try {
        const res = await fetch(`/control/e2e/status?repo_root=${encodeURIComponent(REPO_ROOT)}`);
        const data = await res.json();

        // Update mutable last run state
        if (data.last_run) {
            e2eLastRun = data.last_run;
        }

        // Get header badge elements
        const badge = document.getElementById('e2eHeaderBadge');
        const statusIcon = badge?.querySelector('.status-icon');

        // Create state key for comparison
        const stateKey = JSON.stringify({
            running: data.running,
            lastRunStatus: data.last_run?.status,
            lastRunId: data.last_run?.id,
        });

        // Skip updates if state hasn't changed (reduces visual churn)
        if (stateKey === e2eLastProgressState) {
            return;
        }
        e2eLastProgressState = stateKey;

        // Update header badge class
        if (badge) {
            badge.classList.remove('running', 'passed', 'failed');
            if (data.running) {
                badge.classList.add('running');
            } else if (data.last_run?.status === 'passed') {
                badge.classList.add('passed');
            } else if (data.last_run?.status === 'failed') {
                badge.classList.add('failed');
            }
        }

        // Update status icon
        if (statusIcon) {
            if (data.running) {
                statusIcon.textContent = '⟳';
            } else if (data.last_run?.status === 'passed') {
                statusIcon.textContent = '✓';
            } else if (data.last_run?.status === 'failed') {
                statusIcon.textContent = '✗';
            } else {
                statusIcon.textContent = '○';
            }
        }

        // Stop polling when not running
        if (!data.running) {
            stopE2EPolling();
        }
    } catch (err) {
        console.error('E2E progress update failed:', err);
    }
}

async function startE2E(forceRestart = false) {
    try {
        // If forcing restart, stop first then start
        if (forceRestart) {
            const stopRes = await fetch('/control/e2e/stop', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_root: REPO_ROOT })
            });
            if (!stopRes.ok) {
                showToast('Failed to stop running E2E', true);
                return;
            }
            // Brief delay to let worker terminate
            await new Promise(r => setTimeout(r, 500));
        }

        const res = await fetch('/control/e2e/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_root: REPO_ROOT })
        });
        const data = await res.json();
        if (res.ok) {
            showToast('E2E tests started');
            // Update header badge to running state
            const badge = document.getElementById('e2eHeaderBadge');
            const statusIcon = badge?.querySelector('.status-icon');

            if (badge) {
                badge.classList.remove('passed', 'failed');
                badge.classList.add('running');
            }
            if (statusIcon) statusIcon.textContent = '⟳';

            // Update E2E tab controls if on E2E tab
            const e2eControls = document.getElementById('e2eControls');
            if (e2eControls) {
                e2eControls.innerHTML = `
                    <button class="issue-action-btn stop-btn" onclick="stopE2E()" id="e2eStopBtn">
                        <span aria-hidden="true">⏹</span> Stop E2E
                    </button>
                    <span class="e2e-progress-text" id="e2eProgressText">Running...</span>
                `;
            }

            startE2EPolling();
        } else if (data.error === 'already_running') {
            // Ask user if they want to cancel and restart
            if (confirm('E2E tests are already running.\n\nCancel the current run and start fresh?')) {
                startE2E(true);  // Restart with force flag
            }
        } else {
            showToast(data.detail || data.error || 'Failed to start E2E', true);
        }
    } catch (err) {
        showToast('Failed to start E2E: ' + err.message, true);
    }
}

async function stopE2E() {
    try {
        const res = await fetch('/control/e2e/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_root: REPO_ROOT })
        });
        const data = await res.json();
        if (res.ok) {
            showToast('E2E tests stopped');
            stopE2EPolling();
            // Update header badge to stopped state
            const badge = document.getElementById('e2eHeaderBadge');
            const statusIcon = badge?.querySelector('.status-icon');

            if (badge) {
                badge.classList.remove('running');
            }
            if (statusIcon) statusIcon.textContent = '○';

            // Update E2E tab controls if on E2E tab
            const e2eControls = document.getElementById('e2eControls');
            if (e2eControls) {
                e2eControls.innerHTML = `
                    <button class="issue-action-btn start-btn" onclick="startE2E()" id="e2eStartBtn">
                        <span aria-hidden="true">▶</span> Start E2E Tests
                    </button>
                    <span class="e2e-last-run">Stopped</span>
                `;
            }
        } else {
            showToast(data.detail || 'Failed to stop E2E', true);
        }
    } catch (err) {
        showToast('Failed to stop E2E: ' + err.message, true);
    }
}

// Start polling if E2E is already running on page load
if (window.dashboardData.e2eRunning) {
document.addEventListener('DOMContentLoaded', () => startE2EPolling());
}

async function showE2ELogs() {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }
    if (!e2eLastRun.log_path) {
        showToast('No log file for this run', true);
        return;
    }

    try {
        const res = await fetch(`/control/e2e/logs/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&tail=200`);
        const data = await res.json();
        if (res.ok) {
            const content = data.content || 'No logs available';
            // Show in a simple modal/alert for now
            alert(`E2E Logs (last ${data.returned_lines} lines):\n\n${content}`);
        } else {
            showToast(data.detail || 'Failed to fetch logs', true);
        }
    } catch (err) {
        showToast('Failed to fetch E2E logs', true);
    }
}

async function showQuarantineList() {
    try {
        const res = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}`);
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || 'Failed to load quarantine list', true);
            return;
        }

        let message = `Quarantine List\n`;
        message += `${'='.repeat(40)}\n\n`;
        message += `File: ${data.quarantine_file}\n`;
        message += `Status: ${data.exists ? 'exists' : 'not found'}\n`;
        message += `Count: ${data.count} test(s)\n`;

        if (data.tests.length > 0) {
            message += `\n${'─'.repeat(40)}\nQuarantined Tests:\n\n`;
            for (const test of data.tests) {
                message += `• ${test}\n`;
            }
            message += `\n${'─'.repeat(40)}\n`;
            message += `These tests are excluded from failure counts.\n`;
            message += `Edit ${data.quarantine_file} to modify.`;
        } else {
            message += `\nNo tests are currently quarantined.`;
        }

        alert(message);
    } catch (err) {
        showToast('Failed to load quarantine list: ' + err.message, true);
    }
}

async function showE2EFailures() {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }

    try {
        const res = await fetch(`/control/e2e/summary/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}`);
        const summary = await res.json();

        if (!res.ok) {
            showToast(summary.error || 'Failed to load test summary', true);
            return;
        }

        const counts = summary.counts;
        let message = `E2E Test Summary (Run #${e2eLastRun.id})\n`;
        message += `${'='.repeat(40)}\n\n`;
        message += `Total: ${counts.total} tests\n`;
        message += `  ✓ Passed: ${counts.passed}\n`;
        message += `  ✗ Failed: ${counts.failed}\n`;

        if (counts.passed_on_retry > 0) {
            message += `  ↻ Passed on Retry: ${counts.passed_on_retry}\n`;
        }
        if (counts.quarantined > 0) {
            message += `  ⚠ Quarantined: ${counts.quarantined}\n`;
        }
        if (counts.skipped > 0) {
            message += `  ○ Skipped: ${counts.skipped}\n`;
        }

        // Show failed tests
        if (summary.failed.length > 0) {
            message += `\n${'─'.repeat(40)}\nFailed Tests:\n`;
            for (const f of summary.failed) {
                message += `\n• ${f.nodeid}\n`;
                if (f.longrepr) {
                    message += `  ${f.longrepr.substring(0, 150)}...\n`;
                }
            }
        }

        // Show passed on retry
        if (summary.passed_on_retry.length > 0) {
            message += `\n${'─'.repeat(40)}\nPassed on Retry (flaky):\n`;
            for (const f of summary.passed_on_retry) {
                message += `• ${f.nodeid}\n`;
            }
        }

        // Show quarantined
        if (summary.quarantined.length > 0) {
            message += `\n${'─'.repeat(40)}\nQuarantined (excluded from failure count):\n`;
            for (const q of summary.quarantined) {
                message += `• ${q.nodeid}\n`;
            }
        }

        alert(message);
    } catch (err) {
        showToast('Failed to load test summary: ' + err.message, true);
    }
}

// E2E Diagnosis state
let e2eCurrentDiagnosis = null;

async function showE2EDiagnosis() {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }

    // Show modal with loading state
    document.getElementById('e2eDiagnosisContent').innerHTML = '<div class="loading-spinner">Loading diagnosis...</div>';
    document.getElementById('e2eDiagnosisModal').classList.add('visible');

    try {
        const res = await fetch(`/control/e2e/diagnosis/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}`);
        const diagnosis = await res.json();

        if (!res.ok) {
            showToast(diagnosis.error || diagnosis.detail || 'Failed to load diagnosis', true);
            closeE2EDiagnosisModal();
            return;
        }

        e2eCurrentDiagnosis = diagnosis;
        renderE2EDiagnosis(diagnosis);
    } catch (err) {
        showToast('Failed to load diagnosis: ' + err.message, true);
        closeE2EDiagnosisModal();
    }
}

function renderE2EDiagnosis(diagnosis) {
    const content = document.getElementById('e2eDiagnosisContent');

    let html = `
        <div class="diagnosis-header">
            <span class="diagnosis-status status-${diagnosis.status}">${diagnosis.status}</span>
            <span class="diagnosis-meta">
                Run #${diagnosis.run_id} &middot; ${diagnosis.commit_sha ? diagnosis.commit_sha.slice(0, 7) : 'unknown'} &middot; ${diagnosis.branch || 'unknown'}
                ${diagnosis.duration_seconds ? ` &middot; ${diagnosis.duration_seconds.toFixed(1)}s` : ''}
            </span>
        </div>

        <div class="diagnosis-summary">
            <div class="stat"><span class="label">Total</span><span class="value">${diagnosis.total_tests}</span></div>
            <div class="stat passed"><span class="label">Passed</span><span class="value">${diagnosis.passed_count}</span></div>
            <div class="stat failed"><span class="label">Failed</span><span class="value">${diagnosis.failed_count}</span></div>
            <div class="stat flaky"><span class="label">Flaky</span><span class="value">${diagnosis.passed_on_retry_count}</span></div>
        </div>
    `;

    // Warnings
    if (diagnosis.warnings && diagnosis.warnings.length > 0) {
        html += `
            <div class="diagnosis-section warnings">
                <h3>Warnings</h3>
                <ul>${diagnosis.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('')}</ul>
            </div>
        `;
    }

    // Suggestions
    if (diagnosis.suggestions && diagnosis.suggestions.length > 0) {
        html += `
            <div class="diagnosis-section suggestions">
                <h3>Suggestions</h3>
                <ul>${diagnosis.suggestions.map(s => `<li>${escapeHtml(s)}</li>`).join('')}</ul>
            </div>
        `;
    }

    // Failed tests
    if (diagnosis.failed_tests && diagnosis.failed_tests.length > 0) {
        html += `
            <div class="diagnosis-section">
                <h3>Failed Tests (${diagnosis.failed_tests.length})</h3>
                ${diagnosis.failed_tests.map(t => `
                    <div class="failed-test">
                        <div class="test-nodeid">${escapeHtml(t.nodeid)}</div>
                        <pre class="test-error">${escapeHtml(t.longrepr || 'No error details')}</pre>
                    </div>
                `).join('')}
            </div>
        `;
    }

    // Flaky tests
    if (diagnosis.flaky_tests && diagnosis.flaky_tests.length > 0) {
        html += `
            <div class="diagnosis-section">
                <h3>Flaky Tests - Passed on Retry (${diagnosis.flaky_tests.length})</h3>
                ${diagnosis.flaky_tests.map(t => `
                    <div class="failed-test">
                        <div class="test-nodeid">${escapeHtml(t.nodeid)}</div>
                        <pre class="test-error">${escapeHtml(t.longrepr || 'No error details')}</pre>
                    </div>
                `).join('')}
            </div>
        `;
    }

    // Log content
    if (diagnosis.log_content) {
        html += `
            <details class="diagnosis-section logs">
                <summary>Full Log Output (${diagnosis.log_content.split('\\n').length} lines)</summary>
                <pre>${escapeHtml(diagnosis.log_content)}</pre>
            </details>
        `;
    } else if (diagnosis.log_path) {
        html += `
            <div class="diagnosis-section">
                <h3>Log File</h3>
                <p style="color: #8b949e;">Log file: <code>${escapeHtml(diagnosis.log_path)}</code>
                    <button class="btn-secondary btn-sm" onclick="openPath('${escapeHtml(diagnosis.log_path)}')">Open</button>
                </p>
                <p style="color: #d29922;">${diagnosis.log_exists ? 'Log content not loaded' : 'Log file not found'}</p>
            </div>
        `;
    }

    content.innerHTML = html;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function closeE2EDiagnosisModal() {
    document.getElementById('e2eDiagnosisModal').classList.remove('visible');
}

// E2E Stats Modal
async function showE2EStats() {
    const modal = document.getElementById('e2eStatsModal');
    const content = document.getElementById('e2eStatsContent');

    content.innerHTML = '<div class="loading-spinner">Loading stats...</div>';
    modal.classList.add('visible');

    try {
        const res = await fetch(`/control/e2e/stats?repo_root=${encodeURIComponent(REPO_ROOT)}`);
        const data = await res.json();

        if (!res.ok) {
            content.innerHTML = `<div style="color: #f85149;">Error: ${escapeHtml(data.error || data.detail || 'Failed to load stats')}</div>`;
            return;
        }

        // Render stats
        const passRatePercent = data.pass_rate_percent !== null ? data.pass_rate_percent : '—';
        const passRateClass = data.pass_rate_percent === null ? 'pass-rate-unknown' :
            data.pass_rate_percent >= 90 ? 'pass-rate-good' :
            data.pass_rate_percent >= 50 ? 'pass-rate-warn' : 'pass-rate-bad';
        const passRateFill = data.pass_rate_percent !== null ? Math.min(100, Math.max(0, data.pass_rate_percent)) : 0;

        let html = `
            <div class="stats-section">
                <div class="stats-header">Pass rate (last ${data.runs_analyzed || data.flake_window_runs} runs)</div>
                <div class="stats-pass-rate">
                    <span class="stats-pass-rate-value ${passRateClass}">${passRatePercent}%</span>
                </div>
                <div class="stats-pass-rate-bar">
                    <div class="stats-pass-rate-fill ${passRateClass}" style="width: ${passRateFill}%;"></div>
                </div>
            </div>

            <div class="stats-row">
                <div class="stats-item">
                    <span class="stats-label">Flaky tests:</span>
                    <span class="stats-value">${data.flaky_count}</span>
                    ${data.flaky_count > 0 ? `<button class="btn-link" onclick="showFlakyTestsList()">View List</button>` : ''}
                </div>
            </div>

            <div class="stats-row">
                <div class="stats-item">
                    <span class="stats-label">Quarantined:</span>
                    <span class="stats-value">${data.quarantine_count}</span>
                    <button class="btn-link" onclick="closeE2EStatsModal(); openQuarantineManager();">Manage</button>
                </div>
            </div>
        `;

        if (data.next_check) {
            html += `
                <div class="stats-section stats-next-check">
                    <div class="stats-label">Next check:</div>
                    <div class="stats-value">${escapeHtml(data.next_check)}</div>
                    ${data.next_check_reason ? `<div class="stats-hint" title="Triggers when interval passed and main branch has new commits">(${escapeHtml(data.next_check_reason)})</div>` : ''}
                </div>
            `;
        }

        content.innerHTML = html;
    } catch (err) {
        content.innerHTML = `<div style="color: #f85149;">Error: ${escapeHtml(err.message)}</div>`;
    }
}

function closeE2EStatsModal() {
    document.getElementById('e2eStatsModal').classList.remove('visible');
}

async function showFlakyTestsList() {
    // Close stats modal and show flaky tests in a simple alert for now
    closeE2EStatsModal();

    try {
        const res = await fetch(`/control/e2e/flaky-tests?repo_root=${encodeURIComponent(REPO_ROOT)}`);
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || 'Failed to load flaky tests', true);
            return;
        }

        if (data.flaky_tests.length === 0) {
            showToast('No flaky tests detected');
            return;
        }

        let message = `Flaky Tests (flip rate > ${data.threshold}%)\n`;
        message += `${'='.repeat(50)}\n\n`;

        for (const test of data.flaky_tests) {
            const quarantineBadge = test.is_quarantined ? ' [QUARANTINED]' : '';
            message += `• ${test.nodeid}${quarantineBadge}\n`;
            message += `  Flip rate: ${test.flip_rate_percent}% (${test.flip_count} flips in ${data.window} runs)\n\n`;
        }

        alert(message);
    } catch (err) {
        showToast('Failed to load flaky tests: ' + err.message, true);
    }
}

// Current test failure being viewed in the modal
let currentTestFailure = null;

// Open the test failure detail modal
async function openTestFailureDetail(nodeid) {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }

    const modal = document.getElementById('testFailureModal');
    const content = document.getElementById('testFailureContent');

    // Show modal with loading state
    content.innerHTML = '<div class="loading-spinner">Loading test details...</div>';
    modal.classList.add('visible');

    try {
        // Use the dedicated test detail endpoint
        const res = await fetch(`/control/e2e/test/${e2eLastRun.id}?nodeid=${encodeURIComponent(nodeid)}&repo_root=${encodeURIComponent(REPO_ROOT)}`);
        const data = await res.json();

        if (!res.ok) {
            content.innerHTML = `<div style="color: #f85149;">Error: ${escapeHtml(data.error || data.detail || 'Failed to load test details')}</div>`;
            return;
        }

        // Store for action buttons
        currentTestFailure = {
            nodeid,
            test: data.test,
            run: data.run,
            history: data.history,
            history_summary: data.history_summary,
            flake_count: data.flake_count,
            flip_count: data.flip_count,
            flip_rate: data.flip_rate,
            flip_rate_percent: data.flip_rate_percent,
            category: data.category,
            is_likely_flaky: data.is_likely_flaky,
            existing_issue: data.existing_issue,
            log_excerpt: data.log_excerpt,
        };

        // Render the test failure details
        renderTestFailureDetail(currentTestFailure);
    } catch (err) {
        content.innerHTML = `<div style="color: #f85149;">Failed to load test details: ${escapeHtml(err.message)}</div>`;
    }
}

function renderTestFailureDetail(data) {
    const content = document.getElementById('testFailureContent');
    const test = data.test;
    const run = data.run;
    const shortName = test.nodeid.split('::').pop();

    // Build status line
    let statusParts = [`<strong>FAILED</strong>`];
    if (test.duration_seconds) {
        statusParts.push(`${test.duration_seconds.toFixed(1)}s`);
    }
    if (test.retry_outcome) {
        statusParts.push(`retry: ${test.retry_outcome}`);
    }

    // Build history visualization (last N runs as icons)
    let historyHtml = '';
    if (data.history && data.history.length > 0) {
        const icons = data.history.map(h => {
            if (h.outcome === 'passed') return '<span style="color: #3fb950;">✓</span>';
            if (h.outcome === 'failed' || h.outcome === 'error') return '<span style="color: #f85149;">✗</span>';
            return '<span style="color: #8b949e;">○</span>';
        }).reverse().join(' ');

        const summary = data.history_summary;
        let passRateText = '';
        if (summary.pass_rate !== null) {
            passRateText = ` (${Math.round(summary.pass_rate * 100)}% pass rate)`;
        }

        let flakyWarning = '';
        if (data.category === 'flaky') {
            flakyWarning = `<span style="color: #d29922; margin-left: 8px;">⚠ Flaky (${data.flip_rate_percent}% flip rate)</span>`;
        } else if (data.category === 'consistently_failing') {
            flakyWarning = `<span style="color: #f85149; margin-left: 8px;">⚠ Consistently failing</span>`;
        } else if (data.category === 'new_failure') {
            flakyWarning = `<span style="color: #58a6ff; margin-left: 8px;">● New failure</span>`;
        } else if (data.category === 'recovered') {
            flakyWarning = `<span style="color: #3fb950; margin-left: 8px;">↑ Recovered</span>`;
        }

        historyHtml = `
            <div class="test-failure-section" style="background: #161b22; padding: 12px; border-radius: 6px; margin-bottom: 16px;">
                <div style="font-size: 13px; color: #8b949e; margin-bottom: 4px;">History (last ${data.history.length} runs):</div>
                <div style="font-size: 16px; letter-spacing: 2px;">${icons}${passRateText}</div>
                ${flakyWarning}
            </div>
        `;
    }

    // Existing issue link
    let existingIssueHtml = '';
    if (data.existing_issue) {
        existingIssueHtml = `
            <div class="test-failure-section" style="background: #1f3d1f; padding: 12px; border-radius: 6px; margin-bottom: 16px; border: 1px solid #238636;">
                <span style="color: #3fb950;">✓</span>
                <span>Issue already exists: </span>
                <a href="https://github.com/${window.dashboardData.githubOwner}/${window.dashboardData.githubRepo}/issues/${data.existing_issue.github_issue_number}"
                   target="_blank" style="color: #58a6ff;">#${data.existing_issue.github_issue_number}</a>
                ${data.existing_issue.resolution ? `<span style="color: #8b949e;"> (${data.existing_issue.resolution})</span>` : ''}
            </div>
        `;
    }

    let html = `
        <div class="test-failure-header">
            <span class="status-icon failed">✗</span>
            <div class="test-failure-info">
                <div class="test-failure-nodeid">${escapeHtml(test.nodeid)}</div>
                <div class="test-failure-meta">
                    <span>${statusParts.join(' · ')}</span>
                    ${run.started_at ? `<span><strong>Run:</strong> ${new Date(run.started_at).toLocaleString()}</span>` : ''}
                    ${run.commit_sha ? `<span><strong>Commit:</strong> ${run.commit_sha.substring(0, 7)}</span>` : ''}
                </div>
            </div>
        </div>

        ${historyHtml}
        ${existingIssueHtml}
    `;

    // Error section
    html += `
        <div class="test-failure-section">
            <h3>Error</h3>
            <div class="test-failure-error">${test.longrepr ? escapeHtml(test.longrepr) : '<span style="color: #8b949e;">No error details available</span>'}</div>
        </div>
    `;

    // Log excerpt (expandable)
    if (data.log_excerpt) {
        const lineCount = data.log_excerpt.split('\\n').length;
        html += `
            <details class="test-failure-section">
                <summary style="cursor: pointer; color: #58a6ff; font-size: 14px; font-weight: 600;">
                    Test Logs (${lineCount} lines)
                </summary>
                <pre class="test-failure-traceback" style="margin-top: 8px;">${escapeHtml(data.log_excerpt)}</pre>
            </details>
        `;
    }

    // "What to do" section with Diagnose button
    html += `
        <div class="test-failure-section" style="margin-top: 20px; padding-top: 16px; border-top: 1px solid #21262d;">
            <h3>What To Do</h3>
            <p style="color: #8b949e; font-size: 13px; margin-bottom: 12px;">
                Get AI-powered analysis to help understand this failure and suggest fixes.
            </p>
            <button class="btn-primary" onclick="diagnoseCurrentTest()" style="display: flex; align-items: center; gap: 6px;">
                <span>🔍</span> Diagnose This Failure
            </button>
        </div>
    `;

    content.innerHTML = html;
}

async function diagnoseCurrentTest() {
    if (!currentTestFailure) {
        showToast('No test selected', true);
        return;
    }

    // For now, show the full run diagnosis with this test highlighted
    // In the future, this could trigger AI analysis
    showToast('Opening full diagnosis...', false);
    closeTestFailureModal();
    showE2EDiagnosis();
}

function closeTestFailureModal() {
    document.getElementById('testFailureModal').classList.remove('visible');
    currentTestFailure = null;
}

async function createIssueForCurrentTest() {
    if (!currentTestFailure) {
        showToast('No test selected', true);
        return;
    }
    closeTestFailureModal();
    await createSingleIssue(currentTestFailure.nodeid);
}

async function quarantineCurrentTest() {
    if (!currentTestFailure) {
        showToast('No test selected', true);
        return;
    }
    closeTestFailureModal();
    await quarantineSingleTest(currentTestFailure.nodeid);
}

function copyTestError() {
    if (!currentTestFailure || !currentTestFailure.test) {
        showToast('No test selected', true);
        return;
    }

    const test = currentTestFailure.test;
    const text = `Test: ${test.nodeid}\\n\\n` +
        `Outcome: ${test.outcome}\\n` +
        `Duration: ${test.duration_seconds ? test.duration_seconds.toFixed(2) + 's' : 'unknown'}\\n\\n` +
        `Error:\\n${test.longrepr || 'No error details available'}`;

    navigator.clipboard.writeText(text).then(() => {
        showToast('Error details copied to clipboard');
    }).catch(err => {
        showToast('Failed to copy: ' + err.message, true);
    });
}

// Legacy function - redirect to new modal
async function showTestDiagnosis(nodeid) {
    await openTestFailureDetail(nodeid);
}

// Create issue for a single test
async function createSingleIssue(nodeid) {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }

    // Get available agents (just the names)
    const agentList = window.dashboardData.agents;
    if (agentList.length === 0) {
        showToast('No agents configured', true);
        return;
    }

    // Use first agent or prompt if multiple
    let agent = agentList[0];
    if (agentList.length > 1) {
        const choice = prompt(`Select agent for this issue:\n\nAvailable: ${agentList.join(', ')}\n\nEnter agent name:`, agentList[0]);
        if (!choice) return;
        if (!agentList.includes(choice)) {
            showToast(`Invalid agent: ${choice}`, true);
            return;
        }
        agent = choice;
    }

    try {
        const res = await fetch(`/control/e2e/create-issues/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                nodeids: [nodeid],
                agent: agent,
            }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to create issue', true);
            return;
        }

        const testName = nodeid.split('::').pop();
        if (data.parent_issue) {
            showToast(`Created issue #${data.parent_issue.number} for ${testName}`);
            // Open issue in new tab
            if (data.parent_issue.url) {
                window.open(data.parent_issue.url, '_blank');
            }
        } else {
            showToast('Issue created successfully');
        }

        // Refresh to show the new issue
        setTimeout(() => location.reload(), 500);
    } catch (err) {
        showToast('Failed to create issue: ' + err.message, true);
    }
}

// Quarantine a single test
async function quarantineSingleTest(nodeid) {
    if (!confirm(`Add "${nodeid.split('::').pop()}" to quarantine?\n\nQuarantined tests are excluded from E2E failure counts.`)) {
        return;
    }

    try {
        const res = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'add', nodeids: [nodeid] }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || 'Failed to quarantine test', true);
            return;
        }

        showToast(`Added ${nodeid.split('::').pop()} to quarantine`);
        // Refresh to update the UI
        setTimeout(() => location.reload(), 500);
    } catch (err) {
        showToast('Failed to quarantine test: ' + err.message, true);
    }
}

async function createE2EDiagnosticIssue() {
    const runId = e2eCurrentDiagnosis?.run_id
        || currentRunDetails?.run?.id
        || e2eLastRun?.id;
    if (!runId) {
        showToast('No run data available', true);
        return;
    }

    const agentSelect = document.getElementById('e2eDiagnosisAgent');
    const agent = agentSelect.value;
    if (!agent) {
        showToast('Please select an agent to work on this issue', true);
        agentSelect.focus();
        return;
    }

    const btn = document.getElementById('e2eCreateIssueBtn');
    btn.disabled = true;
    btn.textContent = 'Creating...';

    try {
        const res = await fetch(`/control/e2e/diagnosis/${runId}/issue?repo_root=${encodeURIComponent(REPO_ROOT)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent: agent }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to create issue', true);
            return;
        }

        showToast(`Issue #${data.issue_number} created!`);
        closeE2EDiagnosisModal();

        if (data.url) {
            window.open(data.url, '_blank');
        }
    } catch (err) {
        showToast('Failed to create issue: ' + err.message, true);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Create Issue';
    }
}

// E2E Triage Functions
let e2eTriageData = null;

// Store current run details for test failure drill-down
let currentRunDetails = null;

async function showE2ERunDetails(runId) {
    // Redirect to the unified run view
    return showUnifiedRunView(runId);
}

// Legacy run details view - kept for reference but replaced by showUnifiedRunView
async function showE2ERunDetailsLegacy(runId) {
    // Show run details in the diagnosis modal
    document.getElementById('e2eDiagnosisContent').innerHTML = '<div class="loading-spinner">Loading run details...</div>';
    document.getElementById('e2eDiagnosisModal').classList.add('visible');

    try {
        const res = await fetch(`/control/e2e/run/${runId}?repo_root=${encodeURIComponent(REPO_ROOT)}&enhanced=false`);
        const data = await res.json();

        if (!res.ok) {
            closeE2EDiagnosisModal();
            showToast(data.error || data.detail || 'Failed to load run details', true);
            return;
        }

        currentRunDetails = data;

        // Render run details with test results
        const content = document.getElementById('e2eDiagnosisContent');
        const run = data.run;
        const results = data.results || [];
        const statusClass = run.status === 'passed' ? 'passed' : run.status === 'failed' ? 'failed' : '';

        // Categorize test results (respect retry_outcome for flaky tests)
        const passed = results.filter(r => r.outcome === 'passed');
        const passedOnRetry = results.filter(r => r.outcome === 'failed' && r.retry_outcome === 'passed' && !r.is_quarantined);
        const failed = results.filter(r => r.outcome === 'failed' && r.retry_outcome !== 'passed' && !r.is_quarantined);
        const quarantined = results.filter(r => r.is_quarantined);
        const skipped = results.filter(r => r.outcome === 'skipped');

        let html = `
            <div class="run-details">
                <div class="run-header">
                    <h3>E2E Run #${run.id}</h3>
                    <span class="e2e-status-badge ${statusClass}">${run.status}</span>
                </div>
                <div class="run-info">
                    <div class="info-row"><span class="label">Started:</span> <span>${run.started_at || 'N/A'}</span></div>
                    <div class="info-row"><span class="label">Finished:</span> <span>${run.finished_at || 'N/A'}</span></div>
                    <div class="info-row"><span class="label">Commit:</span> <span>${run.commit_sha || 'N/A'}</span></div>
                    <div class="info-row"><span class="label">Summary:</span> <span class="test-summary">
                        <span class="passed-count">${passed.length + passedOnRetry.length} passed</span>
                        ${passedOnRetry.length > 0 ? `<span class="flaky-count">${passedOnRetry.length} flaky</span>` : ''}
                        ${failed.length > 0 ? `<span class="failed-count">${failed.length} failed</span>` : ''}
                        ${quarantined.length > 0 ? `<span class="quarantined-count">${quarantined.length} quarantined</span>` : ''}
                        ${skipped.length > 0 ? `<span class="skipped-count">${skipped.length} skipped</span>` : ''}
                    </span></div>
                    ${run.log_path ? `<div class="info-row"><span class="label">Log:</span> <span><code>${escapeHtml(run.log_path)}</code> <button class="btn-secondary btn-sm" onclick="openPath('${escapeHtml(run.log_path)}')">Open</button></span></div>` : ''}
                </div>
        `;

        // Show failed tests if any
        if (failed.length > 0) {
            html += `
                <div class="test-results-section">
                    <h4>Failed Tests (${failed.length})</h4>
                    <div class="test-results-list">
            `;
            for (const test of failed) {
                const shortName = test.nodeid.split('::').pop();
                html += `
                    <div class="test-result-item failed clickable" onclick="showRunTestDetail('${escapeHtml(test.nodeid)}')" title="Click to view details">
                        <span class="status-icon failed">✗</span>
                        <span class="test-name" title="${escapeHtml(test.nodeid)}">${escapeHtml(shortName)}</span>
                        ${test.duration_seconds ? `<span class="duration">${test.duration_seconds.toFixed(1)}s</span>` : ''}
                        <span class="click-hint">→</span>
                    </div>
                `;
            }
            html += `</div></div>`;
        }

        // Show flaky tests (passed on retry)
        if (passedOnRetry.length > 0) {
            html += `
                <div class="test-results-section">
                    <h4>Passed on Retry – Flaky (${passedOnRetry.length})</h4>
                    <div class="test-results-list">
            `;
            for (const test of passedOnRetry) {
                const shortName = test.nodeid.split('::').pop();
                html += `
                    <div class="test-result-item flaky clickable" onclick="showRunTestDetail('${escapeHtml(test.nodeid)}')" title="Click to view details">
                        <span class="status-icon flaky">⟳</span>
                        <span class="test-name" title="${escapeHtml(test.nodeid)}">${escapeHtml(shortName)}</span>
                        ${test.duration_seconds ? `<span class="duration">${test.duration_seconds.toFixed(1)}s</span>` : ''}
                        <span class="click-hint">→</span>
                    </div>
                `;
            }
            html += `</div></div>`;
        }

        // Show quarantined tests if any
        if (quarantined.length > 0) {
            html += `
                <div class="test-results-section">
                    <h4>Quarantined (${quarantined.length})</h4>
                    <div class="test-results-list">
            `;
            for (const test of quarantined) {
                const shortName = test.nodeid.split('::').pop();
                const outcome = test.outcome === 'failed' ? 'failed' : 'passed';
                html += `
                    <div class="test-result-item quarantined clickable" onclick="showRunTestDetail('${escapeHtml(test.nodeid)}')" title="Click to view details">
                        <span class="status-icon quarantined">⚠</span>
                        <span class="test-name" title="${escapeHtml(test.nodeid)}">${escapeHtml(shortName)}</span>
                        <span class="outcome-badge ${outcome}">${outcome}</span>
                        <span class="click-hint">→</span>
                    </div>
                `;
            }
            html += `</div></div>`;
        }

        html += `</div>`;
        content.innerHTML = html;
    } catch (err) {
        closeE2EDiagnosisModal();
        showToast('Failed to load run details: ' + err.message, true);
    }
}

// Show details for a specific test from run details
function showRunTestDetail(nodeid) {
    if (!currentRunDetails || !currentRunDetails.results) {
        showToast('No test data available', true);
        return;
    }

    const test = currentRunDetails.results.find(r => r.nodeid === nodeid);
    if (!test) {
        showToast('Test not found', true);
        return;
    }

    // Render test detail in the same modal
    const content = document.getElementById('e2eDiagnosisContent');
    const shortName = test.nodeid.split('::').pop();
    const statusClass = test.outcome === 'passed' ? 'passed' : 'failed';

    let html = `
        <div class="test-detail-view">
            <button class="back-btn" onclick="showE2ERunDetails(${currentRunDetails.run.id})">← Back to Run</button>
            <div class="test-detail-header">
                <h3>${escapeHtml(shortName)}</h3>
                <span class="e2e-status-badge ${statusClass}">${test.outcome}</span>
                ${test.is_quarantined ? '<span class="quarantine-badge">Quarantined</span>' : ''}
            </div>
            <div class="test-detail-info">
                <div class="info-row"><span class="label">Full path:</span> <code>${escapeHtml(test.nodeid)}</code></div>
                ${test.duration_seconds ? `<div class="info-row"><span class="label">Duration:</span> <span>${test.duration_seconds.toFixed(2)}s</span></div>` : ''}
                ${test.retry_outcome ? `<div class="info-row"><span class="label">Retry:</span> <span>${test.retry_outcome}</span></div>` : ''}
            </div>
    `;

    if (test.error_message) {
        html += `
            <div class="test-error-section">
                <h4>Error</h4>
                <pre class="error-output">${escapeHtml(test.error_message)}</pre>
            </div>
        `;
    }

    if (test.stdout) {
        html += `
            <div class="test-output-section">
                <h4>Stdout</h4>
                <pre class="test-output">${escapeHtml(test.stdout)}</pre>
            </div>
        `;
    }

    if (test.stderr) {
        html += `
            <div class="test-output-section">
                <h4>Stderr</h4>
                <pre class="test-output">${escapeHtml(test.stderr)}</pre>
            </div>
        `;
    }

    // Add rerun/copy buttons for failed tests
    if (test.outcome === 'failed') {
        html += `
            <div class="test-detail-actions">
                <button class="btn-secondary btn-sm" onclick="rerunTest('${escapeHtml(test.nodeid)}')">Rerun Test</button>
                <button class="btn-secondary btn-sm" onclick="copyTestCommand('${escapeHtml(test.nodeid)}')">Copy Command</button>
            </div>
        `;
    }

    html += `</div>`;
    content.innerHTML = html;
}

// Helper to escape HTML
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function rerunTest(nodeid) {
    try {
        const res = await fetch('/control/e2e/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                repo_root: REPO_ROOT,
                pytest_args: [nodeid, '-v'],
            })
        });
        const data = await res.json();
        if (res.ok) {
            showToast('Rerunning test...');
            startE2EPolling();
        } else if (data.error === 'already_running') {
            showToast('E2E tests already running', true);
        } else {
            showToast(data.detail || data.error || 'Failed to start rerun', true);
        }
    } catch (err) {
        showToast('Failed to rerun test: ' + err.message, true);
    }
}

function copyTestCommand(nodeid) {
    const cmd = `cd ${REPO_ROOT} && pytest ${nodeid} -v`;
    navigator.clipboard.writeText(cmd).then(
        () => showToast('Command copied to clipboard'),
        () => showToast('Failed to copy command', true)
    );
}

function rerunCurrentTest() {
    if (currentTestFailure?.nodeid) {
        rerunTest(currentTestFailure.nodeid);
    }
}

function copyCurrentTestCommand() {
    if (currentTestFailure?.nodeid) {
        copyTestCommand(currentTestFailure.nodeid);
    }
}

async function showE2ETriage() {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }

    // Show modal with loading state
    document.getElementById('e2eTriageContent').innerHTML = '<div class="loading-spinner">Loading triage data...</div>';
    document.getElementById('e2eTriageModal').classList.add('visible');

    try {
        const res = await fetch(`/control/e2e/triage/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}`);
        const data = await res.json();

        if (!res.ok) {
            closeE2ETriageModal();
            showToast(data.error || data.detail || 'Failed to load triage data', true);
            return;
        }

        e2eTriageData = data;
        renderE2ETriage(data);
    } catch (err) {
        closeE2ETriageModal();
        showToast('Failed to load triage data: ' + err.message, true);
    }
}

function renderE2ETriage(data) {
    const content = document.getElementById('e2eTriageContent');
    const failures = data.failures || [];

    if (failures.length === 0) {
        content.innerHTML = '<p>No failures to triage.</p>';
        return;
    }

    // Count categories
    const newFailures = failures.filter(f => !f.existing_issue && !f.is_likely_flaky);
    const flakyFailures = failures.filter(f => f.category === 'flaky');
    const consistentFailures = failures.filter(f => f.category === 'consistently_failing');
    const existingIssues = failures.filter(f => f.existing_issue);

    let html = `
        <div class="triage-summary">
            <div class="triage-summary-stat failures">
                <div class="count">${failures.length}</div>
                <div class="label">Total Failures</div>
            </div>
            <div class="triage-summary-stat">
                <div class="count">${newFailures.length}</div>
                <div class="label">New</div>
            </div>
            <div class="triage-summary-stat flaky">
                <div class="count">${flakyFailures.length}</div>
                <div class="label">Flaky</div>
            </div>
            <div class="triage-summary-stat" style="${consistentFailures.length > 0 ? 'color: #f85149;' : ''}">
                <div class="count">${consistentFailures.length}</div>
                <div class="label">Consistent</div>
            </div>
            <div class="triage-summary-stat existing">
                <div class="count">${existingIssues.length}</div>
                <div class="label">Has Issue</div>
            </div>
        </div>

        <div class="triage-select-all">
            <input type="checkbox" id="triageSelectAll" class="triage-checkbox" onchange="toggleAllTriageItems(this.checked)">
            <label for="triageSelectAll">Select all new failures for issue creation</label>
        </div>

        <div class="triage-list">
    `;

    for (const failure of failures) {
        const hasIssue = !!failure.existing_issue;
        const isFlaky = failure.is_likely_flaky;
        const itemClass = hasIssue ? 'has-issue' : (isFlaky ? 'is-flaky' : '');
        const shortNodeid = failure.nodeid.split('::').pop() || failure.nodeid;

        html += `
            <div class="triage-item ${itemClass}">
                <input type="checkbox"
                       class="triage-checkbox triage-item-checkbox"
                       data-nodeid="${escapeAttr(failure.nodeid)}"
                       ${hasIssue ? 'disabled' : ''}
                       ${!hasIssue && !isFlaky ? 'checked' : ''}
                       onchange="updateTriageSelection()">
                <div class="triage-details clickable" data-action="open-test-detail" data-nodeid="${escapeAttr(failure.nodeid)}" title="Click to view full details">
                    <div class="triage-nodeid" title="${escapeHtml(failure.nodeid)}">${escapeHtml(shortNodeid)}</div>
                    <div class="triage-badges">
                        ${hasIssue ? `<span class="triage-badge existing">Issue #${failure.existing_issue.github_issue_number}</span>` : '<span class="triage-badge new">New</span>'}
                        ${failure.category === 'flaky' ? `<span class="triage-badge flaky">Flaky (${failure.flip_rate_percent}%)</span>` : ''}
                        ${failure.category === 'consistently_failing' ? '<span class="triage-badge" style="background: #f85149; color: #fff;">Consistent</span>' : ''}
                        ${failure.category === 'new_failure' ? '<span class="triage-badge" style="background: #58a6ff; color: #fff;">New failure</span>' : ''}
                        ${failure.duration_seconds ? `<span class="triage-badge">${failure.duration_seconds.toFixed(1)}s</span>` : ''}
                    </div>
                    ${failure.longrepr ? `<div class="triage-longrepr">${escapeHtml(failure.longrepr.substring(0, 500))}${failure.longrepr.length > 500 ? '...' : ''}</div>` : ''}
                    <span class="triage-detail-hint">→</span>
                </div>
            </div>
        `;
    }

    html += '</div>';

    if (data.has_parent_issue) {
        html += renderIssueStatusSection(data);
    }

    content.innerHTML = html;
    updateTriageSelection();
}

function renderIssueStatusSection(data) {
    const parentUrl = data.parent_issue_url || '#';
    const isClosed = data.parent_issue_closed;
    const subIssues = data.sub_issues || [];
    const summary = data.sub_issues_summary || { total: 0, resolved: 0 };

    let html = `
        <div class="issue-status-section">
            <div class="issue-status-header">
                <span class="issue-status-title">Issue Status</span>
            </div>
            <div style="margin-bottom: 8px;">
                <a href="${parentUrl}" target="_blank" class="issue-link">
                    #${data.parent_issue_number}
                </a>
                <span class="issue-status-badge ${isClosed ? 'closed' : 'open'}">
                    ${isClosed ? 'CLOSED' : 'OPEN'}
                </span>
            </div>
    `;

    if (subIssues.length > 0) {
        html += `
            <div class="sub-issues-summary">
                Sub-issues: <strong>${summary.resolved}</strong> of <strong>${summary.total}</strong> resolved
            </div>
            <div id="subIssuesToggle" class="sub-issues-toggle" onclick="toggleSubIssuesList()">
                <span class="arrow">▶</span>
                <span class="toggle-text">Show sub-issues</span>
            </div>
            <div id="subIssuesList" class="sub-issues-list">
        `;

        for (const issue of subIssues) {
            const shortNodeid = issue.nodeid.split('::').pop() || issue.nodeid;
            const statusClass = issue.resolved ? 'resolved' : 'open';
            const statusText = issue.resolved ? (issue.resolution || 'resolved') : 'open';
            const issueUrl = issue.url || '#';

            html += `
                <div class="sub-issue-item">
                    <a href="${issueUrl}" target="_blank" class="issue-link">#${issue.issue_number}</a>
                    <span class="sub-issue-nodeid" title="${escapeHtml(issue.nodeid)}">${escapeHtml(shortNodeid)}</span>
                    <span class="issue-status-badge ${statusClass}">${statusText.toUpperCase()}</span>
                </div>
            `;
        }

        html += '</div>';
    }

    html += '</div>';
    return html;
}

function toggleSubIssuesList() {
    const list = document.getElementById('subIssuesList');
    const toggle = document.getElementById('subIssuesToggle');
    const isExpanded = list.classList.contains('expanded');

    if (isExpanded) {
        list.classList.remove('expanded');
        toggle.classList.remove('expanded');
        toggle.querySelector('.toggle-text').textContent = 'Show sub-issues';
    } else {
        list.classList.add('expanded');
        toggle.classList.add('expanded');
        toggle.querySelector('.toggle-text').textContent = 'Hide sub-issues';
    }
}

// Toggle E2E sub-issues expansion in issue list
function toggleE2ESubIssues(issueId, event) {
    event.stopPropagation();
    const container = document.getElementById(`e2e-sub-${issueId}`);
    const button = event.currentTarget;

    if (!container) return;

    const isExpanded = container.style.display !== 'none';
    container.style.display = isExpanded ? 'none' : 'block';
    button.setAttribute('aria-expanded', !isExpanded);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Escape for use in HTML attributes (handles quotes unlike escapeHtml)
function escapeAttr(text) {
    if (!text) return '';
    return text
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function toggleAllTriageItems(checked) {
    const checkboxes = document.querySelectorAll('.triage-item-checkbox:not(:disabled)');
    checkboxes.forEach(cb => cb.checked = checked);
    updateTriageSelection();
}

function updateTriageSelection() {
    const checkboxes = document.querySelectorAll('.triage-item-checkbox:checked:not(:disabled)');
    const createBtn = document.getElementById('e2eCreateIssuesBtn');
    createBtn.disabled = checkboxes.length === 0;
    createBtn.textContent = checkboxes.length > 0 ? `Create ${checkboxes.length} Issue${checkboxes.length > 1 ? 's' : ''}` : 'Create Issues';
}

function closeE2ETriageModal() {
    document.getElementById('e2eTriageModal').classList.remove('visible');
}

async function createE2EIssues() {
    const agentSelect = document.getElementById('e2eTriageAgent');
    const agent = agentSelect.value;
    if (!agent) {
        showToast('Please select an agent to work on these issues', true);
        agentSelect.focus();
        return;
    }

    const checkboxes = document.querySelectorAll('.triage-item-checkbox:checked:not(:disabled)');
    const selectedNodeids = Array.from(checkboxes).map(cb => cb.dataset.nodeid);

    if (selectedNodeids.length === 0) {
        showToast('No failures selected', true);
        return;
    }

    const btn = document.getElementById('e2eCreateIssuesBtn');
    btn.disabled = true;
    btn.textContent = 'Creating...';

    try {
        const res = await fetch(`/control/e2e/create-issues/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nodeids: selectedNodeids, agent: agent }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to create issues', true);
            return;
        }

        showToast(`Created parent issue #${data.parent_issue.number} with ${data.sub_issues.length} sub-issue(s)`);
        closeE2ETriageModal();

        // Open parent issue in new tab
        if (data.parent_issue.url) {
            window.open(data.parent_issue.url, '_blank');
        }
    } catch (err) {
        showToast('Failed to create issues: ' + err.message, true);
    } finally {
        btn.disabled = false;
        updateTriageSelection();
    }
}

async function syncE2EIssues() {
    if (!e2eLastRun) {
        showToast('No E2E run to sync', true);
        return;
    }

    const btn = document.getElementById('e2eSyncBtn');
    btn.disabled = true;
    btn.textContent = 'Syncing...';

    try {
        const res = await fetch(`/control/e2e/sync-issues/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}`, {
            method: 'POST',
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to sync issues', true);
            return;
        }

        const closedCount = data.closed_issues.length;
        const parentCount = data.closed_parent_issues.length;

        if (closedCount === 0 && parentCount === 0) {
            showToast('No issues to sync - all tests still failing or no open issues');
        } else {
            showToast(`Synced: closed ${closedCount} sub-issue(s), ${parentCount} parent issue(s)`);
        }

        // Refresh triage data to show updated state
        if (e2eLastRun.id) {
            triageE2ERun(e2eLastRun.id);
        }
    } catch (err) {
        showToast('Failed to sync issues: ' + err.message, true);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Sync Issues';
    }
}

// Quarantine viewer state
let quarantineData = { current: [], flaky: [], toAdd: new Set(), toRemove: new Set() };

async function showQuarantineViewer() {
    const modal = document.getElementById('e2eQuarantineModal');
    const loading = document.getElementById('quarantineLoading');
    const content = document.getElementById('quarantineContent');

    modal.classList.add('visible');
    loading.style.display = 'block';
    content.style.display = 'none';

    quarantineData = { current: [], flaky: [], toAdd: new Set(), toRemove: new Set() };

    try {
        // Fetch current quarantine list and flaky tests in parallel
        const [quarantineRes, flakyRes] = await Promise.all([
            fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}`),
            fetch(`/control/e2e/flaky-tests?repo_root=${encodeURIComponent(REPO_ROOT)}&threshold=3&window=10`)
        ]);

        const quarantine = await quarantineRes.json();
        const flaky = await flakyRes.json();

        if (!quarantineRes.ok) {
            showToast(quarantine.error || 'Failed to load quarantine list', true);
            closeQuarantineModal();
            return;
        }

        quarantineData.current = quarantine.tests || [];
        quarantineData.flaky = (flaky.flaky_tests || []).filter(t => !t.is_quarantined);

        document.getElementById('quarantineFile').textContent = `File: ${quarantine.quarantine_file}`;
        renderQuarantineViewer();

        loading.style.display = 'none';
        content.style.display = 'block';
    } catch (err) {
        showToast('Failed to load quarantine data: ' + err.message, true);
        closeQuarantineModal();
    }
}

function renderQuarantineViewer() {
    const currentList = document.getElementById('quarantineCurrentList');
    const flakyList = document.getElementById('quarantineFlakyList');
    const flakySection = document.getElementById('quarantineFlakySection');

    // Render flaky suggestions
    if (quarantineData.flaky.length > 0) {
        flakySection.style.display = 'block';
        flakyList.innerHTML = quarantineData.flaky.map(test => `
            <div class="triage-item">
                <label class="triage-checkbox-label">
                    <input type="checkbox" class="quarantine-add-checkbox"
                           data-nodeid="${escapeAttr(test.nodeid)}"
                           data-action="quarantine-add"
                           ${quarantineData.toAdd.has(test.nodeid) ? 'checked' : ''}>
                    <span class="test-nodeid">${escapeHtml(test.nodeid)}</span>
                    <span class="flake-badge">${test.flip_rate_percent}% flip rate</span>
                </label>
            </div>
        `).join('');
    } else {
        flakySection.style.display = 'none';
    }

    // Render current quarantine list
    if (quarantineData.current.length > 0) {
        currentList.innerHTML = quarantineData.current.map(nodeid => `
            <div class="triage-item ${quarantineData.toRemove.has(nodeid) ? 'to-remove' : ''}">
                <label class="triage-checkbox-label">
                    <input type="checkbox" class="quarantine-remove-checkbox"
                           data-nodeid="${escapeAttr(nodeid)}"
                           data-action="quarantine-remove"
                           ${quarantineData.toRemove.has(nodeid) ? 'checked' : ''}>
                    <span class="test-nodeid">${escapeHtml(nodeid)}</span>
                    ${quarantineData.toRemove.has(nodeid) ? '<span class="remove-badge">will remove</span>' : ''}
                </label>
            </div>
        `).join('');
    } else {
        currentList.innerHTML = '<div class="triage-empty">No tests currently quarantined</div>';
    }

    // Update count and save button state
    const effectiveCount = quarantineData.current.length + quarantineData.toAdd.size - quarantineData.toRemove.size;
    document.getElementById('quarantineCount').textContent = `${effectiveCount} test${effectiveCount !== 1 ? 's' : ''} quarantined`;

    const saveBtn = document.getElementById('quarantineSaveBtn');
    saveBtn.disabled = quarantineData.toAdd.size === 0 && quarantineData.toRemove.size === 0;
}

function toggleQuarantineAdd(nodeid) {
    if (quarantineData.toAdd.has(nodeid)) {
        quarantineData.toAdd.delete(nodeid);
    } else {
        quarantineData.toAdd.add(nodeid);
    }
    renderQuarantineViewer();
}

function toggleQuarantineRemove(nodeid) {
    if (quarantineData.toRemove.has(nodeid)) {
        quarantineData.toRemove.delete(nodeid);
    } else {
        quarantineData.toRemove.add(nodeid);
    }
    renderQuarantineViewer();
}

async function saveQuarantineChanges() {
    const btn = document.getElementById('quarantineSaveBtn');
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        // Process additions
        if (quarantineData.toAdd.size > 0) {
            const addRes = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'add', nodeids: Array.from(quarantineData.toAdd) })
            });
            if (!addRes.ok) {
                const data = await addRes.json();
                throw new Error(data.error || 'Failed to add to quarantine');
            }
        }

        // Process removals
        if (quarantineData.toRemove.size > 0) {
            const removeRes = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'remove', nodeids: Array.from(quarantineData.toRemove) })
            });
            if (!removeRes.ok) {
                const data = await removeRes.json();
                throw new Error(data.error || 'Failed to remove from quarantine');
            }
        }

        showToast(`Quarantine updated: +${quarantineData.toAdd.size} added, -${quarantineData.toRemove.size} removed`);
        closeQuarantineModal();
    } catch (err) {
        showToast('Failed to save quarantine changes: ' + err.message, true);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save Changes';
    }
}

function closeQuarantineModal() {
    document.getElementById('e2eQuarantineModal').classList.remove('visible');
}

// ============================================================================
// Unified Run View - Replaces separate Triage and Details modals
// ============================================================================

let unifiedRunData = null;  // Stores data for the current unified run view

/**
 * Show the unified run view for any E2E run.
 * This is the main entry point - called when clicking any run row.
 *
 * @param {number} runId - The E2E run ID to display
 */
async function showUnifiedRunView(runId) {
    // Use the diagnosis modal as the container
    const modal = document.getElementById('e2eDiagnosisModal');
    const content = document.getElementById('e2eDiagnosisContent');
    const modalTitle = modal.querySelector('.modal-header h2');

    // Show modal with loading state
    modalTitle.textContent = `E2E Run #${runId}`;
    content.innerHTML = '<div class="loading-spinner">Loading run details...</div>';
    modal.classList.add('visible');

    try {
        // Fetch enhanced run details with categories and history
        const res = await fetch(`/control/e2e/run/${runId}?repo_root=${encodeURIComponent(REPO_ROOT)}&enhanced=true`);
        const data = await res.json();

        if (!res.ok) {
            content.innerHTML = `<div style="color: #f85149; padding: 20px;">Error: ${escapeHtml(data.error || data.detail || 'Failed to load run details')}</div>`;
            return;
        }

        unifiedRunData = data;
        renderUnifiedRunView(data, runId);
    } catch (err) {
        content.innerHTML = `<div style="color: #f85149; padding: 20px;">Failed to load run details: ${escapeHtml(err.message)}</div>`;
    }
}

/**
 * Render the unified run view with tests grouped by category.
 */
function renderUnifiedRunView(data, runId) {
    const content = document.getElementById('e2eDiagnosisContent');
    const modalTitle = document.getElementById('e2eDiagnosisModal').querySelector('.modal-header h2');
    const run = data.run;
    const summary = data.summary;
    const tests = data.tests_by_category;

    // Update modal title with run info
    const runDate = run.started_at ? new Date(run.started_at).toLocaleString() : 'Unknown';
    modalTitle.textContent = `Run #${run.id} - ${runDate}`;

    // Build header with run info and summary
    let html = `
        <div class="unified-run-view">
        <div class="unified-run-header">
            <div class="run-meta">
                ${run.commit_sha ? `<span class="commit">Commit: <code>${run.commit_sha.substring(0, 7)}</code></span>` : ''}
                <span class="stat">${summary.total} tests</span>
                ${summary.passed > 0 ? `<span class="stat passed">${summary.passed} passed</span>` : ''}
                ${summary.untriaged + summary.has_issue > 0 ? `<span class="stat failed">${summary.untriaged + summary.has_issue} failed</span>` : ''}
            </div>
        </div>
    `;

    // Render each category section
    html += renderCategorySection('untriaged', 'UNTRIAGED', tests.untriaged,
        'Consistently failing tests with no GitHub issue',
        'warning');

    html += renderCategorySection('has_issue', 'HAS ISSUE', tests.has_issue,
        'Failing tests already tracked by a GitHub issue',
        'info');

    html += renderCategorySection('flaky', 'FLAKY', tests.flaky,
        'Unstable tests (flip rate > threshold) - passed OR failed this run',
        'flaky');

    html += renderCategorySection('fixed', 'FIXED', tests.fixed,
        'Passed this run but has an open issue that should be closed',
        'success');

    html += renderCategorySection('passed', 'PASSED', tests.passed,
        'Stable passing tests',
        'passed', true);  // collapsed by default

    if (tests.quarantined && tests.quarantined.length > 0) {
        html += renderCategorySection('quarantined', 'QUARANTINED', tests.quarantined,
            'Tests excluded from E2E failure counts',
            'quarantined', true);
    }

    if (tests.skipped && tests.skipped.length > 0) {
        html += renderCategorySection('skipped', 'SKIPPED', tests.skipped,
            'Tests that were skipped during this run',
            'skipped', true);
    }

    // Add bulk action bar for untriaged tests
    if (tests.untriaged && tests.untriaged.length > 0) {
        html += `
            <div class="bulk-action-bar">
                <span class="bulk-info">${tests.untriaged.length} untriaged test(s)</span>
                <div class="bulk-actions">
                    <select id="unifiedRunAgent" class="agent-select">
                        <option value="">Select agent...</option>
                        ${window.dashboardData.agents.map(a => `<option value="${a}">${a}</option>`).join('')}
                    </select>
                    <button class="btn-primary" onclick="createIssuesForUntriaged()">
                        Create Issues
                    </button>
                </div>
            </div>
        `;
    }

    // Close the unified-run-view wrapper
    html += '</div>';

    content.innerHTML = html;
}

/**
 * Render a category section with its tests.
 */
function renderCategorySection(categoryKey, title, tests, description, styleClass, collapsed = false) {
    if (!tests || tests.length === 0) return '';

    const isCollapsible = collapsed || tests.length > 5;
    const expanded = !collapsed;

    let html = `
        <div class="category-section ${categoryKey}" data-category="${categoryKey}">
            <div class="category-header" ${isCollapsible ? `onclick="toggleCategorySection('${categoryKey}')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleCategorySection('${categoryKey}');}" role="button" tabindex="0" aria-expanded="${expanded}"` : ''}>
                <span class="title">${title}</span>
                <span class="count">${tests.length}</span>
                ${isCollapsible ? `<span class="toggle-icon" id="toggle-${categoryKey}"></span>` : ''}
            </div>
            <div class="category-description">${description}</div>
            <div class="category-tests" id="tests-${categoryKey}" style="${collapsed ? 'display: none;' : ''}">
    `;

    for (const test of tests) {
        html += renderTestRow(test, categoryKey);
    }

    html += '</div></div>';
    return html;
}

/**
 * Render a single test row with inline history and actions.
 */
function renderTestRow(test, category) {
    const shortName = test.nodeid.split('::').pop();
    const effectiveOutcome = test.retry_outcome || test.outcome;
    const outcomeIcon = effectiveOutcome === 'passed' ? '✓' : effectiveOutcome === 'skipped' ? '○' : '✗';
    const outcomeClass = effectiveOutcome === 'passed' ? 'passed' : effectiveOutcome === 'skipped' ? 'skipped' : 'failed';

    // Build history icons from recent runs
    let historyHtml = '';
    if (test.history && test.history.length > 0) {
        const icons = test.history.map(h => {
            if (h.outcome === 'passed') return '<span class="hist-icon pass">✓</span>';
            if (h.outcome === 'failed') return '<span class="hist-icon fail">✗</span>';
            return '<span class="hist-icon skip">○</span>';
        }).reverse().join('');
        historyHtml = `<span class="test-history">${icons}</span>`;
    }

    // Build flip rate indicator for flaky tests
    let flipRateHtml = '';
    if (test.flip_rate_percent && test.flip_rate_percent > 0) {
        flipRateHtml = `<span class="flip-rate">${test.flip_rate_percent}%</span>`;
    }

    // Build duration
    const durationHtml = test.duration_seconds ? `<span class="duration">${test.duration_seconds.toFixed(1)}s</span>` : '';

    // Build issue link or action buttons based on category
    let actionsHtml = '';
    if (test.existing_issue) {
        const issueNum = test.existing_issue.number;
        const issueStatus = test.existing_issue.status;
        if (category === 'fixed' && issueStatus === 'open') {
            actionsHtml = `
                <a href="https://github.com/${window.dashboardData.githubOwner}/${window.dashboardData.githubRepo}/issues/${issueNum}"
                   target="_blank" class="issue-link-inline" onclick="event.stopPropagation();">
                    → #${issueNum} <span class="issue-status ${issueStatus}">${issueStatus}</span>
                </a>
                <div class="test-actions">
                    <button class="action-btn success" onclick="closeE2EIssue(${issueNum}, '${escapeAttr(test.nodeid)}'); event.stopPropagation();">
                        Close #${issueNum}
                    </button>
                </div>
            `;
        } else {
            actionsHtml = `
                <a href="https://github.com/${window.dashboardData.githubOwner}/${window.dashboardData.githubRepo}/issues/${issueNum}"
                   target="_blank" class="issue-link-inline" onclick="event.stopPropagation();">
                    → #${issueNum} <span class="issue-status ${issueStatus}">${issueStatus}</span>
                </a>
            `;
        }
    } else if (category === 'untriaged' || category === 'flaky') {
        actionsHtml = `
            <div class="test-actions">
                <button class="action-btn primary" onclick="showCreateIssueDropdown(this, '${escapeAttr(test.nodeid)}'); event.stopPropagation();">
                    Create Issue ▼
                </button>
                <button class="action-btn warning" onclick="quarantineSingleTest('${escapeAttr(test.nodeid)}'); event.stopPropagation();">
                    Quarantine
                </button>
                <button class="action-btn" onclick="copyTestErrorFromRun('${escapeAttr(test.nodeid)}'); event.stopPropagation();">
                    Copy Error
                </button>
            </div>
        `;
    }

    // Build the error preview (first 2 lines)
    let errorPreviewHtml = '';
    if (test.longrepr && (category === 'untriaged' || category === 'has_issue' || category === 'flaky')) {
        const lines = test.longrepr.split('\n');
        const preview = lines.slice(0, 2).join('\n');
        const hasMore = lines.length > 2;
        errorPreviewHtml = `
            <div class="test-error-preview" data-nodeid="${escapeAttr(test.nodeid)}">
                <pre class="error-text">${escapeHtml(preview)}</pre>
                ${hasMore ? `<button class="expand-btn" onclick="toggleTestError(this); event.stopPropagation();">Expand ▼</button>` : ''}
            </div>
        `;
    }

    return `
        <div class="test-row" data-nodeid="${escapeAttr(test.nodeid)}">
            <div class="test-row-main">
                <span class="status-icon ${outcomeClass}">${outcomeIcon}</span>
                <span class="test-name" title="${escapeHtml(test.nodeid)}">${escapeHtml(shortName)}</span>
                ${historyHtml}
                ${flipRateHtml}
                ${durationHtml}
                ${actionsHtml}
            </div>
            ${errorPreviewHtml}
        </div>
    `;
}

/**
 * Toggle a category section's visibility.
 */
function toggleCategorySection(categoryKey) {
    const testsDiv = document.getElementById(`tests-${categoryKey}`);
    const toggleSpan = document.getElementById(`toggle-${categoryKey}`);
    const section = document.querySelector(`.category-section[data-category="${categoryKey}"]`);
    const header = section?.querySelector('.category-header');
    if (!testsDiv || !toggleSpan) return;

    const isCollapsed = testsDiv.classList.contains('collapsed');
    testsDiv.classList.toggle('collapsed');
    toggleSpan.textContent = isCollapsed ? '▼' : '▶';
    if (header) {
        header.setAttribute('aria-expanded', isCollapsed ? 'true' : 'false');
    }
}

/**
 * Toggle error preview/full view.
 */
function toggleTestError(button) {
    const preview = button.closest('.test-error-preview');
    if (!preview) return;

    const isExpanded = preview.classList.contains('expanded');
    const nodeid = preview.dataset.nodeid;

    if (isExpanded) {
        // Collapse: show first 2 lines
        preview.classList.remove('expanded');
        button.textContent = 'Expand ▼';
        const errorText = preview.querySelector('.error-text');
        if (errorText && unifiedRunData) {
            // Find the test and show preview
            for (const category of Object.values(unifiedRunData.tests_by_category)) {
                const test = category.find(t => t.nodeid === nodeid);
                if (test && test.longrepr) {
                    const lines = test.longrepr.split('\n');
                    errorText.textContent = lines.slice(0, 2).join('\n');
                    break;
                }
            }
        }
    } else {
        // Expand: show full error
        preview.classList.add('expanded');
        button.textContent = 'Collapse ▲';
        const errorText = preview.querySelector('.error-text');
        if (errorText && unifiedRunData) {
            // Find the test and show full error
            for (const category of Object.values(unifiedRunData.tests_by_category)) {
                const test = category.find(t => t.nodeid === nodeid);
                if (test && test.longrepr) {
                    errorText.textContent = test.longrepr;
                    break;
                }
            }
        }
    }
}

/**
 * Copy error text for a specific test.
 */
function copyTestErrorFromRun(nodeid) {
    if (!unifiedRunData) return;

    // Find the test in any category
    for (const category of Object.values(unifiedRunData.tests_by_category)) {
        const test = category.find(t => t.nodeid === nodeid);
        if (test) {
            const text = `Test: ${test.nodeid}\n\nError:\n${test.longrepr || 'No error details'}`;
            navigator.clipboard.writeText(text).then(
                () => showToast('Error copied to clipboard'),
                () => showToast('Failed to copy', true)
            );
            return;
        }
    }
}

/**
 * Create issues for all untriaged tests.
 */
async function createIssuesForUntriaged() {
    if (!unifiedRunData) return;

    const agent = document.getElementById('unifiedRunAgent')?.value;
    if (!agent) {
        showToast('Please select an agent', true);
        return;
    }

    const untriaged = unifiedRunData.tests_by_category.untriaged || [];
    if (untriaged.length === 0) {
        showToast('No untriaged tests', true);
        return;
    }

    const nodeids = untriaged.map(t => t.nodeid);

    try {
        const res = await fetch(`/control/e2e/create-issues/${unifiedRunData.run.id}?repo_root=${encodeURIComponent(REPO_ROOT)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nodeids, agent }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to create issues', true);
            return;
        }

        showToast(`Created parent issue #${data.parent_issue.number} with ${data.sub_issues.length} sub-issue(s)`);

        // Refresh the view
        showUnifiedRunView(unifiedRunData.run.id);

        // Open parent issue in new tab
        if (data.parent_issue.url) {
            window.open(data.parent_issue.url, '_blank');
        }
    } catch (err) {
        showToast('Failed to create issues: ' + err.message, true);
    }
}

/**
 * Close an E2E failure issue that has been fixed.
 */
async function closeE2EIssue(issueNumber, nodeid) {
    if (!confirm(`Close issue #${issueNumber}? The test "${nodeid.split('::').pop()}" is now passing.`)) {
        return;
    }

    try {
        const res = await fetch(`/control/e2e/close-issue/${issueNumber}?repo_root=${encodeURIComponent(REPO_ROOT)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nodeid }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || 'Failed to close issue', true);
            return;
        }

        showToast(`Closed issue #${issueNumber}`);

        // Refresh the view
        if (unifiedRunData) {
            showUnifiedRunView(unifiedRunData.run.id);
        }
    } catch (err) {
        showToast('Failed to close issue: ' + err.message, true);
    }
}

/**
 * Show dropdown for creating a single issue with agent selection.
 */
function showCreateIssueDropdown(button, nodeid) {
    // If dropdown already exists, toggle it
    let dropdown = button.nextElementSibling;
    if (dropdown && dropdown.classList.contains('create-issue-dropdown')) {
        dropdown.remove();
        return;
    }

    // Remove any other open dropdowns
    document.querySelectorAll('.create-issue-dropdown').forEach(d => d.remove());

    // Create dropdown
    dropdown = document.createElement('div');
    dropdown.className = 'create-issue-dropdown';
    dropdown.innerHTML = `
        <div class="dropdown-content">
            ${window.dashboardData.agents.map(a => `
                <button class="dropdown-item" onclick="createSingleIssueWithAgent('${escapeAttr(nodeid)}', '${a}'); event.stopPropagation();">
                    ${a}
                </button>
            `).join('')}
        </div>
    `;
    button.parentNode.insertBefore(dropdown, button.nextSibling);

    // Close dropdown when clicking elsewhere
    const closeHandler = (e) => {
        if (!dropdown.contains(e.target) && e.target !== button) {
            dropdown.remove();
            document.removeEventListener('click', closeHandler);
        }
    };
    setTimeout(() => document.addEventListener('click', closeHandler), 0);
}

/**
 * Create a single issue with specified agent.
 */
async function createSingleIssueWithAgent(nodeid, agent) {
    if (!unifiedRunData) return;

    try {
        const res = await fetch(`/control/e2e/create-issues/${unifiedRunData.run.id}?repo_root=${encodeURIComponent(REPO_ROOT)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nodeids: [nodeid], agent }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to create issue', true);
            return;
        }

        const testName = nodeid.split('::').pop();
        showToast(`Created issue #${data.parent_issue.number} for ${testName}`);

        // Close dropdown
        document.querySelectorAll('.create-issue-dropdown').forEach(d => d.remove());

        // Refresh the view
        showUnifiedRunView(unifiedRunData.run.id);

        // Open issue in new tab
        if (data.parent_issue.url) {
            window.open(data.parent_issue.url, '_blank');
        }
    } catch (err) {
        showToast('Failed to create issue: ' + err.message, true);
    }
}
