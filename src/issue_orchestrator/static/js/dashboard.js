// Helper to hide settings menu (used by multiple functions)
function hideSettingsMenu() {
    const menu = document.getElementById('settingsMenu');
    if (menu) menu.classList.remove('visible');
}

let terminalBackend = 'tmux';
let clientCapabilities = {
    focus_session: false,
    open_path: false,
    reveal_worktree: false,
    local_server_paths_only: true,
    host_platform: 'unknown',
};
let currentCommitSha = null;
let viewModel = null;
const issueRefreshInFlight = new Set();
const issueRefreshLastAttempt = new Map();
let flowRefreshObserver = null;
let networkSyncTimer = null;
let currentDiagnosticsRunDir = null;
const flowRefreshPrefsModal = document.getElementById('flowRefreshPrefsModal');
const FLOW_REFRESH_OVERRIDE_KEY = 'issue-orchestrator.flow-refresh.override.v1';
const NETWORK_SYNC_OVERRIDE_KEY = 'issue-orchestrator.network-sync.override.v1';
const GH_USAGE_UI_PREF_KEY = 'issue-orchestrator.github-usage.ui.v1';
const FLOW_FRESHNESS_PRESETS = {
    aggressive: { enabled: true, staleSeconds: 180, cooldownSeconds: 30 },
    balanced: { enabled: true, staleSeconds: 900, cooldownSeconds: 120 },
    economy: { enabled: true, staleSeconds: 3600, cooldownSeconds: 300 },
};
const FLOW_BUDGET_MULTIPLIER = { low: 1.7, medium: 1.0, high: 0.6 };
const issueRowState = window.issueRowState;
const expandedColumnState = window.expandedColumnState;
const compactCardState = window.compactCardState;
const uiActionContract = window.uiActionContract;
if (!issueRowState) {
    throw new Error('issueRowState helper not loaded');
}
if (!expandedColumnState) {
    throw new Error('expandedColumnState helper not loaded');
}
if (!compactCardState) {
    throw new Error('compactCardState helper not loaded');
}
if (!uiActionContract) {
    throw new Error('uiActionContract helper not loaded');
}

function applyDashboardTheme(theme) {
    // When embedded in CC iframe, honor ?theme= param or postMessage from parent
    const urlTheme = new URLSearchParams(window.location.search).get('theme');
    const storedTheme = theme || urlTheme || localStorage.getItem('theme') || 'system';
    let effectiveTheme = storedTheme;
    if (storedTheme === 'system') {
        effectiveTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    document.documentElement.setAttribute('data-theme', effectiveTheme);
}

function navigateBackToRepositories() {
    window.parent.postMessage({ type: 'cc-back-to-repos' }, '*');
}

// When embedded in CC iframe, hide dashboard header and show embedded header in tab bar
const isEmbedded = new URLSearchParams(window.location.search).get('embedded') === '1';
if (isEmbedded) {
    document.addEventListener('DOMContentLoaded', () => {
        // Hide standalone header (dashboard owns the header via tab bar now)
        const header = document.querySelector('header');
        if (header) header.style.display = 'none';
        // Hide scope-summary by default (toggled via (i) button as dropdown)
        const scopeSummary = document.querySelector('.scope-summary');
        if (scopeSummary) scopeSummary.classList.add('scope-embedded');
        // Show embedded header elements in tab bar
        document.querySelectorAll('.embedded-back, .embedded-repo, .embedded-badge, .embedded-scope-btn, .embedded-sep').forEach(el => {
            el.style.display = '';
        });
        // Populate repo name from server-rendered data
        const repoEl = document.getElementById('embeddedRepoName');
        if (repoEl) repoEl.textContent = window.dashboardData?.repo || '';
        // Back button: repositories in normal mode, collapse in expanded mode
        document.getElementById('embeddedBack')?.addEventListener('click', () => {
            const expanded = document.querySelector('.kanban-column.expanded[data-expanded="true"]');
            const columnId = expanded?.dataset?.column;
            if (columnId) {
                toggleColumnExpand(columnId);
            } else {
                navigateBackToRepositories();
            }
        });
        // (i) scope button → toggle scope-summary as a dropdown below tab bar
        document.getElementById('embeddedScopeBtn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            const scope = document.querySelector('.scope-summary');
            if (scope) scope.classList.toggle('scope-open');
        });
        // Click anywhere (including inside scope) closes scope, except the (i) button itself
        document.addEventListener('click', (e) => {
            const scope = document.querySelector('.scope-summary');
            if (scope?.classList.contains('scope-open') && !e.target.closest('.embedded-scope-btn')) {
                scope.classList.remove('scope-open');
            }
        });
        updateEmbeddedBackButtonVisibility();
    });
}

function updateEmbeddedBackButtonVisibility() {
    if (!isEmbedded) return;
    const back = document.getElementById('embeddedBack');
    const label = document.getElementById('embeddedBackLabel');
    if (!back || !label) return;
    const hasExpandedColumn = Boolean(document.querySelector('.kanban-column.expanded[data-expanded="true"]'));
    back.style.display = '';
    label.textContent = hasExpandedColumn ? 'Back to dashboard' : 'Back to repositories';
    back.setAttribute('aria-label', hasExpandedColumn ? 'Back to dashboard' : 'Back to repositories');
}

// Listen for messages from parent (CC iframe embedding)
window.addEventListener('message', (event) => {
    if (!event.data?.type) return;
    switch (event.data.type) {
        case 'theme':
            applyDashboardTheme(event.data.theme);
            break;
        case 'cc-refresh-from-github':
            refreshFromGitHub();
            break;
        case 'cc-open-flow-prefs':
            openFlowRefreshPrefs();
            break;
        case 'cc-repo-info':
            // Parent CC sends repo display name and config info
            if (event.data.repoName) {
                const el = document.getElementById('embeddedRepoName');
                if (el) el.textContent = event.data.repoName;
            }
            break;
    }
});
fetch('/api/info')
    .then(res => res.json())
    .then(data => {
        if (data.terminal_backend) {
            terminalBackend = data.terminal_backend;
        }
        if (data.client_capabilities) {
            clientCapabilities = {
                ...clientCapabilities,
                ...data.client_capabilities,
            };
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
            hint = clientCapabilities.focus_session
                ? 'Focus terminal session'
                : 'View agent log';
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
    if (!vm) return;

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

    // Update all status badges (standalone header + embedded tab bar)
    document.querySelectorAll('.status-badge').forEach(badge => {
        badge.textContent = text;
        badge.className = className;
    });
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
    if (vm.startup_status && vm.startup_status !== 'complete') {
        const msg = vm.startup_message || 'Starting up...';
        return '<div class="startup-loading">' +
            '<div class="startup-spinner"></div>' +
            '<span class="startup-loading-text">' + msg + '</span>' +
            '</div>';
    }
    if (vm.active_tab === 'history') {
        return 'No session history yet';
    }
    if (vm.active_tab === 'attention') {
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

async function refreshIssueRows(vm, rowsOverride = null) {
    const list = document.getElementById('issueList');
    if (!list) return;

    let rows = rowsOverride;
    if (!Array.isArray(rows)) {
        const url = new URL('/api/issue-rows', window.location.origin);
        const params = new URL(window.location.href).searchParams;
        if (params.get('tab')) url.searchParams.set('tab', params.get('tab'));
        if (params.get('page')) url.searchParams.set('page', params.get('page'));
        if (params.get('e2e_page')) url.searchParams.set('e2e_page', params.get('e2e_page'));

        const res = await fetch(url.toString());
        if (!res.ok) return;
        const data = await res.json();
        rows = data.rows || [];
    }

    const nextIds = new Set(rows.map(row => String(row.issue_number)));
    const existingGroups = Array.from(list.querySelectorAll('.issue-row-group[data-issue]'));
    const existingById = new Map(existingGroups.map((group) => [group.dataset.issue, group]));
    existingGroups.forEach(group => {
        if (!nextIds.has(group.dataset.issue)) {
            group.remove();
        }
    });

    const header = list.querySelector('.issue-header');
    let insertAfter = header;
    rows.forEach(row => {
        const id = String(row.issue_number);
        const existing = existingById.get(id) || null;
        const nextFingerprint = issueRowState.computeIssueRowFingerprint(row);

        let node = existing;
        const shouldReplace = !existing || existing.dataset.rowFingerprint !== nextFingerprint;
        if (shouldReplace) {
            const wrapper = document.createElement('div');
            wrapper.innerHTML = row.html.trim();
            const newNode = wrapper.firstElementChild;
            if (!newNode) {
                return;
            }
            newNode.dataset.rowFingerprint = nextFingerprint;
            if (existing) {
                existing.replaceWith(newNode);
            }
            node = newNode;
        } else if (node) {
            node.dataset.rowFingerprint = nextFingerprint;
        }

        if (!node) {
            return;
        }

        if (node.previousElementSibling !== insertAfter) {
            insertAfter.after(node);
        }
        insertAfter = node;
    });

    ensureEmptyState(vm, rows.length > 0);
    updateActionHints();
    initVisibilityObserver();
    initFlowLazyVisibleRefresh();
}

async function refreshViewModel({ reloadOnListChange = true } = {}) {
    try {
        const endpoint = reloadOnListChange ? '/api/view-model-snapshot' : '/api/view-model';
        const url = new URL(endpoint, window.location.origin);
        const params = new URL(window.location.href).searchParams;
        if (params.get('tab')) url.searchParams.set('tab', params.get('tab'));
        if (params.get('page')) url.searchParams.set('page', params.get('page'));
        if (params.get('e2e_page')) url.searchParams.set('e2e_page', params.get('e2e_page'));

        const res = await fetch(url.toString());
        if (!res.ok) return;
        const payload = await res.json();
        if (reloadOnListChange) {
            if (!payload.view_model || !Array.isArray(payload.rows)) {
                throw new Error('Invalid /api/view-model-snapshot payload shape');
            }
            viewModel = payload.view_model;
        } else {
            viewModel = payload;
        }
        window.dashboardData = viewModel.dashboard_data || window.dashboardData;
        isPaused = !!viewModel.paused;
        updateStatusBadgeFromViewModel(viewModel);
        updatePauseMenuFromViewModel(viewModel);
        updateRefreshStatusFromViewModel(viewModel);
        applyNetworkSyncScheduler();
        renderGitHubUsage();

        // Post status to parent CC when embedded
        if (isEmbedded && viewModel) {
            const usage = window.dashboardData?.githubUsage || {};
            const rate = usage.last_rate_limit_from_headers || {};
            const refresh = window.dashboardData?.refresh || {};
            const scope = window.dashboardData?.scope || {};
            window.parent.postMessage({
                type: 'dashboard-status',
                payload: {
                    ghUsage: {
                        callsPerMinute: Number(usage.calls_per_minute || 0),
                        totalCalls: Number(usage.total_calls || 0),
                        remaining: Number(rate.remaining || 0),
                        limit: Number(rate.limit || 0),
                    },
                    refresh: {
                        lastRefreshLabel: refresh.lastRefreshLabel || 'unknown',
                        lastRefreshAt: Number(refresh.lastRefreshAt || 0),
                        inProgress: Boolean(refresh.inProgress),
                        requested: Boolean(refresh.requested),
                    },
                    scope: {
                        repo: window.dashboardData?.repo || '',
                        inScopeTotal: Number(scope.in_scope_total || 0),
                        filterMilestones: scope.filter_milestones || [],
                        filterLabel: scope.filter_label || '',
                        excludeLabels: scope.exclude_labels || [],
                    },
                    counts: {
                        queued: Number(viewModel.queue_count || 0),
                        running: Number(viewModel.active_count || 0),
                        blocked: Number(viewModel.blocked_count || 0),
                        awaitingMerge: Number(viewModel.awaiting_merge_count || 0),
                        completed: Number(viewModel.completed_count || 0),
                    },
                    paused: Boolean(viewModel.paused),
                    shutdownRequested: Boolean(viewModel.shutdown_requested),
                    startupStatus: viewModel.startup_status || '',
                },
            }, '*');
        }

        // Update kanban column counts and compact cards from fresh view model
        if (viewModel.flow_columns) {
            for (const col of viewModel.flow_columns) {
                const colEl = document.querySelector(`[data-column="${col.id}"]`);
                if (!colEl) continue;
                const countEl = colEl.querySelector('.count');
                const visibleItems = filterSuppressedItems(col.items || [], col.id);
                if (countEl) countEl.textContent = visibleItems.length;

                // Rebuild compact cards (skip if column is expanded — it has its own refresh)
                if (colEl.dataset.expanded !== 'true') {
                    const cardsEl = colEl.querySelector('.column-cards');
                    if (cardsEl) renderCompactCards(cardsEl, visibleItems);
                }
            }
        }

        // If a column is expanded, refresh its content
        const expandedCol = document.querySelector('.kanban-column.expanded');
        if (expandedCol) {
            loadExpandedColumn(expandedCol.dataset.column, { viewModel });
        }

        if (reloadOnListChange && viewModel.startup_status === 'complete') {
            await refreshIssueRows(viewModel, payload.rows);
        } else if (viewModel.startup_status && viewModel.startup_status !== 'complete') {
            ensureEmptyState(viewModel, false);
        }
    } catch (e) {
        console.error('Failed to refresh view-model:', e);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    applyDashboardTheme();
    updateActionHints();
    initFlowLazyVisibleRefresh();
    applyGitHubUsagePrefs();
    renderGitHubUsage();
    applyNetworkSyncScheduler();
    refreshViewModel({ reloadOnListChange: false });
    initVisibilityObserver();
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

document.addEventListener('change', (event) => {
    if (event.target?.id === 'flowRefreshOverrideEnabled') {
        setFlowRefreshInputsEnabled(Boolean(event.target.checked));
    }
    if (event.target?.id === 'networkSyncCadenceSource' || event.target?.id === 'saveCadenceToConfig') {
        updateCadencePreferenceInputState();
    }
});
let visibilityObserver = null;
let visibilityPostTimer = null;
let lastVisibilityPayload = '';

function initVisibilityObserver() {
    if (visibilityObserver) {
        visibilityObserver.disconnect();
        visibilityObserver = null;
    }
    const enabled = Boolean(window.dashboardData && window.dashboardData.fetchLayerVisibilityAwareEnabled);
    if (!enabled) return;
    const cards = Array.from(document.querySelectorAll('.issue-card[data-issue]'));
    if (!cards.length) return;

    const visibleNumbers = new Set();
    visibilityObserver = new IntersectionObserver((entries) => {
        let changed = false;
        entries.forEach((entry) => {
            const issueNumber = parseInt(entry.target.dataset.issue || '0', 10);
            if (!issueNumber) return;
            if (entry.isIntersecting) {
                if (!visibleNumbers.has(issueNumber)) {
                    visibleNumbers.add(issueNumber);
                    changed = true;
                }
            } else if (visibleNumbers.delete(issueNumber)) {
                changed = true;
            }
        });
        if (changed) {
            scheduleVisibilityPost(Array.from(visibleNumbers));
        }
    }, { threshold: 0.25 });

    cards.forEach((card) => visibilityObserver.observe(card));
}

function scheduleVisibilityPost(visibleIssueNumbers) {
    const normalized = Array.from(new Set(visibleIssueNumbers.map(n => parseInt(n, 10)).filter(Boolean))).sort((a, b) => a - b);
    const payload = JSON.stringify(normalized);
    if (payload === lastVisibilityPayload) return;
    if (visibilityPostTimer) window.clearTimeout(visibilityPostTimer);
    visibilityPostTimer = window.setTimeout(() => {
        postVisibility(visibleIssueNumbers);
    }, 600);
}

async function postVisibility(visibleIssueNumbers) {
    const normalized = Array.from(new Set(visibleIssueNumbers.map(n => parseInt(n, 10)).filter(Boolean))).sort((a, b) => a - b);
    const payload = JSON.stringify(normalized);
    if (payload === lastVisibilityPayload) return;
    try {
        const res = await fetch('/api/refresh/visibility', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ issues: normalized }),
        });
        if (res.ok) {
            lastVisibilityPayload = payload;
        }
    } catch (err) {
        console.error('Failed to post visibility hint:', err);
    }
}

let logPoller = null;
let logFollow = true;
let logIssue = null;
let logRunDir = null;
let logRecordingContext = null;
let sessionReplayState = null;

function clearDiagnosticsActionMessage() {
    const msg = document.getElementById('diagActionMessage');
    if (!msg) return;
    msg.textContent = '';
    msg.style.display = 'none';
}

function showDiagnosticsActionMessage(message) {
    const msg = document.getElementById('diagActionMessage');
    if (!msg) {
        showToast(message, 'error');
        return;
    }
    msg.textContent = String(message || 'Action failed');
    msg.style.display = 'block';
}

function reportActionError(message, surface = 'toast') {
    if (surface === 'inline') {
        showDiagnosticsActionMessage(message);
        return;
    }
    showToast(message, 'error');
}

function isNearBottom(element, threshold = 24) {
    return element.scrollTop + element.clientHeight >= element.scrollHeight - threshold;
}

async function refreshAgentLog(issueNumber, forceScroll = false, runDir = null) {
    const effectiveRunDir = runDir || logRunDir;
    if (!effectiveRunDir) {
        const msg = 'Session recording requires a run-scoped action (missing run_dir).';
        const statusEl = document.getElementById('sessionReplayStatus');
        if (statusEl) statusEl.textContent = msg;
        return;
    }
    const request = uiActionContract.buildTerminalRecordingRequest(issueNumber, effectiveRunDir, {
        offset: sessionReplayState ? sessionReplayState.events.length : 0,
        limit: 0,
        round_index: sessionReplayState && sessionReplayState.recordingContext
            ? sessionReplayState.recordingContext.round_index
            : null,
        session_role: sessionReplayState && sessionReplayState.recordingContext
            ? sessionReplayState.recordingContext.session_role
            : null,
    });
    const res = await fetch(request.endpoint, { method: request.method });
    const data = await res.json().catch(() => ({}));

    if (data.error) {
        const statusEl = document.getElementById('sessionReplayStatus');
        if (statusEl) statusEl.textContent = data.error;
        return;
    }

    if (!sessionReplayState || sessionReplayState.issueNumber !== issueNumber || sessionReplayState.runDir !== effectiveRunDir) {
        return;
    }
    const incomingEvents = Array.isArray(data.events) ? data.events : [];
    if (!sessionReplayState.initialGeometry) {
        sessionReplayState.initialGeometry = resolveSessionReplayInitialGeometry(data, incomingEvents);
    }
    if (incomingEvents.length > 0) {
        const wasAtEnd = sessionReplayState.playbackIndex >= sessionReplayState.events.length;
        sessionReplayState.events.push(...incomingEvents);
        if (sessionReplayState.follow && (forceScroll || wasAtEnd) && !sessionReplayState.playing) {
            replaySessionToIndex(sessionReplayState.events.length);
        }
    }
    const recordingPathEl = document.getElementById('sessionReplayPath');
    if (recordingPathEl) recordingPathEl.textContent = data.recording_path || '';
    updateSessionReplayUi();
}

async function openAgentLog(issueNumber, logLabel = 'Session Recording', runDir = null, errorSurface = 'toast', context = null) {
    if (!runDir) {
        reportActionError('Session recording requires run context. Open from a timeline entry.', errorSurface);
        return;
    }
    modalOverlay.querySelector('.modal').classList.remove('diagnostics-modal');
    clearDiagnosticsActionMessage();
    logIssue = issueNumber;
    logRunDir = runDir;
    logRecordingContext = context && (context.round_index || context.session_role) ? {
        round_index: Number.isInteger(Number(context.round_index)) ? Number(context.round_index) : null,
        session_role: context.session_role ? String(context.session_role).trim() : null,
    } : null;
    const request = uiActionContract.buildTerminalRecordingRequest(issueNumber, runDir, {
        offset: 0,
        limit: 0,
        round_index: logRecordingContext ? logRecordingContext.round_index : null,
        session_role: logRecordingContext ? logRecordingContext.session_role : null,
    });
    const res = await fetch(request.endpoint, { method: request.method });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) {
        reportActionError(data.error || `Failed to load session recording (HTTP ${res.status})`, errorSurface);
        return;
    }

    const logContent = `
        <div class="session-replay-shell">
            <div class="session-replay-toolbar">
                <div class="session-replay-toolbar-main">
                    <button class="issue-action-btn" id="sessionReplayRestart">Replay</button>
                    <button class="issue-action-btn" id="sessionReplayPlayPause">Play</button>
                    <button class="issue-action-btn" id="sessionReplayJumpLive">Jump to latest</button>
                    <button class="issue-action-btn" id="sessionReplayRefresh">Refresh</button>
                </div>
                <div class="session-replay-toolbar-meta">
                    <label class="session-replay-control">
                        Speed
                        <select id="sessionReplaySpeed">
                            <option value="0.5">0.5x</option>
                            <option value="1" selected>1x</option>
                            <option value="2">2x</option>
                            <option value="4">4x</option>
                        </select>
                    </label>
                    <label class="session-replay-control">
                        <input type="checkbox" id="logFollowToggle" checked>
                        Follow live
                    </label>
                    <span class="session-replay-status" id="sessionReplayStatus"></span>
                </div>
            </div>
            <div class="session-replay-progress">
                <input class="session-replay-seek" type="range" id="sessionReplaySeek" min="0" max="0" value="0" step="1">
                <span class="session-replay-progress-text" id="sessionReplayProgressText">0 / 0 events</span>
                <span class="session-replay-meta" id="sessionReplayClock">0.0s</span>
            </div>
            <div class="session-replay-terminal-wrap">
                <div id="sessionReplayTerminal" class="session-replay-terminal"></div>
            </div>
            <div class="session-replay-hint">Raw run-scoped terminal replay rendered in an emulator. Use Replay for after-the-fact inspection; keep Follow live on during active runs.</div>
            <div class="session-replay-prompt">
                <details>
                    <summary>Prompt</summary>
                    <div id="logPromptMeta" class="session-replay-meta"></div>
                    <pre id="logPromptPre"></pre>
                </details>
            </div>
            <div class="session-replay-meta">Recording: <span id="sessionReplayPath"></span></div>
        </div>
    `;

    document.getElementById('modalTitle').textContent = `${logLabel} #${issueNumber}`;
    document.getElementById('modalBody').innerHTML = logContent;
    document.getElementById('modalOverlay').classList.add('visible');

    initializeSessionReplay(issueNumber, runDir, data);

    const toggle = document.getElementById('logFollowToggle');
    if (toggle) {
        toggle.addEventListener('change', (e) => {
            logFollow = e.target.checked;
            if (sessionReplayState) {
                sessionReplayState.follow = logFollow;
            }
            updateSessionReplayUi();
        });
    }
    document.getElementById('sessionReplayRestart')?.addEventListener('click', () => restartSessionReplay(true));
    document.getElementById('sessionReplayPlayPause')?.addEventListener('click', () => toggleSessionReplayPlayback());
    document.getElementById('sessionReplayJumpLive')?.addEventListener('click', () => jumpSessionReplayToLatest());
    document.getElementById('sessionReplayRefresh')?.addEventListener('click', () => refreshAgentLog(issueNumber, true, runDir));
    document.getElementById('sessionReplaySeek')?.addEventListener('input', (event) => {
        pauseSessionReplay();
        const nextIndex = Number(event.target.value || 0);
        replaySessionToIndex(nextIndex);
    });
    document.getElementById('sessionReplaySpeed')?.addEventListener('change', (event) => {
        if (!sessionReplayState) return;
        sessionReplayState.speed = Number(event.target.value || 1) || 1;
        updateSessionReplayUi();
        if (sessionReplayState.playing) {
            scheduleSessionReplayStep();
        }
    });
    window.addEventListener('resize', fitSessionReplayTerminal);

    await refreshInlineSessionPrompt(issueNumber, runDir);
    if (logPoller) {
        clearInterval(logPoller);
    }
    logPoller = setInterval(() => {
        refreshAgentLog(issueNumber, false, logRunDir);
    }, 2000);
}

function openAgentLogAction(issueNumber, runDir = null, logLabel = 'Session Recording', errorSurface = 'toast', context = null) {
    return openAgentLog(issueNumber, logLabel, runDir, errorSurface, context);
}

async function openReviewTranscript(issueNumber, runDir = null, context = null, errorSurface = 'toast') {
    if (!runDir) {
        const message = 'Review transcript requires run-scoped context.';
        if (errorSurface === 'inline') {
            openModal(`Review Transcript #${issueNumber}`, `<p>${escapeHtml(message)}</p>`);
        } else {
            showToast(message, true);
        }
        return;
    }
    try {
        const params = new URLSearchParams({ run_dir: String(runDir) });
        const effectiveRound = Number(context && context.round_index);
        if (Number.isInteger(effectiveRound) && effectiveRound > 0) {
            params.set('round_index', String(effectiveRound));
        }
        const effectiveRole = context && context.transcript_role
            ? String(context.transcript_role).trim()
            : '';
        if (effectiveRole) {
            params.set('transcript_role', effectiveRole);
        }
        const res = await fetch(`/api/session/review-transcript/${issueNumber}?${params.toString()}`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
            const message = data.error || `Review transcript unavailable (HTTP ${res.status})`;
            if (errorSurface === 'inline') {
                openModal(`Review Transcript #${issueNumber}`, `<p>${escapeHtml(message)}</p>`);
            } else {
                showToast(message, true);
            }
            return;
        }
        const meta = data.transcript_path
            ? `<div class="session-replay-note">Transcript: ${escapeHtml(data.transcript_path)}</div>`
            : '';
        const content = typeof data.content === 'string' && data.content.length > 0
            ? escapeHtml(data.content)
            : '(empty)';
        const scopeLabel = typeof data.scope_label === 'string' && data.scope_label.trim()
            ? ` — ${escapeHtml(data.scope_label)}`
            : '';
        openModal(`Review Transcript #${data.issue_number}${scopeLabel}`, `${meta}<pre>${content}</pre>`);
    } catch (err) {
        const message = `Failed to load review transcript: ${err instanceof Error ? err.message : String(err)}`;
        if (errorSurface === 'inline') {
            openModal(`Review Transcript #${issueNumber}`, `<p>${escapeHtml(message)}</p>`);
        } else {
            showToast(message, true);
        }
    }
}

async function copyAgentLogAction(issueNumber, runDir = null) {
    if (!runDir) {
        showToast('No run-scoped session recording is available to copy', true);
        return;
    }
    try {
        const request = uiActionContract.buildTerminalRecordingRequest(issueNumber, runDir, { offset: 0, limit: 0 });
        const res = await fetch(request.endpoint, { method: request.method });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
            throw new Error(data.error || `HTTP ${res.status}`);
        }
        const text = extractPlainTextFromRecordingEvents(Array.isArray(data.events) ? data.events : []);
        if (!text.trim()) {
            showToast('Session recording is empty', true);
            return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            showToast('Session recording copied');
            return;
        }
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(textarea);
        showToast(ok ? 'Session recording copied' : 'Failed to copy', !ok);
    } catch (err) {
        showToast(`Failed to copy session recording: ${err instanceof Error ? err.message : String(err)}`, true);
    }
}

function initializeSessionReplay(issueNumber, runDir, payload) {
    destroySessionReplay();
    const events = Array.isArray(payload.events) ? payload.events : [];
    const initialGeometry = resolveSessionReplayInitialGeometry(payload, events);
    sessionReplayState = {
        issueNumber,
        runDir,
        events,
        initialGeometry,
        recordingContext: logRecordingContext,
        playbackIndex: 0,
        playing: false,
        playTimer: null,
        speed: 1,
        follow: true,
        terminal: null,
        fitAddon: null,
    };
    logFollow = true;
    const pathEl = document.getElementById('sessionReplayPath');
    if (pathEl) pathEl.textContent = payload.recording_path || '';
    const terminalHost = document.getElementById('sessionReplayTerminal');
    if (!terminalHost) return;
    createSessionReplayTerminal();
    replaySessionToIndex(events.length);
}

function resolveSessionReplayInitialGeometry(payload, events) {
    const payloadGeometry = normalizeSessionReplayGeometry(payload?.initial_geometry);
    if (payloadGeometry) {
        return payloadGeometry;
    }
    for (const event of events || []) {
        const eventGeometry = normalizeSessionReplayGeometry(event);
        if (eventGeometry) {
            return eventGeometry;
        }
    }
    return null;
}

function normalizeSessionReplayGeometry(candidate) {
    if (!candidate || typeof candidate !== 'object') return null;
    const rows = Number(candidate.rows);
    const cols = Number(candidate.cols);
    if (!Number.isInteger(rows) || !Number.isInteger(cols) || rows <= 0 || cols <= 0) {
        return null;
    }
    return { rows, cols };
}

function createSessionReplayTerminal() {
    const host = document.getElementById('sessionReplayTerminal');
    if (!host || !sessionReplayState) return;
    if (sessionReplayState.terminal) {
        sessionReplayState.terminal.dispose();
    }
    host.innerHTML = '';
    const terminalOptions = {
        convertEol: false,
        cursorBlink: false,
        disableStdin: true,
        fontFamily: '"SFMono-Regular", "Menlo", "Consolas", monospace',
        fontSize: 12,
        scrollback: 10000,
        theme: {
            background: '#08111c',
            foreground: '#d7e2ef',
            cursor: '#4ea1ff',
            black: '#08111c',
            brightBlack: '#5b6f87',
            red: '#e57878',
            green: '#46c37b',
            yellow: '#f0b24f',
            blue: '#4ea1ff',
            magenta: '#9db4ff',
            cyan: '#62d5f5',
            white: '#d7e2ef',
            brightWhite: '#ffffff',
        },
    };
    if (sessionReplayState.initialGeometry) {
        terminalOptions.rows = sessionReplayState.initialGeometry.rows;
        terminalOptions.cols = sessionReplayState.initialGeometry.cols;
    }
    const terminal = new Terminal(terminalOptions);
    const fitAddon = new FitAddon.FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(host);
    sessionReplayState.terminal = terminal;
    sessionReplayState.fitAddon = fitAddon;
    fitSessionReplayTerminal();
}

function fitSessionReplayTerminal() {
    if (!sessionReplayState || !sessionReplayState.fitAddon) return;
    if (sessionReplayState.initialGeometry) return;
    try {
        sessionReplayState.fitAddon.fit();
    } catch (_err) {
        // Ignore fit errors while the modal is still laying out.
    }
}

function destroySessionReplay() {
    if (!sessionReplayState) return;
    if (sessionReplayState.playTimer) {
        clearTimeout(sessionReplayState.playTimer);
    }
    if (sessionReplayState.terminal) {
        sessionReplayState.terminal.dispose();
    }
    sessionReplayState = null;
    logRecordingContext = null;
}

function replaySessionToIndex(targetIndex) {
    if (!sessionReplayState) return;
    const clampedIndex = Math.max(0, Math.min(Number(targetIndex || 0), sessionReplayState.events.length));
    if (!sessionReplayState.terminal) {
        createSessionReplayTerminal();
    }
    if (clampedIndex < sessionReplayState.playbackIndex) {
        createSessionReplayTerminal();
        sessionReplayState.playbackIndex = 0;
    }
    for (let index = sessionReplayState.playbackIndex; index < clampedIndex; index += 1) {
        applyTerminalRecordingEvent(sessionReplayState.events[index]);
    }
    sessionReplayState.playbackIndex = clampedIndex;
    updateSessionReplayUi();
}

function applyTerminalRecordingEvent(event) {
    if (!sessionReplayState || !sessionReplayState.terminal || !event || typeof event !== 'object') return;
    if (event.event_type === 'resize' && Number.isInteger(event.cols) && Number.isInteger(event.rows)) {
        sessionReplayState.initialGeometry = { rows: event.rows, cols: event.cols };
        sessionReplayState.terminal.resize(event.cols, event.rows);
        return;
    }
    if (event.event_type !== 'output' || !event.data_b64) {
        return;
    }
    sessionReplayState.terminal.write(decodeTerminalRecordingData(event.data_b64));
}

function decodeTerminalRecordingData(dataB64) {
    const binary = atob(String(dataB64 || ''));
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
        bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
}

function extractPlainTextFromRecordingEvents(events) {
    const decoder = new TextDecoder();
    return (events || [])
        .filter(event => event && event.event_type === 'output' && event.data_b64)
        .map(event => decoder.decode(decodeTerminalRecordingData(event.data_b64)))
        .join('');
}

function restartSessionReplay(autoPlay = false) {
    pauseSessionReplay();
    replaySessionToIndex(0);
    if (autoPlay) {
        startSessionReplay();
    }
}

function jumpSessionReplayToLatest() {
    pauseSessionReplay();
    if (!sessionReplayState) return;
    replaySessionToIndex(sessionReplayState.events.length);
}

function toggleSessionReplayPlayback() {
    if (!sessionReplayState) return;
    if (sessionReplayState.playing) {
        pauseSessionReplay();
        return;
    }
    if (sessionReplayState.playbackIndex >= sessionReplayState.events.length) {
        replaySessionToIndex(0);
    }
    startSessionReplay();
}

function startSessionReplay() {
    if (!sessionReplayState) return;
    sessionReplayState.playing = true;
    scheduleSessionReplayStep();
    updateSessionReplayUi();
}

function pauseSessionReplay() {
    if (!sessionReplayState) return;
    sessionReplayState.playing = false;
    if (sessionReplayState.playTimer) {
        clearTimeout(sessionReplayState.playTimer);
        sessionReplayState.playTimer = null;
    }
    updateSessionReplayUi();
}

function scheduleSessionReplayStep() {
    if (!sessionReplayState) return;
    if (sessionReplayState.playTimer) {
        clearTimeout(sessionReplayState.playTimer);
        sessionReplayState.playTimer = null;
    }
    if (!sessionReplayState.playing) return;
    if (sessionReplayState.playbackIndex >= sessionReplayState.events.length) {
        sessionReplayState.playing = false;
        updateSessionReplayUi();
        return;
    }
    const nextIndex = sessionReplayState.playbackIndex;
    const previousOffset = nextIndex > 0 ? Number(sessionReplayState.events[nextIndex - 1]?.offset_ms || 0) : 0;
    const nextOffset = Number(sessionReplayState.events[nextIndex]?.offset_ms || 0);
    const delayMs = Math.max(0, Math.round((nextOffset - previousOffset) / Math.max(sessionReplayState.speed || 1, 0.1)));
    sessionReplayState.playTimer = setTimeout(() => {
        if (!sessionReplayState) return;
        applyTerminalRecordingEvent(sessionReplayState.events[nextIndex]);
        sessionReplayState.playbackIndex = nextIndex + 1;
        updateSessionReplayUi();
        scheduleSessionReplayStep();
    }, delayMs);
}

function updateSessionReplayUi() {
    if (!sessionReplayState) return;
    const total = sessionReplayState.events.length;
    const current = sessionReplayState.playbackIndex;
    const seekEl = document.getElementById('sessionReplaySeek');
    const progressEl = document.getElementById('sessionReplayProgressText');
    const statusEl = document.getElementById('sessionReplayStatus');
    const clockEl = document.getElementById('sessionReplayClock');
    const playPauseEl = document.getElementById('sessionReplayPlayPause');
    const followToggleEl = document.getElementById('logFollowToggle');
    if (seekEl) {
        seekEl.max = String(total);
        seekEl.value = String(current);
    }
    if (progressEl) {
        progressEl.textContent = `${current} / ${total} events`;
    }
    if (clockEl) {
        const activeEvent = current > 0 ? sessionReplayState.events[current - 1] : sessionReplayState.events[0];
        const offsetMs = Number(activeEvent?.offset_ms || 0);
        clockEl.textContent = `${(offsetMs / 1000).toFixed(1)}s`;
    }
    if (statusEl) {
        if (total === 0) {
            statusEl.textContent = 'Waiting for first output...';
        } else if (sessionReplayState.playing) {
            statusEl.textContent = `Playing at ${sessionReplayState.speed}x`;
        } else if (current >= total) {
            statusEl.textContent = sessionReplayState.follow ? 'At latest output' : 'Paused at end';
        } else {
            statusEl.textContent = 'Paused';
        }
    }
    if (playPauseEl) {
        playPauseEl.textContent = sessionReplayState.playing ? 'Pause' : 'Play';
    }
    if (followToggleEl) {
        followToggleEl.checked = !!sessionReplayState.follow;
    }
}

function formatLogStreamObservation(obs) {
    if (!obs || typeof obs !== 'object') return '';
    const fmt = (fileObs) => {
        if (!fileObs || typeof fileObs !== 'object') return 'n/a';
        const exists = fileObs.exists ? 'yes' : 'no';
        const bytes = Number.isFinite(fileObs.bytes) ? `${fileObs.bytes}B` : '?';
        const mtime = Number.isFinite(fileObs.mtime_epoch)
            ? new Date(fileObs.mtime_epoch * 1000).toLocaleTimeString()
            : '—';
        return `${exists}, ${bytes}, ${mtime}`;
    };
    return `Stream observation - recording: ${fmt(obs.terminal_recording)} | stdout: ${fmt(obs.provider_stdout)} | stderr: ${fmt(obs.provider_stderr)}`;
}

async function refreshInlineSessionPrompt(issueNumber, runDir = null) {
    const promptMeta = document.getElementById('logPromptMeta');
    const promptPre = document.getElementById('logPromptPre');
    if (!promptMeta || !promptPre) return;
    if (!runDir) {
        promptMeta.textContent = 'Prompt unavailable (missing run context).';
        promptPre.textContent = '';
        return;
    }
    try {
        const params = new URLSearchParams();
        params.set('run_dir', runDir);
        const suffix = params.toString() ? `?${params.toString()}` : '';
        const res = await fetch(`/api/session/prompt/${issueNumber}${suffix}`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
            promptMeta.textContent = data.error || `Prompt unavailable (HTTP ${res.status})`;
            promptPre.textContent = '';
            return;
        }
        promptMeta.textContent = data.prompt_path ? `Prompt: ${data.prompt_path}` : 'Prompt';
        promptPre.textContent = data.content || '';
    } catch (err) {
        promptMeta.textContent = `Prompt unavailable: ${err instanceof Error ? err.message : String(err)}`;
        promptPre.textContent = '';
    }
}

async function openSessionManifest(issueNumber, runDir = null) {
    const params = new URLSearchParams();
    if (runDir) params.set('run_dir', runDir);
    const suffix = params.toString() ? `?${params.toString()}` : '';
    const res = await fetch(`/api/dialog/session-diagnostics/${issueNumber}${suffix}`);
    const data = await res.json();
    const modalEl = modalOverlay.querySelector('.modal');
    if (data.error) {
        document.getElementById('modalTitle').textContent = `Session Diagnostics #${issueNumber}`;
        document.getElementById('modalBody').innerHTML = `<div class="diag-action-message" style="display:block;">${escapeHtml(data.error)}</div>`;
        modalEl.classList.add('diagnostics-modal');
        document.getElementById('modalOverlay').classList.add('visible');
        return;
    }

    const rows = data.rows || [];
    const actions = data.actions || [];
    currentDiagnosticsRunDir = runDir || ((actions.find(action => action && action.run_dir) || {}).run_dir || null);
    const rowByLabel = new Map(rows.map(row => [String(row.label || '').toLowerCase(), String(row.value || '')]));
    const worktree = rowByLabel.get('worktree') || '';

    const hasWorktree = worktree && worktree !== '-';
    const hasDiagnostic = actions.some(action => action.type === 'open_path' && (action.label || '').toLowerCase().includes('diagnostic'));
    const hasValidation = actions.some(action => action.type === 'open_path' && (action.label || '').toLowerCase().includes('validation'));

    const chips = [
        `<span class="diag-chip ${hasWorktree ? 'is-ok' : 'is-muted'}">${hasWorktree ? 'Worktree Present' : 'Worktree Unavailable'}</span>`,
        `<span class="diag-chip ${hasDiagnostic ? 'is-ok' : 'is-muted'}">${hasDiagnostic ? 'Diagnostic Available' : 'No Diagnostic Yet'}</span>`,
        `<span class="diag-chip ${hasValidation ? 'is-ok' : 'is-muted'}">${hasValidation ? 'Validation Captured' : 'No Validation Artifact'}</span>`,
    ].join('');

    const overviewKeys = new Set([
        'session',
        'started',
        'run id',
        'backend',
        'agent',
        'claude session',
        'retention tier',
        'retention expires',
        'retention pinned',
    ]);
    const launchKeys = new Set([
        'task',
        'branch',
        'provider',
        'model',
        'permission mode',
        'timeout',
        'provider args',
        'launch args',
        'prompt mode',
    ]);
    const overviewRows = rows.filter(row => overviewKeys.has(String(row.label || '').toLowerCase()));
    const launchRows = rows.filter(row => launchKeys.has(String(row.label || '').toLowerCase()));
    const pathRows = rows.filter(row => {
        const key = String(row.label || '').toLowerCase();
        return !overviewKeys.has(key) && !launchKeys.has(key);
    });
    const analysis = data.analysis && typeof data.analysis === 'object' ? data.analysis : null;
    const followUpIssues = Array.isArray(data.follow_up_issues) ? data.follow_up_issues : [];

    const hasActions = actions.length > 0;

    let html = '<div class="diag-modal">';
    html += '<div id="diagActionMessage" class="diag-action-message"></div>';
    html += '<div class="diag-header">';
    html += `<div class="diag-header-title">Issue #${issueNumber} Diagnostics</div>`;
    html += `<div class="diag-chip-row">${chips}</div>`;
    html += '</div>';

    html += '<div class="diag-grid">';
    html += '<section class="diag-section">';
    html += '<div class="diag-section-title">Session Overview</div>';
    html += renderDialogRows(overviewRows);
    html += '</section>';
    html += '<section class="diag-section">';
    html += '<div class="diag-section-title">Launch</div>';
    html += renderDialogRows(launchRows, { monospace: true });
    html += '</section>';
    html += '<section class="diag-section">';
    html += '<div class="diag-section-title">Paths</div>';
    html += renderDialogRows(pathRows, { monospace: true });
    html += '</section>';
    html += '</div>';

    if (analysis && analysis.headline) {
        html += '<section class="diag-section diag-analysis">';
        html += '<div class="diag-section-title">Current Diagnosis</div>';
        html += `<div class="diag-analysis-headline">${escapeHtml(String(analysis.headline || ''))}</div>`;
        if (analysis.detail) {
            html += `<div class="diag-analysis-detail">${escapeHtml(String(analysis.detail))}</div>`;
        }
        if (Array.isArray(analysis.suggestions) && analysis.suggestions.length > 0) {
            html += '<ul class="diag-analysis-suggestions">';
            for (const suggestion of analysis.suggestions) {
                html += `<li>${escapeHtml(String(suggestion))}</li>`;
            }
            html += '</ul>';
        }
        html += '</section>';
    }

    if (followUpIssues.length > 0) {
        html += '<section class="diag-section diag-analysis">';
        html += '<div class="diag-section-title">Proposed Follow-up Issues</div>';
        html += '<div class="diag-analysis-detail">Ancillary work discovered during the run and deferred to keep the assigned issue time-bounded.</div>';
        html += '<ul class="diag-analysis-suggestions">';
        for (const item of followUpIssues) {
            const title = escapeHtml(String(item.title || 'Untitled follow-up'));
            const reason = escapeHtml(String(item.reason || 'No reason provided.'));
            const evidence = item.evidence ? ` <span class="diag-followup-evidence">${escapeHtml(String(item.evidence))}</span>` : '';
            const labels = Array.isArray(item.suggested_labels) && item.suggested_labels.length > 0
                ? ` <span class="diag-followup-labels">labels: ${escapeHtml(item.suggested_labels.join(', '))}</span>`
                : '';
            const blocking = item.blocking ? ' <span class="diag-followup-blocking">blocking</span>' : '';
            html += `<li><strong>${title}</strong><div>${reason}${blocking}${evidence}${labels}</div></li>`;
        }
        html += '</ul>';
        html += '</section>';
    }

    if (hasActions) {
        html += '<div class="diag-actions">';
        html += renderGroupedDialogActions(actions);
        html += '</div>';
    } else {
        html += '<div class="diag-empty">No diagnostic actions available for this run.</div>';
    }

    html += '<div class="diag-footnote">Tip: this view is for deep troubleshooting and artifact access.</div>';
    html += '</div>';

    modalEl.classList.add('diagnostics-modal');
    openModal(data.title || `Session Diagnostics #${issueNumber}`, html);
}

async function openValidationFailure(issueNumber, runDir = null, mode = 'modal') {
    const params = new URLSearchParams();
    if (runDir) params.set('run_dir', runDir);
    const suffix = params.toString() ? `?${params.toString()}` : '';
    const res = await fetch(`/api/dialog/validation-failure/${issueNumber}${suffix}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) {
        const message = data.error || `Failed to load validation details (HTTP ${res.status})`;
        if (mode === 'inline') {
            showToast(message, 'error');
            return;
        }
        openModal(`Validation Failure #${issueNumber}`, `<div class="diag-action-message" style="display:block;">${escapeHtml(message)}</div>`);
        return;
    }

    const actions = Array.isArray(data.actions) ? data.actions : [];
    currentDiagnosticsRunDir = runDir || ((actions.find(action => action && action.run_dir) || {}).run_dir || null);
    const failedTests = Array.isArray(data.failed_tests) ? data.failed_tests : [];
    const stdoutExcerpt = Array.isArray(data.stdout_excerpt) ? data.stdout_excerpt : [];
    const stderrExcerpt = Array.isArray(data.stderr_excerpt) ? data.stderr_excerpt : [];
    const summaryRows = [
        { label: 'Reason', value: String(data.reason || 'Validation failed') },
        { label: 'Suite', value: String(data.suite || '-') },
        { label: 'Command', value: String(data.command || '-') },
        { label: 'Exit Code', value: String(data.exit_code ?? '-') },
        { label: 'Started', value: String(data.started_at || '-') },
        { label: 'Ended', value: String(data.ended_at || '-') },
    ];

    let html = '<div class="diag-modal">';
    html += '<section class="diag-section diag-validation-summary">';
    html += '<div class="diag-section-title">Validation Summary</div>';
    html += renderDialogRows(summaryRows, { monospace: true });
    html += '</section>';

    html += '<section class="diag-section">';
    html += '<div class="diag-section-title">Failed Tests</div>';
    if (failedTests.length > 0) {
        html += '<ul class="diag-validation-tests">';
        for (const testName of failedTests) {
            html += `<li><code>${escapeHtml(String(testName))}</code></li>`;
        }
        html += '</ul>';
    } else {
        html += '<div class="diag-empty">No failed test names were extracted from validation output.</div>';
    }
    html += '</section>';

    html += '<section class="diag-section">';
    html += '<div class="diag-section-title">Validation Output Excerpt</div>';
    if (stdoutExcerpt.length > 0) {
        html += `<pre class="diag-validation-pre">${escapeHtml(stdoutExcerpt.join('\n'))}</pre>`;
    } else {
        html += '<div class="diag-empty">No stdout excerpt captured.</div>';
    }
    html += '</section>';

    if (stderrExcerpt.length > 0) {
        html += '<section class="diag-section">';
        html += '<div class="diag-section-title">Validation Error Output</div>';
        html += `<pre class="diag-validation-pre">${escapeHtml(stderrExcerpt.join('\n'))}</pre>`;
        html += '</section>';
    }

    if (actions.length > 0) {
        html += '<div class="diag-actions">';
        html += renderGroupedDialogActions(actions);
        html += '</div>';
    }
    html += '<div class="diag-footnote">Validation details come from the run-scoped validation record and log artifacts.</div>';
    html += '</div>';

    openModal(data.title || `Validation Failure #${issueNumber}`, html);
}

function renderDialogRows(rows, options = {}) {
    const useMonospace = !!options.monospace;
    if (!rows || rows.length === 0) {
        return '<div class="diag-empty">No data available.</div>';
    }
    let html = '<div class="diag-rows">';
    for (const row of rows) {
        const label = escapeHtml(String(row.label || ''));
        const rawValue = String(row.value || '-');
        const value = escapeHtml(rawValue);
        html += '<div class="diag-row">';
        html += `<span class="diag-row-label">${label}</span>`;
        if (useMonospace) {
            html += `<code class="diag-row-value is-monospace">${value}</code>`;
        } else {
            html += `<span class="diag-row-value">${value}</span>`;
        }
        html += '</div>';
    }
    html += '</div>';
    return html;
}

function renderDialogAction(action) {
    return renderDialogActionWithLabel(action);
}

function renderGroupedDialogActions(actions) {
    const items = (actions || []).map(action => ({
        action,
        label: _dialogActionShortLabel(action),
    }));
    if (items.length === 0) return '';

    const primaryTypes = [
        'open_validation_failure',
        'open_agent_log',
        'open_review_feedback',
        'open_review_transcript',
    ];
    const primary = [];
    const used = new Set();
    for (const type of primaryTypes) {
        const item = items.find(candidate => String(candidate.action?.type || '') === type);
        if (!item) continue;
        primary.push(item);
        used.add(item);
    }

    const secondary = items.filter(item => !used.has(item));

    let html = '<section class="diag-section">';
    html += '<div class="diag-section-title">Actions</div>';
    html += '<div class="diag-primary-actions">';
    for (const item of primary) {
        html += renderDialogActionWithLabel(item.action, item.label);
    }
    html += '</div>';
    if (secondary.length > 0) {
        html += '<div class="diag-secondary-actions">';
        for (const item of secondary) {
            html += renderDialogActionMenuItem(item.action, item.label);
        }
        html += '</div>';
    }
    html += '</section>';
    return html;
}

function _dialogActionShortLabel(action) {
    if (!action) return 'Action';
    const type = String(action.type || '');
    const label = String(action.label || '');
    if (type === 'open_agent_log') return 'Session Recording';
    if (type === 'open_review_transcript') return 'Review Transcript';
    if (type === 'open_validation_failure') return 'Validation Details';
    if (type === 'copy_agent_log') return 'Copy Session Recording';
    if (type === 'view_claude_log') return 'Claude Log';
    if (type === 'open_orchestrator_log') return 'Issue-Scoped Orchestrator Log';
    if (type === 'open_review_feedback') return 'Review Feedback';
    if (type === 'open_session_diagnostics') return label || 'Diagnostics';
    if (type === 'open_path') {
        const normalized = label.replace(/^Open\s+/i, '').replace(/\s+↗$/, '').trim();
        if (/^completion$/i.test(normalized)) return 'Completion Record';
        if (/^validation$/i.test(normalized)) return 'Validation Record';
        if (/^run dir$/i.test(normalized)) return 'Run Directory';
        return normalized || 'Path';
    }
    return label || 'Action';
}

function renderDialogActionWithLabel(action, labelOverride = null) {
    return _renderDialogActionButton(action, labelOverride, 'btn-secondary');
}

function renderDialogActionMenuItem(action, labelOverride = null) {
    return _renderDialogActionButton(action, labelOverride, 'diag-more-item');
}

function _renderDialogActionButton(action, labelOverride, cssClass) {
    if (!action) return '';
    const label = escapeHtml(labelOverride || action.label || 'Action');
    const fallbackRunDir = action.run_dir || currentDiagnosticsRunDir || null;
    if (action.type === 'open_path') {
        return `<button class="${cssClass}" onclick="openPath('${escapeHtml(action.path)}')">${label}</button>`;
    }
    if (action.type === 'open_validation_failure') {
        if (!fallbackRunDir) return '';
        return `<button class="${cssClass}" onclick="openValidationFailure(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))}, 'inline')">${label}</button>`;
    }
    if (action.type === 'open_agent_log') {
        if (!fallbackRunDir) return '';
        const runDirFirstArg = `${JSON.stringify(String(fallbackRunDir))}, `;
        const contextLiteral = JSON.stringify({
            round_index: Number.isInteger(Number(action.round_index)) ? Number(action.round_index) : null,
            session_role: action.session_role || null,
        });
        return `<button class="${cssClass}" onclick="openAgentLogAction(${action.issue_number}, ${runDirFirstArg}'Session Recording', 'inline', ${contextLiteral})">${label}</button>`;
    }
    if (action.type === 'open_review_transcript') {
        if (!fallbackRunDir) return '';
        const roundIndexLiteral = Number.isInteger(Number(action.round_index))
            ? String(Number(action.round_index))
            : 'null';
        const roleLiteral = JSON.stringify(action.transcript_role || null);
        return `<button class="${cssClass}" onclick="openReviewTranscript(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))}, { round_index: ${roundIndexLiteral}, transcript_role: ${roleLiteral} }, 'inline')">${label}</button>`;
    }
    if (action.type === 'copy_agent_log') {
        if (!fallbackRunDir) return '';
        return `<button class="${cssClass}" onclick="copyAgentLogAction(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))})">${label}</button>`;
    }
    if (action.type === 'view_claude_log') {
        if (!fallbackRunDir) return '';
        return `<button class="${cssClass}" onclick="viewClaudeLog(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))}, 'inline')">${label}</button>`;
    }
    if (action.type === 'open_orchestrator_log') {
        if (fallbackRunDir) {
            return `<button class="${cssClass}" onclick="openFilteredOrchestratorLog(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))}, 'inline')">${label}</button>`;
        }
        return `<button class="${cssClass}" onclick="openFilteredOrchestratorLog(${action.issue_number}, null, 'inline')">${label}</button>`;
    }
    if (action.type === 'open_review_feedback') {
        return `<button class="${cssClass}" onclick="openReviewFeedback(${action.issue_number})">${label}</button>`;
    }
    if (action.type === 'open_session_diagnostics') {
        if (fallbackRunDir) {
            return `<button class="${cssClass}" onclick="openSessionManifest(${action.issue_number}, ${JSON.stringify(String(fallbackRunDir))})">${label}</button>`;
        }
        return `<button class="${cssClass}" onclick="openSessionManifest(${action.issue_number})">${label}</button>`;
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
        if (!clientCapabilities.focus_session) {
            try {
                await openSessionManifest(issueNumber);
            } catch (err) {
                showToast('Failed to open session diagnostics', 'error');
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
    if (!confirm(`Terminate session #${issueNumber}?\n\nThis will stop the active agent and place the issue on hold.\nIt will not run again until you explicitly retry/unblock it.`)) return;
    try {
        const res = await fetch(`/api/kill/${issueNumber}`, { method: 'POST' });
        const data = await res.json();
        if (data.status === 'terminated') {
            showToast(`Terminated #${issueNumber} (on hold)`);
            location.reload();
        } else {
            showToast(data.error || 'Failed to terminate session', true);
        }
    } catch (e) {
        showToast('Failed to terminate session: ' + e.message, true);
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
            const data = await res.json();
            if (data.refresh?.requested || data.refresh?.in_progress) {
                const statusText = document.getElementById('refreshStatusText');
                if (statusText) {
                    statusText.textContent = data.refresh?.in_progress ? 'Refreshing from GitHub...' : 'Refresh requested...';
                }
            }
            showToast('Refresh requested from GitHub');
            setTimeout(() => refreshViewModel({ reloadOnListChange: false }), 1200);
        } else {
            showToast('Refresh failed', true);
        }
    } catch (e) {
        console.error('Refresh failed:', e);
        showToast('Refresh failed', true);
    }
}

function deriveFlowConfig(strategy) {
    const normalizedMode = ['aggressive', 'balanced', 'economy'].includes(strategy.freshnessMode)
        ? strategy.freshnessMode
        : 'balanced';
    const normalizedBudget = ['low', 'medium', 'high'].includes(strategy.apiBudget)
        ? strategy.apiBudget
        : 'medium';
    const normalizedAttention = ['strict', 'normal'].includes(strategy.attentionPriority)
        ? strategy.attentionPriority
        : 'strict';
    const preset = FLOW_FRESHNESS_PRESETS[normalizedMode];
    const multiplier = FLOW_BUDGET_MULTIPLIER[normalizedBudget];
    const hasCustomStale = Number.isFinite(Number(strategy.flowStaleSeconds));
    const hasCustomCooldown = Number.isFinite(Number(strategy.flowCooldownSeconds));
    const staleBase = hasCustomStale ? Number(strategy.flowStaleSeconds) : preset.staleSeconds * multiplier;
    const cooldownBase = hasCustomCooldown ? Number(strategy.flowCooldownSeconds) : preset.cooldownSeconds * multiplier;
    return {
        enabled: strategy.flowLazyEnabled ?? Boolean(preset.enabled),
        staleSeconds: Math.max(60, Math.round(staleBase)),
        cooldownSeconds: Math.max(0, Math.round(cooldownBase)),
        freshnessMode: normalizedMode,
        apiBudget: normalizedBudget,
        attentionPriority: normalizedAttention,
    };
}

function serverRefreshStrategy() {
    const refresh = window.dashboardData?.refresh || {};
    return {
        flowLazyEnabled: Boolean(refresh.flowLazyEnabled),
        flowStaleSeconds: Number(refresh.flowStaleSeconds || 900),
        flowCooldownSeconds: Number(refresh.flowCooldownSeconds || 120),
        freshnessMode: String(refresh.freshnessMode || 'balanced'),
        apiBudget: String(refresh.apiBudget || 'medium'),
        attentionPriority: String(refresh.attentionPriority || 'strict'),
    };
}

function currentRefreshConfig() {
    const server = deriveFlowConfig(serverRefreshStrategy());
    const override = getFlowRefreshOverride();
    if (!override?.enabled) {
        return { ...server, source: 'yaml' };
    }
    const combined = deriveFlowConfig({
        flowLazyEnabled: override.flowLazyEnabled,
        flowStaleSeconds: Number(override.flowStaleSeconds),
        flowCooldownSeconds: Number(override.flowCooldownSeconds),
        freshnessMode: override.freshnessMode || server.freshnessMode,
        apiBudget: override.apiBudget || server.apiBudget,
        attentionPriority: override.attentionPriority || server.attentionPriority,
    });
    return { ...combined, source: 'override' };
}

function getFlowRefreshOverride() {
    const raw = localStorage.getItem(FLOW_REFRESH_OVERRIDE_KEY);
    if (!raw) return null;
    try {
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object') return null;
        return {
            enabled: Boolean(parsed.enabled),
            flowLazyEnabled: Boolean(parsed.flowLazyEnabled),
            flowStaleSeconds: Math.max(60, Number(parsed.flowStaleSeconds || 900)),
            flowCooldownSeconds: Math.max(0, Number(parsed.flowCooldownSeconds || 120)),
            freshnessMode: ['aggressive', 'balanced', 'economy'].includes(parsed.freshnessMode) ? parsed.freshnessMode : 'balanced',
            apiBudget: ['low', 'medium', 'high'].includes(parsed.apiBudget) ? parsed.apiBudget : 'medium',
            attentionPriority: ['strict', 'normal'].includes(parsed.attentionPriority) ? parsed.attentionPriority : 'strict',
        };
    } catch {
        return null;
    }
}

function getNetworkSyncOverride() {
    const raw = localStorage.getItem(NETWORK_SYNC_OVERRIDE_KEY);
    if (!raw) return null;
    try {
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object') return null;
        const seconds = Math.max(5, Number(parsed.seconds || 0));
        if (!Number.isFinite(seconds)) return null;
        return { enabled: Boolean(parsed.enabled), seconds };
    } catch {
        return null;
    }
}

function currentNetworkSyncCadence() {
    const server = Number(window.dashboardData?.refresh?.networkSyncSeconds || 60);
    const override = getNetworkSyncOverride();
    if (!override?.enabled) {
        return { seconds: Math.max(5, server), source: 'config' };
    }
    return { seconds: Math.max(5, Number(override.seconds || server)), source: 'override' };
}

async function requestNetworkSyncSilently() {
    try {
        await fetch('/api/refresh', { method: 'POST' });
    } catch (err) {
        console.error('Background network sync request failed:', err);
    }
}

function applyNetworkSyncScheduler() {
    if (networkSyncTimer) {
        window.clearInterval(networkSyncTimer);
        networkSyncTimer = null;
    }
    const cadence = currentNetworkSyncCadence();
    if (cadence.source !== 'override') {
        return;
    }
    networkSyncTimer = window.setInterval(() => {
        requestNetworkSyncSilently();
    }, cadence.seconds * 1000);
}

function formatResetLabel(resetEpochSeconds) {
    if (!resetEpochSeconds || !Number.isFinite(resetEpochSeconds)) return '-';
    const delta = Math.max(0, Math.floor(resetEpochSeconds - Date.now() / 1000));
    const mins = Math.floor(delta / 60);
    const secs = delta % 60;
    return `${mins}m ${secs}s`;
}

function getGitHubUsagePrefs() {
    const raw = localStorage.getItem(GH_USAGE_UI_PREF_KEY);
    if (!raw) {
        return { hidden: false, expanded: false };
    }
    try {
        const parsed = JSON.parse(raw);
        return {
            hidden: Boolean(parsed.hidden),
            expanded: Boolean(parsed.expanded),
        };
    } catch {
        return { hidden: false, expanded: false };
    }
}

function saveGitHubUsagePrefs(prefs) {
    localStorage.setItem(GH_USAGE_UI_PREF_KEY, JSON.stringify({
        hidden: Boolean(prefs.hidden),
        expanded: Boolean(prefs.expanded),
    }));
}

function applyGitHubUsagePrefs() {
    const prefs = getGitHubUsagePrefs();
    const wrap = document.getElementById('ghUsageWrap');
    const panel = document.getElementById('ghUsagePanel');
    const pill = document.getElementById('ghUsagePill');
    if (!wrap || !panel || !pill) return;
    wrap.style.display = prefs.hidden ? 'none' : '';
    panel.classList.toggle('visible', !prefs.hidden && prefs.expanded);
    pill.setAttribute('aria-expanded', (!prefs.hidden && prefs.expanded) ? 'true' : 'false');
}

function toggleGitHubUsagePanel() {
    const prefs = getGitHubUsagePrefs();
    prefs.hidden = false;
    prefs.expanded = !prefs.expanded;
    saveGitHubUsagePrefs(prefs);
    applyGitHubUsagePrefs();
}

function setGitHubUsageHidden(hidden) {
    const prefs = getGitHubUsagePrefs();
    prefs.hidden = Boolean(hidden);
    if (prefs.hidden) {
        prefs.expanded = false;
    }
    saveGitHubUsagePrefs(prefs);
    applyGitHubUsagePrefs();
}

function renderGitHubUsage() {
    const usage = window.dashboardData?.githubUsage || {};
    const rate = usage.last_rate_limit_from_headers || {};
    const remaining = Number(rate.remaining);
    const limit = Number(rate.limit);
    const callsPerMinute = Number(usage.calls_per_minute || 0);
    const totalCalls = Number(usage.total_calls || 0);
    const errors = Number(usage.errors || 0);
    const summary = document.getElementById('ghUsageSummary');
    const cpmEl = document.getElementById('ghUsageCallsPerMinute');
    const totalEl = document.getElementById('ghUsageTotalCalls');
    const errEl = document.getElementById('ghUsageErrors');
    const limitEl = document.getElementById('ghUsageRateLimit');
    const resetEl = document.getElementById('ghUsageReset');

    if (summary) {
        summary.textContent = `${callsPerMinute}/min`;
    }
    if (cpmEl) cpmEl.textContent = callsPerMinute.toLocaleString();
    if (totalEl) totalEl.textContent = totalCalls.toLocaleString();
    if (errEl) errEl.textContent = errors.toLocaleString();
    if (limitEl) {
        if (Number.isFinite(remaining) && Number.isFinite(limit) && limit > 0) {
            const used = Number.isFinite(Number(rate.used)) ? Number(rate.used) : Math.max(0, limit - remaining);
            const resource = rate.resource ? ` (${String(rate.resource)})` : '';
            limitEl.textContent = `${used.toLocaleString()} used · ${remaining.toLocaleString()} left${resource}`;
        } else {
            limitEl.textContent = 'No rate header yet';
        }
    }
    if (resetEl) {
        resetEl.textContent = formatResetLabel(Number(rate.reset || 0));
    }
}

function updateRefreshStatusFromViewModel(vm) {
    const refresh = vm?.dashboard_data?.refresh;
    if (!refresh) return;
    window.dashboardData = window.dashboardData || {};
    window.dashboardData.refresh = refresh;

    const statusText = document.getElementById('refreshStatusText');
    const statusMeta = document.getElementById('refreshStatusMeta');
    if (statusText) {
        if (refresh.inProgress) {
            statusText.textContent = 'Refreshing from GitHub...';
        } else if (refresh.requested) {
            statusText.textContent = 'Refresh requested...';
        } else {
            statusText.textContent = `Last GitHub sync: ${refresh.lastRefreshLabel || 'unknown'}`;
        }
    }
    if (statusMeta) {
        const cfg = currentRefreshConfig();
        const flowSource = cfg.source === 'override' ? 'override' : 'config';
        const network = currentNetworkSyncCadence();
        if (cfg.enabled) {
            statusMeta.textContent = `· ${cfg.freshnessMode}/${cfg.apiBudget}/${cfg.attentionPriority} · stale>${cfg.staleSeconds}s (${flowSource}) · network ${network.seconds}s (${network.source})`;
        } else {
            statusMeta.textContent = `· lazy visible refresh off (${flowSource}) · network ${network.seconds}s (${network.source})`;
        }
    }
}

function updateIssueCardFreshness(issueNumber, freshness) {
    const cards = document.querySelectorAll(
        `.issue-card[data-issue="${issueNumber}"], .attention-item[data-issue="${issueNumber}"], .history-item[data-issue="${issueNumber}"]`
    );
    cards.forEach((card) => {
        if (typeof freshness.last_refreshed_age_seconds === 'number') {
            card.dataset.lastRefreshAgeSeconds = String(freshness.last_refreshed_age_seconds);
        }
        card.dataset.stale = freshness.is_stale ? 'true' : 'false';
        const actionRow = card.querySelector('.card-head-actions') || card.querySelector('.attention-actions');
        let staleDot = card.querySelector('.stale-dot');
        if (freshness.is_stale) {
            if (!staleDot && actionRow) {
                staleDot = document.createElement('span');
                staleDot.className = 'stale-dot';
                actionRow.prepend(staleDot);
            }
            if (staleDot && freshness.stale_reason) {
                staleDot.title = freshness.stale_reason;
                staleDot.setAttribute('aria-label', freshness.stale_reason);
            }
        } else if (staleDot) {
            staleDot.remove();
        }
    });
}

async function refreshIssueCard(issueNumber, triggerEl = null, options = {}) {
    const now = Date.now();
    const cfg = currentRefreshConfig();
    const cooldownMs = Math.max(0, cfg.cooldownSeconds) * 1000;
    const lastAttempt = issueRefreshLastAttempt.get(issueNumber) || 0;
    if (!options.force && now - lastAttempt < cooldownMs) {
        return;
    }
    if (issueRefreshInFlight.has(issueNumber)) {
        return;
    }

    issueRefreshLastAttempt.set(issueNumber, now);
    issueRefreshInFlight.add(issueNumber);
    if (triggerEl) {
        triggerEl.disabled = true;
    }

    try {
        const res = await fetch(`/api/issues/${issueNumber}/refresh`, { method: 'POST' });
        const data = await res.json();
        if (!res.ok) {
            if (!options.silent) {
                showToast(data.error || `Refresh failed for #${issueNumber}`, true);
            }
            return;
        }
        updateIssueCardFreshness(issueNumber, {
            is_stale: false,
            stale_reason: '',
            last_refreshed_age_seconds: 0,
            last_refreshed_label: 'just now',
        });
        if (!options.silent) {
            showToast(`Refreshed issue #${issueNumber}`);
        }
    } catch (error) {
        if (!options.silent) {
            showToast(`Refresh failed for #${issueNumber}`, true);
        }
    } finally {
        issueRefreshInFlight.delete(issueNumber);
        if (triggerEl) {
            triggerEl.disabled = false;
        }
    }
}

function maybeRefreshVisibleCard(card) {
    const issueNumber = Number(card.dataset.issue);
    if (!Number.isInteger(issueNumber)) {
        return;
    }
    if (card.dataset.stale !== 'true') {
        return;
    }
    const cfg = currentRefreshConfig();
    if (!cfg.enabled) {
        return;
    }
    refreshIssueCard(issueNumber, null, { silent: true });
}

function initFlowLazyVisibleRefresh() {
    if (flowRefreshObserver) {
        flowRefreshObserver.disconnect();
        flowRefreshObserver = null;
    }
    const cfg = currentRefreshConfig();
    if (!cfg.enabled) return;
    const selectors = ['#panel-flow .issue-card[data-issue]'];
    if (cfg.attentionPriority === 'strict') {
        selectors.push('#panel-attention .attention-item[data-issue]');
    }
    const cards = document.querySelectorAll(selectors.join(', '));
    if (!cards.length) return;
    flowRefreshObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
            if (entry.isIntersecting && entry.intersectionRatio >= 0.4) {
                maybeRefreshVisibleCard(entry.target);
            }
        }
    }, { threshold: [0.4] });
    cards.forEach((card) => flowRefreshObserver.observe(card));
}

function setFlowRefreshInputsEnabled(enabled) {
    const ids = [
        'flowRefreshEnabled',
        'flowRefreshStaleSeconds',
        'flowRefreshCooldownSeconds',
        'flowFreshnessMode',
        'flowApiBudget',
        'flowAttentionPriority',
    ];
    ids.forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.disabled = !enabled;
    });
}

function setNetworkSyncInputEnabled(enabled) {
    const overrideEl = document.getElementById('networkSyncOverrideSeconds');
    if (overrideEl) {
        overrideEl.disabled = !enabled;
    }
}

function setFullScanInputEnabled(enabled) {
    const fullScanEl = document.getElementById('fullScanConfigSeconds');
    if (fullScanEl) {
        fullScanEl.disabled = !enabled;
    }
}

function updateCadencePreferenceInputState() {
    const cadenceSourceEl = document.getElementById('networkSyncCadenceSource');
    const saveCadenceEl = document.getElementById('saveCadenceToConfig');
    if (!cadenceSourceEl || !saveCadenceEl) return;
    const saveToConfig = Boolean(saveCadenceEl.checked);
    setNetworkSyncInputEnabled(saveToConfig || cadenceSourceEl.value === 'override');
    setFullScanInputEnabled(saveToConfig);
}

async function saveCadenceSettingsToConfig(networkSyncSeconds, fullScanIntervalSeconds) {
    const response = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            concurrency: {
                fetch_layer_network_sync_seconds: networkSyncSeconds,
                fetch_layer_full_scan_interval_seconds: fullScanIntervalSeconds,
            },
        }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
        const detail = typeof result.error === 'string' ? result.error : 'Failed to save cadence settings';
        throw new Error(detail);
    }
    return result;
}

function openSettingsForRefreshPrefs() {
    closeFlowRefreshPrefs();
    window.location.href = '/settings';
}

function openFlowRefreshPrefs() {
    hideSettingsMenu();
    const override = getFlowRefreshOverride();
    const cadence = currentNetworkSyncCadence();
    const refresh = window.dashboardData?.refresh || {};
    const cadenceSourceEl = document.getElementById('networkSyncCadenceSource');
    const cadenceOverrideEl = document.getElementById('networkSyncOverrideSeconds');
    const fullScanConfigEl = document.getElementById('fullScanConfigSeconds');
    const saveCadenceToConfigEl = document.getElementById('saveCadenceToConfig');
    const overrideEnabledEl = document.getElementById('flowRefreshOverrideEnabled');
    const enabledEl = document.getElementById('flowRefreshEnabled');
    const staleEl = document.getElementById('flowRefreshStaleSeconds');
    const cooldownEl = document.getElementById('flowRefreshCooldownSeconds');
    const freshnessModeEl = document.getElementById('flowFreshnessMode');
    const apiBudgetEl = document.getElementById('flowApiBudget');
    const attentionPriorityEl = document.getElementById('flowAttentionPriority');
    if (!cadenceSourceEl || !cadenceOverrideEl || !fullScanConfigEl || !saveCadenceToConfigEl || !overrideEnabledEl || !enabledEl || !staleEl || !cooldownEl || !freshnessModeEl || !apiBudgetEl || !attentionPriorityEl || !flowRefreshPrefsModal) return;

    const useOverride = Boolean(override?.enabled);
    const base = currentRefreshConfig();
    cadenceSourceEl.value = cadence.source;
    cadenceOverrideEl.value = String(cadence.seconds);
    fullScanConfigEl.value = String(Number(refresh.fullScanIntervalSeconds || 1800));
    saveCadenceToConfigEl.checked = false;
    cadenceSourceEl.onchange = () => updateCadencePreferenceInputState();
    saveCadenceToConfigEl.onchange = () => updateCadencePreferenceInputState();
    updateCadencePreferenceInputState();
    overrideEnabledEl.checked = useOverride;
    const server = serverRefreshStrategy();
    enabledEl.checked = useOverride ? Boolean(override.flowLazyEnabled) : Boolean(server.flowLazyEnabled);
    staleEl.value = String(useOverride ? Number(override.flowStaleSeconds) : Number(base.staleSeconds || 900));
    cooldownEl.value = String(useOverride ? Number(override.flowCooldownSeconds) : Number(base.cooldownSeconds || 120));
    freshnessModeEl.value = useOverride ? String(override.freshnessMode || 'balanced') : String(server.freshnessMode || 'balanced');
    apiBudgetEl.value = useOverride ? String(override.apiBudget || 'medium') : String(server.apiBudget || 'medium');
    attentionPriorityEl.value = useOverride ? String(override.attentionPriority || 'strict') : String(server.attentionPriority || 'strict');
    setFlowRefreshInputsEnabled(useOverride);
    flowRefreshPrefsModal.classList.add('visible');
}

function closeFlowRefreshPrefs(e) {
    if (!e || e.target === flowRefreshPrefsModal) {
        flowRefreshPrefsModal?.classList.remove('visible');
    }
}

async function saveFlowRefreshPrefs() {
    const cadenceSourceEl = document.getElementById('networkSyncCadenceSource');
    const cadenceOverrideEl = document.getElementById('networkSyncOverrideSeconds');
    const fullScanConfigEl = document.getElementById('fullScanConfigSeconds');
    const saveCadenceToConfigEl = document.getElementById('saveCadenceToConfig');
    const overrideEnabledEl = document.getElementById('flowRefreshOverrideEnabled');
    const enabledEl = document.getElementById('flowRefreshEnabled');
    const staleEl = document.getElementById('flowRefreshStaleSeconds');
    const cooldownEl = document.getElementById('flowRefreshCooldownSeconds');
    const freshnessModeEl = document.getElementById('flowFreshnessMode');
    const apiBudgetEl = document.getElementById('flowApiBudget');
    const attentionPriorityEl = document.getElementById('flowAttentionPriority');
    if (!cadenceSourceEl || !cadenceOverrideEl || !fullScanConfigEl || !saveCadenceToConfigEl || !overrideEnabledEl || !enabledEl || !staleEl || !cooldownEl || !freshnessModeEl || !apiBudgetEl || !attentionPriorityEl) return;

    const networkSyncSeconds = Math.max(5, Number(cadenceOverrideEl.value || 60));
    const fullScanIntervalSeconds = Math.max(60, Number(fullScanConfigEl.value || 1800));
    const saveCadenceToConfig = Boolean(saveCadenceToConfigEl.checked);

    if (saveCadenceToConfig) {
        try {
            const result = await saveCadenceSettingsToConfig(networkSyncSeconds, fullScanIntervalSeconds);
            localStorage.removeItem(NETWORK_SYNC_OVERRIDE_KEY);
            if (window.dashboardData?.refresh) {
                window.dashboardData.refresh.networkSyncSeconds = networkSyncSeconds;
                window.dashboardData.refresh.fullScanIntervalSeconds = fullScanIntervalSeconds;
            }
            if (viewModel?.dashboard_data?.refresh) {
                viewModel.dashboard_data.refresh.networkSyncSeconds = networkSyncSeconds;
                viewModel.dashboard_data.refresh.fullScanIntervalSeconds = fullScanIntervalSeconds;
            }
            if (result && result.restart_required) {
                showToast('Cadence settings saved; restart required for full effect', 'warning');
            }
        } catch (err) {
            showToast(`Failed to save cadence settings: ${err.message}`, 'error');
            return;
        }
    } else if (cadenceSourceEl.value === 'override') {
        localStorage.setItem(NETWORK_SYNC_OVERRIDE_KEY, JSON.stringify({ enabled: true, seconds: networkSyncSeconds }));
    } else {
        localStorage.removeItem(NETWORK_SYNC_OVERRIDE_KEY);
    }

    if (!overrideEnabledEl.checked) {
        localStorage.removeItem(FLOW_REFRESH_OVERRIDE_KEY);
        updateRefreshStatusFromViewModel(viewModel);
        applyNetworkSyncScheduler();
        initFlowLazyVisibleRefresh();
        closeFlowRefreshPrefs();
        showToast('Refresh preferences saved');
        return;
    }

    const staleSeconds = Math.max(60, Number(staleEl.value || 900));
    const cooldownSeconds = Math.max(0, Number(cooldownEl.value || 120));
    const payload = {
        enabled: true,
        flowLazyEnabled: Boolean(enabledEl.checked),
        flowStaleSeconds: staleSeconds,
        flowCooldownSeconds: cooldownSeconds,
        freshnessMode: ['aggressive', 'balanced', 'economy'].includes(freshnessModeEl.value) ? freshnessModeEl.value : 'balanced',
        apiBudget: ['low', 'medium', 'high'].includes(apiBudgetEl.value) ? apiBudgetEl.value : 'medium',
        attentionPriority: ['strict', 'normal'].includes(attentionPriorityEl.value) ? attentionPriorityEl.value : 'strict',
    };
    localStorage.setItem(FLOW_REFRESH_OVERRIDE_KEY, JSON.stringify(payload));
    updateRefreshStatusFromViewModel(viewModel);
    applyNetworkSyncScheduler();
    initFlowLazyVisibleRefresh();
    closeFlowRefreshPrefs();
    showToast('Refresh preferences saved');
}

function resetFlowRefreshPrefs() {
    localStorage.removeItem(NETWORK_SYNC_OVERRIDE_KEY);
    localStorage.removeItem(FLOW_REFRESH_OVERRIDE_KEY);
    updateRefreshStatusFromViewModel(viewModel);
    applyNetworkSyncScheduler();
    initFlowLazyVisibleRefresh();
    closeFlowRefreshPrefs();
    showToast('Flow refresh preferences reset');
}
// Shutdown state - used to cancel polling when "Shutdown now" is clicked
let shutdownInProgress = false;

async function shutdown() {
    // First, check if there are active sessions
    const statusRes = await fetch('/api/status');
    const status = await statusRes.json();
    const activeSessions = status.active_sessions || [];

    if (activeSessions.length > 0) {
        // Show modal with options — make user decide about active work
        showShutdownModal(activeSessions);
    } else {
        // Nothing running — close immediately, no confirmation needed
        showToast('Shutting down...');
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
                <ul style="margin: 12px 0; padding-left: 20px; color: var(--text);">${sessionList}</ul>
                <p style="color: var(--text-muted); font-size: 0.9em;">
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
            <p style="color: var(--text-muted); font-size: 0.9em; margin-top: 12px;">
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
    document.body.innerHTML = '<div style="display:flex;justify-content:center;align-items:center;height:100vh;color:var(--text-muted);flex-direction:column;gap:12px;"><span>Orchestrator stopped.</span><span style="font-size:0.9em;">You can close this tab.</span></div>';
}

// Tab switching
function switchTab(tab) {
    document.body.classList.add('tab-nav-pending');
    document.querySelectorAll('.dashboard-tabs .tab').forEach((btn) => {
        btn.classList.remove('is-loading');
        btn.removeAttribute('aria-busy');
    });
    const targetTab = document.querySelector(`.dashboard-tabs .tab[data-tab="${cssEscape(tab)}"]`);
    if (targetTab) {
        targetTab.classList.add('is-loading');
        targetTab.setAttribute('aria-busy', 'true');
    }
    const url = new URL(window.location.href);
    url.searchParams.set('tab', tab);
    url.searchParams.set('page', '1');  // Reset to page 1 when switching tabs
    window.location.href = url.toString();
}

// Keyboard navigation for tabs (accessibility)
const tabButtons = Array.from(document.querySelectorAll('.dashboard-tabs .tab'));
const tabOrder = tabButtons
    .map((btn) => btn.dataset.tab)
    .filter((tabName) => typeof tabName === 'string' && tabName.length > 0);
tabButtons.forEach(tabBtn => {
    tabBtn.addEventListener('keydown', (e) => {
        const currentTab = tabBtn.dataset.tab || tabBtn.id.replace('tab-', '');
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
            const newTabBtn = document.querySelector(`.dashboard-tabs .tab[data-tab="${tabOrder[newIndex]}"]`);
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

// ── Blocked triage: viewed state (localStorage) ──

const VIEWED_ISSUES_KEY = 'issue-orchestrator.blocked-viewed.v1';

function getViewedIssues() {
    try {
        return new Set(JSON.parse(localStorage.getItem(VIEWED_ISSUES_KEY) || '[]'));
    } catch { return new Set(); }
}

function setViewedIssues(issueNumbers) {
    localStorage.setItem(VIEWED_ISSUES_KEY, JSON.stringify([...issueNumbers]));
}

function markIssuesViewed(numbers) {
    const viewed = getViewedIssues();
    numbers.forEach(n => viewed.add(n));
    setViewedIssues(viewed);
}

function clearIssuesViewed(numbers) {
    const viewed = getViewedIssues();
    numbers.forEach(n => viewed.delete(n));
    setViewedIssues(viewed);
}

const OPTIMISTIC_REQUEUE_HIDE_MS = 30_000;
const optimisticRequeueSuppressions = new Map();

function _optimisticSuppressionKey(issueNumber, columnId) {
    return `${columnId}:${issueNumber}`;
}

function pruneOptimisticSuppressions() {
    const now = Date.now();
    for (const [key, expiresAt] of optimisticRequeueSuppressions.entries()) {
        if (expiresAt <= now) optimisticRequeueSuppressions.delete(key);
    }
}

function suppressIssueInColumn(issueNumber, columnId) {
    optimisticRequeueSuppressions.set(
        _optimisticSuppressionKey(issueNumber, columnId),
        Date.now() + OPTIMISTIC_REQUEUE_HIDE_MS,
    );
}

function isIssueSuppressedInColumn(issueNumber, columnId) {
    pruneOptimisticSuppressions();
    const expiresAt = optimisticRequeueSuppressions.get(_optimisticSuppressionKey(issueNumber, columnId));
    return Boolean(expiresAt && expiresAt > Date.now());
}

function filterSuppressedItems(items, columnId) {
    return (items || []).filter((item) => !isIssueSuppressedInColumn(Number(item.issue_number), columnId));
}

function ensureCompactEmptyState(cardsEl) {
    if (!cardsEl) return;
    const hasCards = cardsEl.querySelector('.issue-card');
    const emptyNode = cardsEl.querySelector('.column-empty');
    if (!hasCards && !emptyNode) {
        cardsEl.innerHTML = '<div class="column-empty">No items</div>';
    } else if (hasCards && emptyNode) {
        emptyNode.remove();
    }
}

function applyOptimisticRequeue(issueNumbers, sourceColumns) {
    const normalizedIssues = (issueNumbers || [])
        .map((n) => Number(n))
        .filter((n) => Number.isFinite(n));
    const normalizedColumns = (sourceColumns || [])
        .map((c) => String(c || '').trim())
        .filter(Boolean);
    if (!normalizedIssues.length || !normalizedColumns.length) return;

    for (const columnId of normalizedColumns) {
        const col = document.querySelector(`[data-column="${cssEscape(columnId)}"]`);
        if (!col) continue;

        let removedCount = 0;
        for (const issueNumber of normalizedIssues) {
            suppressIssueInColumn(issueNumber, columnId);

            const compactCard = col.querySelector(`.issue-card[data-issue="${issueNumber}"]`);
            if (compactCard) {
                compactCard.remove();
                removedCount += 1;
            }

            const expandedCard = col.querySelector(`.expanded-card[data-issue="${issueNumber}"]`);
            if (expandedCard) expandedCard.remove();
        }

        ensureCompactEmptyState(col.querySelector('.column-cards'));
        const countEl = col.querySelector('h2 .count');
        if (countEl && removedCount > 0) {
            const current = Number.parseInt(countEl.textContent || '0', 10);
            countEl.textContent = String(Math.max(0, (Number.isNaN(current) ? 0 : current) - removedCount));
        }

        if (columnId === 'blocked') {
            const items = getAllBlockedItems(col);
            updateBlockedNewCount(col, items, getViewedIssues());
            applyBlockedFilter(col);
        }
        updateBulkBar(columnId);
    }
}

// ── Kanban column expand/collapse ──

function renderCompactCardHtml(card) {
    const n = card.issue_number;
    const cardId = String(card.card_id || `issue-${n}`);
    const staleAttr = card.is_stale ? 'true' : 'false';
    const staleDot = card.is_stale
        ? `<span class="stale-dot" title="${card.stale_reason || 'Issue may be stale'}" aria-label="Issue data may be stale"></span>`
        : '';
    const staleBadge = card.is_stale
        ? '<span class="badge badge-stale" title="Data may be stale">stale</span>'
        : '';
    const ghLink = card.issue_url
        ? `<a class="card-gh" href="${card.issue_url}" target="_blank" rel="noopener noreferrer" title="Open in GitHub">&#x2197;</a>`
        : '';
    const hasTerminal = card.state_label === 'running' ? 'true' : 'false';
    const action = card.state_label === 'running' ? 'focus' : 'open';
    const menuButton = `<button class="card-menu-btn"
        data-issue="${n}"
        data-title="${escapeAttr(String(card.title || ''))}"
        data-issue-url="${escapeAttr(String(card.issue_url || ''))}"
        data-pr-url="${escapeAttr(String(card.pr_url || ''))}"
        data-status="${escapeAttr(String(card.state_label || ''))}"
        data-row-action="${escapeAttr(String(action || ''))}"
        data-agent="${escapeAttr(String(card.agent_type || ''))}"
        data-has-terminal="${hasTerminal}"
        onclick="openCompactCardActionsMenu(event, this)"
        title="More actions for issue #${n}"
        aria-label="More actions for issue #${n}">&#x22EE;</button>`;
    const phaseLine = card.phase || card.state_label || '';
    const ageStr = card.phase_age ? ` &middot; ${card.phase_age}` : '';
    const queueWaitLine = card.queue_wait_reason
        ? `<div class="card-line card-wait">${escapeHtml(String(card.queue_wait_reason))}</div>`
        : '';
    let detailLine = '';
    if (card.summary && !card.queue_wait_reason) {
        detailLine = `<div class="card-line card-muted">${card.summary}</div>`;
    }
    const orchLabels = card.orchestrator_labels || [];
    const orchPills = orchLabels.map(l => `<span class="badge badge-orch">${l}</span>`).join('');
    const allBadges = orchPills + staleBadge;
    const badgesDiv = allBadges
        ? `<div class="card-badges">${allBadges}</div>`
        : '';
    return `<div class="issue-card" data-card-id="${cardId}" data-issue="${n}" data-stale="${staleAttr}" data-last-refresh-age-seconds="${card.last_refreshed_age_seconds || 0}">
        <div class="card-top">
            <button class="card-focus" onclick="openIssueDetail(${n}, this);event.stopPropagation();" title="Focus issue #${n}">
                #${n} ${card.title}
            </button>
            <div class="card-head-actions">
                ${staleDot}
                <button class="card-refresh-btn" onclick="refreshIssueCard(${n}, this);event.stopPropagation();" title="Refresh issue #${n} from GitHub" aria-label="Refresh issue #${n}">&#x27F3;</button>
                ${ghLink}
                ${menuButton}
                <button class="card-detail-chevron" onclick="openIssueDetail(${n}, this);event.stopPropagation();" title="View details" aria-label="View issue #${n} details">&#x25B8;</button>
            </div>
        </div>
        <div class="card-line">${phaseLine}${ageStr}</div>
        ${queueWaitLine}
        ${detailLine}
        ${badgesDiv}
    </div>`;
}

function renderCompactCards(container, items) {
    if (!items.length) {
        container.innerHTML = '<div class="column-empty">No items</div>';
        return;
    }

    const nextIds = new Set(items.map((card) => String(card.card_id || `issue-${card.issue_number}`)));
    const existingCards = Array.from(container.querySelectorAll('.issue-card[data-card-id], .issue-card[data-issue]'));
    const existingById = new Map(existingCards.map((card) => [String(card.dataset.cardId || `issue-${card.dataset.issue || ''}`), card]));
    existingCards.forEach((card) => {
        const existingId = String(card.dataset.cardId || `issue-${card.dataset.issue || ''}`);
        if (!nextIds.has(existingId)) {
            card.remove();
        }
    });

    let insertAfter = null;
    for (const card of items) {
        const id = String(card.card_id || `issue-${card.issue_number}`);
        const existing = existingById.get(id) || null;
        const nextFingerprint = compactCardState.computeCompactCardFingerprint(card);
        let node = existing;

        if (!existing || existing.dataset.cardFingerprint !== nextFingerprint) {
            const wrapper = document.createElement('div');
            wrapper.innerHTML = renderCompactCardHtml(card).trim();
            const newNode = wrapper.firstElementChild;
            if (!newNode) continue;
            newNode.dataset.cardFingerprint = nextFingerprint;
            if (existing) {
                existing.replaceWith(newNode);
            } else if (insertAfter) {
                insertAfter.after(newNode);
            } else {
                container.prepend(newNode);
            }
            node = newNode;
        } else {
            existing.dataset.cardFingerprint = nextFingerprint;
        }

        if (!node) continue;
        if (insertAfter) {
            if (node.previousElementSibling !== insertAfter) {
                insertAfter.after(node);
            }
        } else if (node.parentElement !== container || node !== container.firstElementChild) {
            container.prepend(node);
        }
        insertAfter = node;
    }
}

const expandedColumnFingerprints = new Map();

function getSelectedIssueSet(columnId) {
    return new Set(getSelectedIssueNumbers(columnId));
}

function reapplyExpandedSelections(columnId, selectedIssues) {
    if (!selectedIssues || selectedIssues.size === 0) return;
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return;
    col.querySelectorAll('.expanded-card').forEach(card => {
        const issueNumber = Number(card.dataset.issue);
        const checkbox = card.querySelector('.card-checkbox');
        if (!checkbox || isNaN(issueNumber)) return;
        checkbox.checked = selectedIssues.has(issueNumber);
    });
}

function toggleColumnExpand(columnId) {
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return;
    const isExpanded = col.dataset.expanded === 'true';

    // Collapse all first — also clear stale checkbox/bulk state
    document.querySelectorAll('.kanban-column').forEach(c => {
        c.classList.remove('expanded', 'collapsed-peer');
        c.dataset.expanded = 'false';
        const expanded = c.querySelector('.column-expanded');
        const cards = c.querySelector('.column-cards');
        if (expanded) expanded.style.display = 'none';
        if (cards) cards.style.display = '';
        // Reset checkboxes and bulk bar so stale state doesn't flash on re-expand
        c.querySelectorAll('.card-checkbox:checked').forEach(cb => { cb.checked = false; });
        const bar = c.querySelector('.bulk-action-bar');
        if (bar) {
            bar.style.display = 'none';
            const countEl = bar.querySelector('.selected-count');
            if (countEl) countEl.textContent = '0 selected';
            if (c.dataset.column === 'blocked') {
                bar.querySelectorAll('.issue-action-btn').forEach((btn) => {
                    btn.disabled = true;
                });
            }
        }
    });

    if (!isExpanded) {
        col.classList.add('expanded');
        col.dataset.expanded = 'true';
        const expanded = col.querySelector('.column-expanded');
        const cards = col.querySelector('.column-cards');
        if (expanded) expanded.style.display = '';
        if (cards) cards.style.display = 'none';
        // Collapse peers
        document.querySelectorAll('.kanban-column:not(.expanded)').forEach(c => {
            c.classList.add('collapsed-peer');
        });
        updateBulkBar(columnId);
        loadExpandedColumn(columnId, { forceRebuild: true });
    }

    document.body.classList.toggle('column-focus-mode', !isExpanded);
    updateEmbeddedBackButtonVisibility();
}

async function loadExpandedColumn(columnId, options = {}) {
    const forceRebuild = Boolean(options.forceRebuild);
    let vm = options.viewModel || null;
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return;
    const expandedList = col.querySelector('.expanded-cards-list');
    if (!expandedList) return;
    const previousSelection = getSelectedIssueSet(columnId);

    try {
        if (!vm) {
            const resp = await fetch(`/api/view-model?tab=${columnId}`);
            if (!resp.ok) return;
            vm = await resp.json();
        }
        const items = filterSuppressedItems(expandedColumnState.getExpandedItemsFromViewModel(vm, columnId), columnId);
        const nextFingerprint = expandedColumnState.computeExpandedItemsFingerprint(items, {
            columnId,
            viewedIssueNumbers: columnId === 'blocked' ? [...getViewedIssues()] : [],
        });
        const prevFingerprint = expandedColumnFingerprints.get(columnId);
        const shouldRebuild = forceRebuild || prevFingerprint !== nextFingerprint;

        if (shouldRebuild) {
            const viewed = columnId === 'blocked' ? getViewedIssues() : new Set();
            expandedList.innerHTML = items.map(item => {
                const isViewed = viewed.has(item.issue_number);
                const n = item.issue_number;
                const orchLabels = item.orchestrator_labels || [];
                const orchPills = orchLabels.map((label) => `<span class="badge badge-orch">${label}</span>`).join('');
                const badgesDiv = orchPills
                    ? `<div class="card-badges">${orchPills}</div>`
                    : '';
                const queueWaitReason = item.queue_wait_reason || '';
                const detailText = queueWaitReason || item.detail_label || item.status || '';
                const detailClass = queueWaitReason ? 'card-line card-wait' : 'card-line card-muted';
                const detailDiv = detailText
                    ? `<div class="${detailClass}">${escapeHtml(String(detailText))}</div>`
                    : '';
                return `
                <div class="expanded-card${isViewed ? ' viewed' : ''}" data-issue="${n}" data-viewed="${isViewed}">
                    <input type="checkbox" class="card-checkbox" onchange="updateBulkBar('${columnId}')">
                    <div class="card-content">
                        <button class="card-focus" onclick="openIssueDetail(${n}, this);event.stopPropagation();"
                                title="Focus issue #${n}">
                            #${n} ${item.title || ''}
                        </button>
                        ${detailDiv}
                        ${badgesDiv}
                    </div>
                    <div class="card-actions">
                        ${columnId === 'blocked' ? `<button class="card-action-btn card-action-unblock" onclick="unblockSingle(${n}, this);event.stopPropagation();" title="Unblock issue #${n}">Unblock</button>` : ''}
                        ${columnId === 'blocked' ? `<button class="card-action-btn card-action-reset" onclick="resetRetrySingle(${n}, this);event.stopPropagation();" title="Full reset and requeue issue #${n}">Reset & Retry</button>` : ''}
                        ${columnId === 'blocked' ? `<button class="card-action-btn card-action-reset" onclick="resetRetrySingleFromScratch(${n}, this);event.stopPropagation();" title="Full reset and requeue issue #${n} from a fresh branch based on main">Reset & Retry From Scratch</button>` : ''}
                        ${columnId === 'running' ? `<button class="card-action-btn card-action-reset" onclick="killExpandedSingle(${n}, this);event.stopPropagation();" title="Terminate issue #${n} and place on hold">Cancel</button>` : ''}
                        ${columnId === 'queued' ? `<button class="card-action-btn card-action-reset" onclick="cancelQueuedSingle(${n}, this);event.stopPropagation();" title="Place queued issue #${n} on hold">Cancel</button>` : ''}
                        ${columnId === 'awaiting-merge' ? `<button class="card-action-btn card-action-unblock" onclick="retryExpandedSingle(${n}, 'awaiting-merge', this);event.stopPropagation();" title="Remove pr-pending and requeue issue #${n}">Retry</button>` : ''}
                        ${columnId === 'awaiting-merge' ? `<button class="card-action-btn card-action-reset" onclick="resetRetrySingle(${n}, this);event.stopPropagation();" title="Full reset and requeue issue #${n}">Reset & Retry</button>` : ''}
                        ${columnId === 'awaiting-merge' ? `<button class="card-action-btn card-action-reset" onclick="resetRetrySingleFromScratch(${n}, this);event.stopPropagation();" title="Full reset and requeue issue #${n} from a fresh branch based on main">Reset & Retry From Scratch</button>` : ''}
                        ${columnId === 'completed' ? `<button class="card-action-btn card-action-unblock" onclick="retryExpandedSingle(${n}, 'completed', this);event.stopPropagation();" title="Requeue issue #${n} for another run">Retry</button>` : ''}
                    ${item.issue_url ? `<a class="card-gh" href="${item.issue_url}" target="_blank" rel="noopener noreferrer" title="Open in GitHub">↗</a>` : ''}
                        ${item.pr_url ? `<a class="card-action-btn" href="${item.pr_url}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation();">PR</a>` : ''}
                        <button class="card-detail-chevron" onclick="openIssueDetail(${n}, this);event.stopPropagation();" title="View details" aria-label="View issue #${n} details">&#x25B8;</button>
                    </div>
                </div>`;
            }).join('');
            expandedColumnFingerprints.set(columnId, nextFingerprint);
            const reconciledSelection = new Set(
                expandedColumnState.reconcileSelectedIssues([...previousSelection], items),
            );
            reapplyExpandedSelections(columnId, reconciledSelection);
        }

        // Update blocked-only derived UI even when list body is unchanged.
        if (columnId === 'blocked') {
            updateBlockedNewCount(col, items, getViewedIssues());
            applyBlockedFilter(col);
        }
        updateBulkBar(columnId);
    } catch (e) {
        console.error('Failed to load expanded column:', e);
        expandedList.innerHTML = '<div class="column-empty">Failed to load items</div>';
        expandedColumnFingerprints.delete(columnId);
    }
}

function updateBlockedNewCount(col, items, viewed) {
    const newCount = items.filter(item => !viewed.has(item.issue_number)).length;
    let badge = col.querySelector('.new-count-badge');
    if (newCount > 0) {
        if (!badge) {
            badge = document.createElement('span');
            badge.className = 'new-count-badge';
            const h2 = col.querySelector('h2');
            if (h2) h2.appendChild(badge);
        }
        badge.textContent = `${newCount} new`;
        badge.title = `${newCount} blocked issue${newCount === 1 ? '' : 's'} not yet viewed`;
    } else if (badge) {
        badge.remove();
    }
}

function applyBlockedFilter(col) {
    if (!col) col = document.querySelector('[data-column="blocked"]');
    if (!col) return;
    const activeBtn = col.querySelector('.filter-btn.active');
    const filter = activeBtn ? activeBtn.dataset.filter : 'all';
    const cards = col.querySelectorAll('.expanded-card');
    cards.forEach(card => {
        const isViewed = card.dataset.viewed === 'true';
        if (filter === 'all') card.style.display = '';
        else if (filter === 'new') card.style.display = isViewed ? 'none' : '';
        else if (filter === 'viewed') card.style.display = isViewed ? '' : 'none';
    });
}

function filterBlockedColumn(filter, btn) {
    const col = document.querySelector('[data-column="blocked"]');
    if (!col) return;
    col.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    applyBlockedFilter(col);
}

function updateBulkBar(columnId) {
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return;
    const checked = col.querySelectorAll('.card-checkbox:checked');
    const bar = col.querySelector('.bulk-action-bar');
    if (!bar) return;
    const alwaysVisibleColumns = new Set(['blocked', 'awaiting-merge', 'completed', 'running']);
    const alwaysVisible = alwaysVisibleColumns.has(columnId);
    bar.style.display = alwaysVisible || checked.length > 0 ? 'flex' : 'none';
    const countEl = bar.querySelector('.selected-count');
    if (countEl) {
        countEl.textContent = checked.length > 0 ? `${checked.length} selected` : 'No issues selected';
    }
    const actionButtons = bar.querySelectorAll('.issue-action-btn');
    actionButtons.forEach((btn) => {
        const requiresSelection = btn.dataset.requiresSelection !== 'false';
        btn.disabled = requiresSelection && checked.length === 0;
    });
}

async function killExpandedSingle(issueNumber, btn) {
    const confirmMsg = `Cancel running issue #${issueNumber}?\n\nThis will terminate the active session and place the issue on hold.\nIt will not run again until you explicitly retry/unblock it.`;
    if (!await showConfirm(confirmMsg, btn)) return;
    if (btn) btn.disabled = true;
    try {
        const res = await fetch(`/api/kill/${issueNumber}`, { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.status === 'terminated') {
            showToast(`Cancelled #${issueNumber} (on hold)`);
            await refreshViewModel();
        } else {
            showToast(data.error || `Cancel failed (${res.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Cancel failed:', e);
        showToast('Cancel failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

async function bulkKillRunning() {
    const numbers = getSelectedIssueNumbers('running');
    if (!numbers.length) return;
    const confirmMsg = `Cancel ${numbers.length} running issue(s)?\n\nThis will terminate active sessions and place issues on hold.\nThey will not run again until explicitly retried/unblocked.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const res = await fetch('/api/bulk-kill', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ issue_numbers: numbers }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
            const terminated = Array.isArray(data.terminated) ? data.terminated.length : 0;
            const failed = Array.isArray(data.failed) ? data.failed.length : 0;
            if (terminated > 0) showToast(`Cancelled ${terminated} running issue(s)`);
            if (failed > 0) showToast(`Failed to cancel ${failed} issue(s)`, true);
            await refreshViewModel();
            return;
        }
        showToast(data.error || `Bulk cancel failed (${res.status})`, true);
    } catch (e) {
        console.error('Bulk cancel failed:', e);
        showToast('Bulk cancel failed: network error', true);
    }
}

async function bulkKillAllRunning() {
    const col = document.querySelector('[data-column="running"]');
    if (!col) return;
    const allNumbers = Array.from(col.querySelectorAll('.expanded-card'))
        .map((card) => Number(card.dataset.issue))
        .filter((n) => Number.isInteger(n));
    if (!allNumbers.length) {
        showToast('No running issues to cancel');
        return;
    }
    const confirmMsg = `Cancel ALL ${allNumbers.length} running issue(s)?\n\nThis will terminate active sessions and place issues on hold.\nThey will not run again until explicitly retried/unblocked.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const res = await fetch('/api/bulk-kill', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ issue_numbers: allNumbers }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
            const terminated = Array.isArray(data.terminated) ? data.terminated.length : 0;
            const failed = Array.isArray(data.failed) ? data.failed.length : 0;
            if (terminated > 0) showToast(`Cancelled ${terminated} running issue(s)`);
            if (failed > 0) showToast(`Failed to cancel ${failed} issue(s)`, true);
            await refreshViewModel();
            return;
        }
        showToast(data.error || `Cancel all failed (${res.status})`, true);
    } catch (e) {
        console.error('Cancel all failed:', e);
        showToast('Cancel all failed: network error', true);
    }
}

async function cancelQueuedSingle(issueNumber, btn) {
    const confirmMsg = `Cancel queued issue #${issueNumber}?\n\nThis will place the issue on hold before it launches.\nIt will not run again until you explicitly retry/unblock it.`;
    if (!await showConfirm(confirmMsg, btn)) return;
    if (btn) btn.disabled = true;
    try {
        const req = uiActionContract.buildBulkCancelQueuedRequest([issueNumber]);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && Array.isArray(data.cancelled) && data.cancelled.includes(issueNumber)) {
            showToast(`Cancelled queued issue #${issueNumber}`);
            await refreshViewModel();
            return;
        }
        showToast(data.error || `Queue cancel failed (${res.status})`, true);
        if (btn) btn.disabled = false;
    } catch (e) {
        console.error('Queue cancel failed:', e);
        showToast('Queue cancel failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

async function bulkRefreshRunning() {
    const numbers = getSelectedIssueNumbers('running');
    if (!numbers.length) return;
    const confirmMsg = `Refresh ${numbers.length} running issue(s) from GitHub now?`;
    if (!await showConfirm(confirmMsg)) return;
    const failures = [];
    for (const issueNumber of numbers) {
        try {
            const res = await fetch(`/api/issues/${issueNumber}/refresh`, { method: 'POST' });
            if (!res.ok) failures.push(issueNumber);
        } catch (_) {
            failures.push(issueNumber);
        }
    }
    if (failures.length === 0) {
        showToast(`Refreshed ${numbers.length} running issue(s)`);
    } else {
        showToast(`Refresh failed for ${failures.length} issue(s)`, true);
    }
    await refreshViewModel();
}

function getSelectedIssueNumbers(columnId) {
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return [];
    return Array.from(col.querySelectorAll('.expanded-card'))
        .filter(card => card.querySelector('.card-checkbox:checked'))
        .map(card => Number(card.dataset.issue))
        .filter(n => !isNaN(n));
}

async function unblockSingle(issueNumber, btn) {
    const confirmMsg = `Requeue issue #${issueNumber}?\n\nThis will REMOVE retry-gating labels (including blocking labels and pr-pending).\n\nIt will not delete the local worktree or remote branch.`;
    if (!await showConfirm(confirmMsg, btn || lastContextMenuPoint)) return;
    if (btn) btn.disabled = true;
    try {
        const req = uiActionContract.buildUnblockRequest([issueNumber]);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        if (resp.ok) {
            applyOptimisticRequeue([issueNumber], ['blocked']);
            showToast(`Unblocked #${issueNumber} → Queued`);
            await refreshViewModel();
        } else {
            const data = await resp.json().catch(() => ({}));
            showToast(data.error || `Unblock failed (${resp.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Unblock failed:', e);
        showToast('Unblock failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

async function bulkUnblock() {
    const numbers = getSelectedIssueNumbers('blocked');
    if (!numbers.length) return;
    const confirmMsg = `Requeue ${numbers.length} issue(s)?\n\nThis will REMOVE retry-gating labels (including blocking labels and pr-pending).\n\nIt will not delete local worktrees or remote branches.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildUnblockRequest(numbers);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        if (resp.ok) {
            applyOptimisticRequeue(numbers, ['blocked']);
            showToast(`Unblocking ${numbers.length} issue(s) → Queued`);
            await refreshViewModel();
        } else {
            const data = await resp.json().catch(() => ({}));
            showToast(data.error || `Bulk unblock failed (${resp.status})`, true);
        }
    } catch (e) {
        console.error('Bulk unblock failed:', e);
        showToast('Bulk unblock failed: network error', true);
    }
}

async function bulkResetRetry() {
    const numbers = getSelectedIssueNumbers('blocked').concat(getSelectedIssueNumbers('awaiting-merge'));
    if (!numbers.length) return;
    const confirmMsg = `Full reset and requeue ${numbers.length} issue(s)?\n\nThis will DELETE:\n• Local worktrees\n• Remote branches\n• Orchestrator labels\n\nAfter reset, the issues will be requeued for a fresh retry.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildResetRetryRequest(numbers, { fromScratch: false });
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.reset && data.reset.length > 0) {
            applyOptimisticRequeue(data.reset, ['blocked', 'awaiting-merge']);
            showToast(`Reset ${data.reset.length} issue(s) → Queued`);
            await refreshViewModel();
        } else if (res.ok && data.failed && data.failed.length > 0) {
            showToast(`Failed to reset some issues: ${data.failed.map((f) => f.error).join(', ')}`, true);
        } else {
            showToast(data.error || `Reset failed (${res.status})`, true);
        }
    } catch (e) {
        console.error('Bulk reset failed:', e);
        showToast('Bulk reset failed: network error', true);
    }
}

async function bulkResetRetryFromScratch() {
    const numbers = getSelectedIssueNumbers('blocked').concat(getSelectedIssueNumbers('awaiting-merge'));
    if (!numbers.length) return;
    const confirmMsg = `Full reset and requeue ${numbers.length} issue(s) from scratch?\n\nThis will DELETE:\n• Local worktrees\n• Remote branches\n• Orchestrator labels\n\nNext launch will force NEW branches from base (main), not prior issue branch history.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildResetRetryRequest(numbers, { fromScratch: true });
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.reset && data.reset.length > 0) {
            applyOptimisticRequeue(data.reset, ['blocked', 'awaiting-merge']);
            showToast(`Reset ${data.reset.length} issue(s) from scratch → Queued`);
            await refreshViewModel();
        } else if (res.ok && data.failed && data.failed.length > 0) {
            showToast(`Failed to reset some issues: ${data.failed.map((f) => f.error).join(', ')}`, true);
        } else {
            showToast(data.error || `Reset failed (${res.status})`, true);
        }
    } catch (e) {
        console.error('Bulk reset from scratch failed:', e);
        showToast('Bulk reset from scratch failed: network error', true);
    }
}

async function resetRetrySingle(issueNumber, btn) {
    const confirmMsg = `Full reset and requeue issue #${issueNumber}?\n\nThis will DELETE:\n• Local worktree\n• Remote branch\n• Orchestrator labels\n\nAfter reset, the issue will be requeued for a fresh retry.`;
    if (!await showConfirm(confirmMsg, btn || lastContextMenuPoint)) return;
    await performResetRetry(issueNumber, btn, { fromScratch: false });
}

async function resetRetrySingleFromScratch(issueNumber, btn) {
    const confirmMsg = `Full reset and requeue issue #${issueNumber} from scratch?\n\nThis will DELETE:\n• Local worktree\n• Remote branch\n• Orchestrator labels\n\nNext launch will force a NEW branch from base (main), not prior issue branch history.`;
    if (!await showConfirm(confirmMsg, btn || lastContextMenuPoint)) return;
    await performResetRetry(issueNumber, btn, { fromScratch: true });
}

async function performResetRetry(issueNumber, btn, options = {}) {
    if (btn) btn.disabled = true;
    try {
        const fromScratch = Boolean(options.fromScratch);
        const req = uiActionContract.buildResetRetryRequest([issueNumber], { fromScratch });
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.reset && data.reset.length > 0) {
            applyOptimisticRequeue(data.reset, ['blocked']);
            showToast(fromScratch ? `Reset #${issueNumber} from scratch → Queued` : `Reset #${issueNumber} → Queued`);
            await refreshViewModel();
        } else if (res.ok && data.failed && data.failed.length > 0) {
            showToast(`Reset failed: ${data.failed.map((f) => f.error).join(', ')}`, true);
            if (btn) btn.disabled = false;
        } else {
            showToast(data.error || `Reset failed (${res.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Single reset failed:', e);
        showToast('Reset failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

function bulkMarkViewed() {
    const numbers = getSelectedIssueNumbers('blocked');
    if (!numbers.length) return;
    markIssuesViewed(numbers);
    // Update card visuals immediately
    const col = document.querySelector('[data-column="blocked"]');
    if (col) {
        numbers.forEach(n => {
            const card = col.querySelector(`.expanded-card[data-issue="${n}"]`);
            if (card) { card.classList.add('viewed'); card.dataset.viewed = 'true'; }
        });
        updateBlockedNewCount(col, getAllBlockedItems(col), getViewedIssues());
        applyBlockedFilter(col);
    }
    // Deselect checkboxes
    uncheckAll('blocked');
    showToast(`Marked ${numbers.length} issue(s) as viewed`);
}

function bulkClearViewed() {
    const numbers = getSelectedIssueNumbers('blocked');
    if (!numbers.length) return;
    clearIssuesViewed(numbers);
    const col = document.querySelector('[data-column="blocked"]');
    if (col) {
        numbers.forEach(n => {
            const card = col.querySelector(`.expanded-card[data-issue="${n}"]`);
            if (card) { card.classList.remove('viewed'); card.dataset.viewed = 'false'; }
        });
        updateBlockedNewCount(col, getAllBlockedItems(col), getViewedIssues());
        applyBlockedFilter(col);
    }
    uncheckAll('blocked');
    showToast(`Cleared viewed status for ${numbers.length} issue(s)`);
}

function getAllBlockedItems(col) {
    return Array.from(col.querySelectorAll('.expanded-card'))
        .map(card => ({ issue_number: Number(card.dataset.issue) }));
}

function uncheckAll(columnId) {
    const col = document.querySelector(`[data-column="${columnId}"]`);
    if (!col) return;
    col.querySelectorAll('.card-checkbox:checked').forEach(cb => { cb.checked = false; });
    updateBulkBar(columnId);
}

function bulkOpenPRs() {
    const col = document.querySelector('[data-column="awaiting-merge"]');
    if (!col) return;
    const cards = Array.from(col.querySelectorAll('.expanded-card'))
        .filter(card => card.querySelector('.card-checkbox:checked'));
    cards.forEach(card => {
        const link = card.querySelector('.card-gh');
        if (link && link.href) window.open(link.href, '_blank');
    });
}

async function retryExpandedSingle(issueNumber, columnId, btn) {
    if (columnId === 'awaiting-merge') {
        const confirmMsg = `Retry issue #${issueNumber} from Awaiting Merge?\n\nThis will REMOVE pr-pending and requeue the issue for another run.\n\nUse this when you want new work despite an existing PR state.`;
        if (!await showConfirm(confirmMsg, btn)) return;
        if (btn) btn.disabled = true;
        try {
            const req = uiActionContract.buildUnblockRequest([issueNumber]);
            const resp = await fetch(req.endpoint, {
                method: req.method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(req.body),
            });
            if (resp.ok) {
                applyOptimisticRequeue([issueNumber], ['awaiting-merge']);
                showToast(`Retrying #${issueNumber} from Awaiting Merge`);
                await refreshViewModel();
            } else {
                const data = await resp.json().catch(() => ({}));
                showToast(data.error || `Retry failed (${resp.status})`, true);
                if (btn) btn.disabled = false;
            }
        } catch (e) {
            console.error('Retry failed:', e);
            showToast('Retry failed: network error', true);
            if (btn) btn.disabled = false;
        }
        return;
    }

    const confirmMsg = `Retry completed issue #${issueNumber}?\n\nThis will requeue the issue for another run.\nUse this when you want the agent to re-run with newer context.`;
    if (!await showConfirm(confirmMsg, btn)) return;
    if (btn) btn.disabled = true;
    try {
        const req = uiActionContract.buildBulkRetryRequest([issueNumber]);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.retried && data.retried.length > 0) {
            applyOptimisticRequeue(data.retried, ['completed']);
            showToast(`Retrying completed issue #${issueNumber}`);
            await refreshViewModel();
        } else {
            showToast(data.error || `Retry failed (${resp.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Retry failed:', e);
        showToast('Retry failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

async function bulkRetryAwaitingMerge() {
    const numbers = getSelectedIssueNumbers('awaiting-merge');
    if (!numbers.length) return;
    const confirmMsg = `Retry ${numbers.length} Awaiting Merge issue(s)?\n\nThis will REMOVE pr-pending and requeue selected issues for another run.\n\nUse this when you intentionally want a new run before merge.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildUnblockRequest(numbers);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        if (resp.ok) {
            applyOptimisticRequeue(numbers, ['awaiting-merge']);
            showToast(`Retrying ${numbers.length} Awaiting Merge issue(s)`);
            await refreshViewModel();
        } else {
            const data = await resp.json().catch(() => ({}));
            showToast(data.error || `Retry failed (${resp.status})`, true);
        }
    } catch (e) {
        console.error('Bulk retry failed:', e);
        showToast('Bulk retry failed: network error', true);
    }
}

async function bulkRetryCompleted() {
    const numbers = getSelectedIssueNumbers('completed');
    if (!numbers.length) return;
    const confirmMsg = `Retry ${numbers.length} completed issue(s)?\n\nThis will requeue selected issues for another run.\nUse this when you want re-execution with newer context or codebase changes.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildBulkRetryRequest(numbers);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.retried && data.retried.length > 0) {
            applyOptimisticRequeue(data.retried, ['completed']);
            showToast(`Retrying ${data.retried.length} completed issue(s)`);
            await refreshViewModel();
        } else {
            showToast(data.error || `Retry failed (${resp.status})`, true);
        }
    } catch (e) {
        console.error('Bulk retry failed:', e);
        showToast('Bulk retry failed: network error', true);
    }
}

async function bulkDeprioritize() {
    const numbers = getSelectedIssueNumbers('queued');
    if (!numbers.length) return;
    try {
        const req = uiActionContract.buildBulkDeprioritizeRequest(numbers);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        if (resp.ok) {
            showToast(`Deprioritized ${numbers.length} issue(s)`);
            await refreshViewModel();
        }
    } catch (e) {
        console.error('Bulk deprioritize failed:', e);
    }
}

async function bulkCancelQueued() {
    const numbers = getSelectedIssueNumbers('queued');
    if (!numbers.length) return;
    const confirmMsg = `Cancel ${numbers.length} queued issue(s)?\n\nThis will place them on hold before launch.\nThey will not run again until explicitly retried/unblocked.`;
    if (!await showConfirm(confirmMsg)) return;
    try {
        const req = uiActionContract.buildBulkCancelQueuedRequest(numbers);
        const res = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
            const cancelled = Array.isArray(data.cancelled) ? data.cancelled.length : 0;
            const failed = Array.isArray(data.failed) ? data.failed.length : 0;
            if (cancelled > 0) showToast(`Cancelled ${cancelled} queued issue(s)`);
            if (failed > 0) showToast(`Failed to cancel ${failed} queued issue(s)`, true);
            await refreshViewModel();
            return;
        }
        showToast(data.error || `Queued cancel failed (${res.status})`, true);
    } catch (e) {
        console.error('Queued cancel failed:', e);
        showToast('Queued cancel failed: network error', true);
    }
}

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
    let evtSource = null;
    let reconnectAttempts = 0;
    let reconnectTimer = null;
    let healthPollTimer = null;
    const restartBanner = document.getElementById('engineRestartBanner');
    const HEALTH_POLL_MS = 5000;

    function setRestartBanner(message) {
        if (!restartBanner) return;
        restartBanner.textContent = message;
        restartBanner.style.display = '';
    }

    function clearRestartBanner() {
        if (!restartBanner) return;
        restartBanner.style.display = 'none';
        restartBanner.textContent = '';
    }

    async function checkEngineHealth() {
        try {
            const response = await fetch('/api/info', { cache: 'no-store' });
            if (response.ok) {
                if (evtSource === null) {
                    setRestartBanner('Engine reachable. Reconnecting event stream...');
                } else {
                    clearRestartBanner();
                }
                return true;
            }
        } catch (_) {
            // handled below
        }
        setRestartBanner('Engine restarting... waiting for service to recover.');
        return false;
    }

    function closeEventStream() {
        if (evtSource) {
            evtSource.close();
            evtSource = null;
        }
    }

    function scheduleReconnect() {
        if (reconnectTimer !== null) return;
        const capped = Math.min(reconnectAttempts, 6);
        const backoffMs = Math.min(30000, 1000 * (2 ** capped));
        const jitterMs = Math.floor(Math.random() * 300);
        const waitMs = backoffMs + jitterMs;
        const seconds = Math.max(1, Math.round(waitMs / 1000));
        reconnectAttempts += 1;
        setRestartBanner(`Engine restarting... reconnecting in ${seconds}s.`);
        reconnectTimer = window.setTimeout(() => {
            reconnectTimer = null;
            connectEventStream();
        }, waitMs);
    }

    function wireEventListeners(source) {
        source.onopen = function() {
            console.log('[SSE] Connected to event stream (startup_complete=' + startupComplete + ')');
            reconnectAttempts = 0;
            clearRestartBanner();
            loadDependencyProblems();
            loadStaleIssues();
            refreshViewModel({ reloadOnListChange: false });
        };

        const refreshEvents = [
            'session.started',
            'session.completed',
            'orchestrator.paused',
            'orchestrator.resumed',
            'startup_complete',
        ];
        refreshEvents.forEach(eventType => {
            source.addEventListener(eventType, function(e) {
                console.log('[SSE] Received event:', eventType, e.data);
                if (eventType === 'startup_complete') {
                    document.querySelectorAll('.skeleton-card').forEach(el => el.remove());
                }
                setTimeout(() => refreshViewModel({ reloadOnListChange: true }), 200);
            });
        });

        source.addEventListener('tick.completed', function() {
            refreshViewModel({ reloadOnListChange: false });
        });

        source.addEventListener('shutdown_requested', function(e) {
            console.log('[SSE] Shutdown requested:', e.data);
            const badge = document.querySelector('.status-badge');
            if (badge) {
                badge.textContent = 'Stopping...';
                badge.classList.remove('status-running', 'status-starting');
                badge.classList.add('status-paused');
            }
            setTimeout(() => {
                document.body.innerHTML = '<div style="display:flex;justify-content:center;align-items:center;height:100vh;flex-direction:column;gap:16px;color:var(--text-muted);"><div style="font-size:48px;">👋</div><h2 style="color:var(--text);">Orchestrator Stopped</h2><p>You can close this tab or wait for it to restart.</p></div>';
            }, 500);
        });

        source.addEventListener('queue.changed', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Queue changed:', data.added.length, 'added,', data.removed.length, 'removed');
                setTimeout(() => refreshViewModel({ reloadOnListChange: true }), 200);
            } catch (err) {
                console.error('[SSE] Failed to parse queue.changed:', err);
            }
        });

        source.addEventListener('dependency.blocked', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Dependency blocked:', data);
                dependencyProblems[data.issue_number] = data;
                updateDependencyWarning(data.issue_number, data);
            } catch (err) {
                console.error('[SSE] Failed to parse dependency.blocked:', err);
            }
        });

        source.addEventListener('dependency.unblocked', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Dependency unblocked:', data);
                delete dependencyProblems[data.issue_number];
                updateDependencyWarning(data.issue_number, null);
            } catch (err) {
                console.error('[SSE] Failed to parse dependency.unblocked:', err);
            }
        });

        source.addEventListener('stale.in_progress_detected', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Stale in-progress detected:', data);
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

        source.addEventListener('stale.in_progress_cleared', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Stale in-progress cleared:', data);
                delete staleIssues[data.issue_number];
                updateStaleWarning(data.issue_number, null);
            } catch (err) {
                console.error('[SSE] Failed to parse stale.in_progress_cleared:', err);
            }
        });

        source.addEventListener('stale.persistent_detected', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Persistent stale detected:', data);
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

        // E2E lifecycle events — trigger immediate status refresh instead of
        // waiting for the next poll cycle.
        source.addEventListener('e2e.completed', function(event) {
            console.log('[SSE] E2E run completed');
            updateE2EProgress();
        });
        source.addEventListener('e2e.failed', function(event) {
            console.log('[SSE] E2E run failed');
            updateE2EProgress();
        });
        source.addEventListener('e2e.started', function(event) {
            console.log('[SSE] E2E run started');
            updateE2EProgress();
        });
        source.addEventListener('e2e.stopped', function(event) {
            console.log('[SSE] E2E run stopped');
            updateE2EProgress();
        });

        source.onerror = function() {
            console.log('[SSE] Connection error, scheduling reconnect');
            closeEventStream();
            scheduleReconnect();
        };
    }

    function connectEventStream() {
        closeEventStream();
        try {
            evtSource = new EventSource('/api/events');
            wireEventListeners(evtSource);
        } catch (err) {
            console.error('[SSE] Failed to create EventSource:', err);
            closeEventStream();
            scheduleReconnect();
        }
    }

    connectEventStream();
    healthPollTimer = window.setInterval(() => {
        checkEngineHealth();
    }, HEALTH_POLL_MS);
    checkEngineHealth();

    window.addEventListener('beforeunload', () => {
        if (healthPollTimer !== null) {
            window.clearInterval(healthPollTimer);
        }
        if (reconnectTimer !== null) {
            window.clearTimeout(reconnectTimer);
        }
        closeEventStream();
    });
})();

// Helper to add keyboard support to menu items
function addKeyboardSupport(element) {
    if (!element) return;
    element.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            element.click();
        }
    });
}

function clampPagePoint(left, top, width, height, margin = 8) {
    const minLeft = window.scrollX + margin;
    const minTop = window.scrollY + margin;
    const maxLeft = Math.max(minLeft, window.scrollX + window.innerWidth - width - margin);
    const maxTop = Math.max(minTop, window.scrollY + window.innerHeight - height - margin);
    return {
        left: Math.max(minLeft, Math.min(left, maxLeft)),
        top: Math.max(minTop, Math.min(top, maxTop)),
    };
}

function clampClientPoint(left, top, width, height, margin = 8) {
    const minLeft = margin;
    const minTop = margin;
    const maxLeft = Math.max(minLeft, window.innerWidth - width - margin);
    const maxTop = Math.max(minTop, window.innerHeight - height - margin);
    return {
        left: Math.max(minLeft, Math.min(left, maxLeft)),
        top: Math.max(minTop, Math.min(top, maxTop)),
    };
}

function normalizeToClientPoint(point) {
    if (!point) return null;
    if (Number.isFinite(point.clientX) && Number.isFinite(point.clientY)) {
        return { x: Number(point.clientX), y: Number(point.clientY) };
    }
    if (Number.isFinite(point.pageX) && Number.isFinite(point.pageY)) {
        return {
            x: Number(point.pageX) - window.scrollX,
            y: Number(point.pageY) - window.scrollY,
        };
    }
    if (Number.isFinite(point.x) && Number.isFinite(point.y)) {
        return {
            x: Number(point.x) - window.scrollX,
            y: Number(point.y) - window.scrollY,
        };
    }
    return null;
}

// Context menu
const contextMenu = document.getElementById('contextMenu');
const menuFocus = document.getElementById('menuFocus');
const menuRevealWorktree = document.getElementById('menuRevealWorktree');
const menuLog = document.getElementById('menuLog');
const menuAgentLog = document.getElementById('menuAgentLog');
const menuPrompt = document.getElementById('menuPrompt');
const menuKill = document.getElementById('menuKill');
const menuPR = document.getElementById('menuPR');
const menuUnblock = document.getElementById('menuUnblock');
const menuResetRetry = document.getElementById('menuResetRetry');
const menuResetRetryScratch = document.getElementById('menuResetRetryScratch');
const menuRetry = document.getElementById('menuRetry');
const menuHistoryDivider = document.getElementById('menuHistoryDivider');
const menuDepsDivider = document.getElementById('menuDepsDivider');
const menuDepsLabel = document.getElementById('menuDepsLabel');
const menuDepsContainer = document.getElementById('menuDepsContainer');
let currentRow = null;
let lastContextMenuPoint = null;
const contextMenuEnabled = Boolean(contextMenu);

// Add keyboard support to all context menu items
if (contextMenuEnabled) {
    [menuFocus, menuRevealWorktree, menuLog, menuAgentLog, menuPrompt, menuKill, menuPR, menuUnblock, menuResetRetry, menuResetRetryScratch, menuRetry]
        .filter(Boolean)
        .forEach(addKeyboardSupport);
}

function showContextMenu(e, row) {
    if (!contextMenuEnabled) return;
    e.preventDefault();
    currentRow = row;

    const issueNumber = row.dataset.issue;
    const action = row.dataset.action;
    const prUrl = row.dataset.prUrl;
    const status = row.dataset.status;
    const columnId = String(row.dataset.columnId || '').toLowerCase();
    const hasDeps = row.dataset.hasDependencies === 'true';
    const isCompactCardMenu = row.dataset.compactMenu === 'true';
    const statusLower = String(status || '').toLowerCase();
    const normalizedStatus = statusLower.replace(/_/g, '-');
    const effectiveHistoryStatus = (isCompactCardMenu && columnId) ? columnId : normalizedStatus;
    const isBlockedHistory = effectiveHistoryStatus === 'blocked' || effectiveHistoryStatus === 'needs-human';
    lastContextMenuPoint = {
        pageX: e.pageX,
        pageY: e.pageY,
        clientX: e.clientX,
        clientY: e.clientY,
    };

    const setMenuVisible = (el, visible) => {
        if (!el) return;
        el.style.display = visible ? '' : 'none';
        el.classList.toggle('disabled', !visible);
    };

    // Show only applicable actions; hide the rest.
    const canFocusSession = action === 'focus' && clientCapabilities.focus_session;
    const canRevealWorktree = action === 'focus' && clientCapabilities.reveal_worktree;
    setMenuVisible(menuFocus, canFocusSession);
    setMenuVisible(menuRevealWorktree, canRevealWorktree);

    setMenuVisible(menuPR, Boolean(prUrl || row.dataset.issueUrl));
    if (menuPR) {
        menuPR.textContent = prUrl ? 'Open PR ↗' : 'Open Issue ↗';
    }

    const agentType = row.dataset.agent;
    setMenuVisible(menuPrompt, Boolean(agentType));

    const hasTerminal = row.dataset.hasTerminal === 'true';
    setMenuVisible(menuKill, hasTerminal);
    // Compact-card menus avoid ambiguous log/session entries.
    setMenuVisible(menuLog, !isCompactCardMenu && !isBlockedHistory);
    setMenuVisible(menuAgentLog, !isCompactCardMenu && !isBlockedHistory);

    // Show dependencies section if this issue has dependencies
    if (menuDepsContainer) menuDepsContainer.innerHTML = '';  // Clear previous
    if (hasDeps && menuDepsContainer && menuDepsDivider && menuDepsLabel) {
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
    } else if (menuDepsDivider && menuDepsLabel) {
        menuDepsDivider.style.display = 'none';
        menuDepsLabel.style.display = 'none';
    }

    const hasPrimaryActionsAboveHistory = [
        menuFocus,
        menuRevealWorktree,
        menuLog,
        menuAgentLog,
        menuPrompt,
        menuPR,
        menuKill,
    ].some((el) => el && el.style.display !== 'none');

    // History actions by status:
    // blocked/awaiting-merge => Retry + Reset & Retry + Reset & Retry From Scratch
    // blocked also gets Unblock; others => Retry only
    const resetRetryStatuses = new Set(['blocked', 'awaiting-merge']);
    const otherRetryStatuses = new Set(['failed', 'completed', 'timed-out']);
    if (menuHistoryDivider && menuRetry && menuUnblock && menuResetRetry && menuResetRetryScratch) {
        if (resetRetryStatuses.has(effectiveHistoryStatus) || isBlockedHistory) {
            menuHistoryDivider.style.display = hasPrimaryActionsAboveHistory ? 'block' : 'none';
            menuUnblock.style.display = isBlockedHistory ? '' : 'none';
            menuResetRetry.style.display = '';
            menuResetRetryScratch.style.display = '';
            menuRetry.style.display = '';
        } else if (otherRetryStatuses.has(effectiveHistoryStatus)) {
            menuHistoryDivider.style.display = hasPrimaryActionsAboveHistory ? 'block' : 'none';
            menuUnblock.style.display = 'none';
            menuResetRetry.style.display = 'none';
            menuResetRetryScratch.style.display = 'none';
            menuRetry.style.display = '';
        } else {
            menuHistoryDivider.style.display = 'none';
            menuUnblock.style.display = 'none';
            menuResetRetry.style.display = 'none';
            menuResetRetryScratch.style.display = 'none';
            menuRetry.style.display = 'none';
        }
    }

    // Position menu (clamped to viewport so right-edge triggers still show)
    contextMenu.classList.add('visible');
    const menuRect = contextMenu.getBoundingClientRect();
    const clamped = clampPagePoint(e.pageX, e.pageY, menuRect.width, menuRect.height, 8);
    contextMenu.style.left = `${clamped.left}px`;
    contextMenu.style.top = `${clamped.top}px`;
}

function openRowActionsMenu(event, button) {
    event.preventDefault();
    event.stopPropagation();
    if (typeof event.stopImmediatePropagation === 'function') {
        event.stopImmediatePropagation();
    }
    const row = button?.closest('.issue-row');
    if (!row) return;
    const rect = button.getBoundingClientRect();
    const syntheticEvent = {
        preventDefault: () => {},
        pageX: rect.right + window.scrollX,
        pageY: rect.bottom + window.scrollY,
    };
    showContextMenu(syntheticEvent, row);
}

function openCompactCardActionsMenu(event, button) {
    const issueNumber = Number(button?.dataset?.issue || 0);
    const title = String(button?.dataset?.title || '');
    const issueUrl = String(button?.dataset?.issueUrl || '');
    const prUrl = String(button?.dataset?.prUrl || '');
    const status = String(button?.dataset?.status || '');
    const action = String(button?.dataset?.rowAction || '');
    const agentType = String(button?.dataset?.agent || '');
    const hasTerminal = button?.dataset?.hasTerminal === 'true';
    const columnId = String(button?.closest('.kanban-column')?.dataset?.column || '').toLowerCase();
    if (!contextMenuEnabled) {
        showToast('Actions menu unavailable: context menu not initialized', 'error');
        return;
    }
    event.preventDefault();
    event.stopPropagation();
    if (typeof event.stopImmediatePropagation === 'function') {
        event.stopImmediatePropagation();
    }
    if (!button) {
        showToast('Actions menu unavailable: missing trigger button', 'error');
        return;
    }
    const rect = button.getBoundingClientRect();
    const syntheticEvent = {
        preventDefault: () => {},
        pageX: rect.right + window.scrollX,
        pageY: rect.bottom + window.scrollY,
    };
    const fauxRow = {
        dataset: {
            issue: String(issueNumber),
            title: String(title || ''),
            issueUrl: String(issueUrl || ''),
            prUrl: String(prUrl || ''),
            status: String(status || ''),
            columnId: String(columnId || ''),
            action: String(action || ''),
            agent: String(agentType || ''),
            hasTerminal: hasTerminal ? 'true' : 'false',
            hasDependencies: 'false',
            dependencies: '[]',
            compactMenu: 'true',
        }
    };
    showContextMenu(syntheticEvent, fauxRow);
}

// Hide context menu on click elsewhere
if (contextMenuEnabled) {
    document.addEventListener('click', (event) => {
        const target = event.target;
        if (target instanceof Element) {
            if (target.closest('.context-menu')) return;
            if (target.closest('.issue-row-menu-btn')) return;
            if (target.closest('.card-menu-btn')) return;
        }
        contextMenu?.classList.remove('visible');
    });

    // Menu actions - must stopPropagation to prevent document click handler from interfering
    menuFocus?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow && !menuFocus.classList.contains('disabled')) {
            await fetch(`/api/focus/${currentRow.dataset.issue}`, { method: 'POST' });
        }
    });

    menuRevealWorktree?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow && !menuRevealWorktree.classList.contains('disabled')) {
            const req = uiActionContract.buildRevealWorktreeRequest(currentRow.dataset.issue);
            const res = await fetch(req.endpoint, { method: req.method });
            const data = await res.json();
            handleHostActionResponse(data, 'worktree');
        }
    });

    menuLog?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = currentRow.dataset.issue;
            await openFilteredOrchestratorLog(Number(issueNumber));
        }
    });

    menuAgentLog?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = currentRow.dataset.issue;
            await openSessionManifest(issueNumber);
        }
    });

    menuPrompt?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow && !menuPrompt.classList.contains('disabled')) {
            const agentType = currentRow.dataset.agent;
            const res = await fetch(`/api/prompt/${encodeURIComponent(agentType)}`, { method: 'POST' });
            const data = await res.json();
            console.log('Prompt response:', data);
        }
    });

    menuKill?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = currentRow.dataset.issue;
            const title = currentRow.dataset.title;
            if (confirm(`Terminate session #${issueNumber}: ${title}?\n\nThis will stop the active agent and place the issue on hold.\nIt will not run again until you explicitly retry/unblock it.`)) {
                const res = await fetch(`/api/kill/${issueNumber}`, { method: 'POST' });
                const data = await res.json();
                if (data.status === 'terminated') {
                    location.reload();
                } else {
                    alert(data.error || 'Failed to terminate session');
                }
            }
        }
    });

    menuPR?.addEventListener('click', (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const targetUrl = currentRow.dataset.prUrl || currentRow.dataset.issueUrl;
            if (targetUrl) {
                window.open(targetUrl, '_blank');
            }
        }
    });

    menuRetry?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = Number(currentRow.dataset.issue);
            const status = String(currentRow.dataset.status || '').toLowerCase();
            if (Number.isNaN(issueNumber)) return;
            if (confirm(`Retry issue #${issueNumber}? It will be picked up on the next cycle.`)) {
                const req = uiActionContract.buildIssueRetryRequest(issueNumber);
                const res = await fetch(req.endpoint, {
                    method: req.method,
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(req.body),
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok && (data.retrying || data.success || (data.retried && data.retried.length > 0))) {
                    if (status) applyOptimisticRequeue([issueNumber], [status]);
                    await refreshViewModel();
                } else {
                    alert(data.error || 'Failed to retry issue');
                }
            }
        }
    });

    menuUnblock?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = Number(currentRow.dataset.issue);
            if (!Number.isNaN(issueNumber)) {
                await unblockSingle(issueNumber, lastContextMenuPoint);
            }
        }
    });

    menuResetRetry?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = Number(currentRow.dataset.issue);
            if (!Number.isNaN(issueNumber)) {
                await resetRetrySingle(issueNumber, lastContextMenuPoint);
            }
        }
    });

    menuResetRetryScratch?.addEventListener('click', async (e) => {
        e.stopPropagation();
        contextMenu.classList.remove('visible');
        if (currentRow) {
            const issueNumber = Number(currentRow.dataset.issue);
            if (!Number.isNaN(issueNumber)) {
                await resetRetrySingleFromScratch(issueNumber, lastContextMenuPoint);
            }
        }
    });

}

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
        modalEl.classList.remove('diagnostics-modal', 'log-viewer-modal', 'live-log-modal');
        if (logPoller) {
            clearInterval(logPoller);
            logPoller = null;
        }
        // Stop live log poller if running
        stopLiveLogPoller();
        window.removeEventListener('resize', fitSessionReplayTerminal);
        destroySessionReplay();
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
                phase.validation_passed ? 'var(--ok)' : 'var(--danger)';
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
const issueDetailDrawer = document.getElementById('issueDetailDrawer');
let issueDetailData = null;
let lastIssueDetailTrigger = null;
let journeyFilter = 'latest-run'; // 'latest-run' or 'all'
let timelineView = 'user'; // 'user', 'ops', or 'debug'

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
        renderTimeline(content, data.events || [], data.phase_toc || [], data.cycles || []);
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

function getIssueDetailFocusableElements() {
    if (!issueDetailDrawer) return [];
    return Array.from(
        issueDetailDrawer.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
    ).filter((el) => !el.hasAttribute('disabled') && el.offsetParent !== null);
}

async function openIssueDetail(issueNumber, triggerEl = null) {
    if (!issueDetailDrawer) return;
    lastIssueDetailTrigger = triggerEl || document.activeElement;
    issueDetailDrawer.classList.add('visible');
    issueDetailDrawer.setAttribute('aria-hidden', 'false');
    document.getElementById('issueDetailTitle').textContent = `Issue #${issueNumber}`;
    document.getElementById('issueDetailStatus').textContent = 'Loading issue detail...';
    document.getElementById('issueDetailStatus').className = 'issue-detail-status';
    document.getElementById('issueDetailJourney').innerHTML = '';
    const prevCycles = document.getElementById('issueDetailPrevCycles');
    if (prevCycles) prevCycles.style.display = 'none';
    const rawEvents = document.getElementById('issueDetailRawEvents');
    if (rawEvents) rawEvents.removeAttribute('open');
    const unblockBtn = document.getElementById('issueDetailUnblockBtn');
    if (unblockBtn) {
        unblockBtn.style.display = 'none';
        unblockBtn.disabled = true;
    }
    const retryPublishBtn = document.getElementById('issueDetailRetryPublishBtn');
    if (retryPublishBtn) {
        retryPublishBtn.style.display = 'none';
        retryPublishBtn.disabled = true;
    }
    resetIssueDetailValidation();
    const closeBtn = document.getElementById('issueDetailCloseBtn');
    if (closeBtn) closeBtn.focus();

    try {
        const res = await fetch(`/api/issue-detail/${issueNumber}?view=${timelineView}`);
        if (!res.ok) {
            document.getElementById('issueDetailStatus').textContent = 'Issue detail unavailable.';
            return;
        }
        issueDetailData = await res.json();
        renderIssueDetail();
    } catch (err) {
        console.error('Failed to load issue detail:', err);
        document.getElementById('issueDetailStatus').textContent = 'Failed to load issue detail.';
    }
}

function closeIssueDetail() {
    if (!issueDetailDrawer) return;
    issueDetailDrawer.classList.remove('visible');
    issueDetailDrawer.setAttribute('aria-hidden', 'true');
    if (lastIssueDetailTrigger && typeof lastIssueDetailTrigger.focus === 'function') {
        lastIssueDetailTrigger.focus();
    }
}

async function unblockFromDrawer() {
    if (!issueDetailData) return;
    const n = issueDetailData.issue_number;
    const btn = document.getElementById('issueDetailUnblockBtn');
    const confirmMsg = `Requeue issue #${n}?\n\nThis will REMOVE retry-gating labels (including blocking labels and pr-pending).\n\nIt will not delete the local worktree or remote branch.`;
    if (!await showConfirm(confirmMsg, btn)) return;
    if (btn) btn.disabled = true;
    try {
        const req = uiActionContract.buildUnblockRequest([n]);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        if (resp.ok) {
            applyOptimisticRequeue([n], ['blocked']);
            showToast(`Unblocked #${n} → Queued`);
            closeIssueDetail();
            await refreshViewModel();
        } else {
            const data = await resp.json().catch(() => ({}));
            showToast(data.error || `Unblock failed (${resp.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Unblock from drawer failed:', e);
        showToast('Unblock failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

// ---------------------------------------------------------------------------
// Journey cycles — collapsible lifecycle groups
// ---------------------------------------------------------------------------

function filterRuns(runs, filter) {
    if (!runs.length || filter === 'all') return runs;
    return [runs[runs.length - 1]];
}

function renderJourneyTimeline(container, data) {
    _renderJourneyRuns(container, data.runs || []);
}

function _renderJourneyRuns(container, allRuns) {
    const runs = filterRuns(allRuns, journeyFilter);
    const isLatestRun = journeyFilter === 'latest-run';
    const isAll = journeyFilter === 'all';
    const issueNum = issueDetailData ? issueDetailData.issue_number : null;
    const timelineDiagnostic = issueDetailData?.summary?.timeline_diagnostic || null;

    let html = `<div class="journey-filter">
        <button class="journey-filter-btn ${isLatestRun ? 'active' : ''}" onclick="setJourneyFilter('latest-run')" title="Show the current run (all cycles in the latest lifecycle)">Latest run</button>
        <button class="journey-filter-btn ${isAll ? 'active' : ''}" onclick="setJourneyFilter('all')">All</button>
        <button class="journey-filter-btn journey-copy-btn" onclick="copyJourneyTimeline()" title="Copy timeline as text">Copy</button>
        <span class="journey-filter-separator"></span>
        <button class="journey-filter-btn ${timelineView === 'user' ? 'active' : ''}" onclick="setTimelineView('user')" title="Show key events: coding, review, outcome">Story</button>
        <button class="journey-filter-btn ${timelineView === 'ops' ? 'active' : ''}" onclick="setTimelineView('ops')" title="Show operational events: validation, retries, exchanges">Ops</button>
        <button class="journey-filter-btn ${timelineView === 'debug' ? 'active' : ''}" onclick="setTimelineView('debug')" title="Show all internal events">Debug</button>
    </div>`;

    if (runs.length === 0) {
        if (timelineDiagnostic && timelineDiagnostic.state === 'expected_history_missing') {
            const signals = Array.isArray(timelineDiagnostic.signals) ? timelineDiagnostic.signals.join(', ') : 'unknown';
            const store = timelineDiagnostic.expected_timeline_store || 'unknown';
            html += `<div class="timeline-empty">Timeline data missing (signals: ${escapeHtml(signals)}).</div>`;
            html += `<div class="timeline-empty">Expected timeline store: <code>${escapeHtml(store)}</code></div>`;
        } else {
            html += '<div class="timeline-empty">No activity recorded.</div>';
        }
        container.innerHTML = html;
        return;
    }

    for (let runIndex = 0; runIndex < runs.length; runIndex++) {
        const run = runs[runIndex];
        const runExpanded = journeyFilter === 'latest-run' ? true : Boolean(run.expanded);
        const runToggle = runExpanded ? '\u25be' : '\u25b8';
        const runId = `journey-run-${runIndex}`;
        const runBodyClass = runExpanded ? '' : ' collapsed';
        html += `<div class="journey-run" id="${runId}">
            <div class="journey-cycle-header" onclick="toggleJourneyCycle('${runId}')">
                <span class="journey-cycle-toggle">${runToggle}</span>
                <span class="journey-cycle-label">Run ${run.run_number || (runIndex + 1)}</span>
                <span class="journey-cycle-outcome ${_cycleOutcomeClass(run.outcome || '')}">\u2014 ${escapeHtml(run.outcome || 'In progress')}</span>
                <span class="journey-cycle-time">${escapeHtml(formatJourneyHeaderTimestamp(run.timestamp || '', run.time_label || ''))}</span>
            </div>
            <div class="journey-cycle-body${runBodyClass}" id="${runId}-body">`;

        const cycles = run.cycles || [];
        for (let cycleIndex = 0; cycleIndex < cycles.length; cycleIndex++) {
            const c = cycles[cycleIndex];
            const cycleId = `journey-cycle-${runIndex}-${cycleIndex}`;
            const cycleExpanded = journeyFilter === 'latest-run' ? true : Boolean(c.expanded);
            const toggle = cycleExpanded ? '\u25be' : '\u25b8';
            const bodyClass = cycleExpanded ? '' : ' collapsed';
            const displayCycleNumber = c.cycle_in_run || c.cycle || (cycleIndex + 1);
            const agentPill = c.agent ? `<span class="journey-cycle-agent">(${escapeHtml(c.agent)})</span>` : '';
            const retryInfo = c.retry_count > 0 ? `<span class="journey-cycle-retries">${c.retry_count} ${c.retry_count === 1 ? 'retry' : 'retries'}</span>` : '';
            const outcomeClass = _cycleOutcomeClass(c.outcome || '');
            const artifacts = c.artifacts || {};
            const hasArtifacts = artifacts.log_url || artifacts.pr_url || artifacts.has_review_feedback;

            html += `<div class="journey-cycle" id="${cycleId}">
            <div class="journey-cycle-header" onclick="toggleJourneyCycle('${cycleId}')">
                <span class="journey-cycle-toggle">${toggle}</span>
                <span class="journey-cycle-label">Cycle ${displayCycleNumber}</span>
                ${agentPill}
                ${retryInfo}
                <span class="journey-cycle-outcome ${outcomeClass}">\u2014 ${escapeHtml(c.outcome || 'In progress')}</span>
                <span class="journey-cycle-time">${escapeHtml(formatJourneyHeaderTimestamp(c.timestamp || '', c.time_label || ''))}</span>
                ${hasArtifacts ? `<span class="journey-cycle-artifacts-btn" onclick="event.stopPropagation(); toggleArtifactPopover(${runIndex}, ${cycleIndex}, ${issueNum})" title="Cycle artifacts">\ud83d\udcce</span>` : ''}
            </div>
            <div class="journey-cycle-body${bodyClass}" id="${cycleId}-body">`;

            const phaseGroups = Array.isArray(c.phase_groups) && c.phase_groups.length > 0
                ? c.phase_groups
                : [{ key: 'events', label: '', steps: c.steps || [] }];
            for (const group of phaseGroups) {
                html += `<div class="journey-phase-group">`;
                if (group.label) {
                    html += `<div class="journey-phase-header">${escapeHtml(group.label)}</div>`;
                }
                const steps = group.steps || [];
                for (const s of steps) {
                    const statusClass = s.status ? 'status-' + escapeHtml(s.status) : '';
                    const detail = s.detail ? `<div class="journey-detail">${escapeHtml(s.detail)}</div>` : '';
                    const actions = renderTimelineEventActions(s.actions || []);
                    html += `<div class="journey-step ${statusClass}">
                    <span class="journey-time">${escapeHtml(formatJourneyStepTimestamp(s.timestamp || '', s.time_label || ''))}</span>
                    <div class="journey-main">
                        <div class="journey-summary-row">
                            <span class="journey-narrative">${escapeHtml(s.narrative || s.event || '')}</span>
                            ${actions}
                        </div>
                        ${detail}
                    </div>
                </div>`;
                }
                html += `</div>`;
            }

            html += `</div></div>`;
        }
        html += `</div></div>`;
    }

    container.innerHTML = html;

    // Wire up delegated click handler for action buttons inside journey steps
    if (!container.dataset.journeyActionsBound) {
        container.addEventListener('click', (event) => {
            const actionTarget = event.target.closest('.timeline-action-btn, .timeline-more-item');
            if (actionTarget && actionTarget.dataset.action) {
                try {
                    const action = JSON.parse(actionTarget.dataset.action);
                    closeTimelineEventMenus();
                    runTimelineEventAction(action);
                } catch (err) {
                    console.error('Failed to parse run action:', err);
                    showToast('Unable to execute action', 'error');
                }
                return;
            }
            const menuTrigger = event.target.closest('.timeline-event-menu-trigger');
            if (menuTrigger) {
                const ownerMenu = menuTrigger.closest('.timeline-event-menu');
                closeTimelineEventMenus(ownerMenu);
                return;
            }
            if (!event.target.closest('.timeline-event-menu')) {
                closeTimelineEventMenus();
            }
        });
        container.dataset.journeyActionsBound = 'true';
    }
}

function _cycleOutcomeClass(outcome) {
    const lower = outcome.toLowerCase();
    if (lower.includes('failed') || lower.includes('blocked') || lower.includes('timed out')) return 'outcome-failed';
    if (lower.includes('approved') || lower.includes('merged') || lower.includes('completed')) return 'outcome-success';
    if (lower.includes('changes requested') || lower.includes('escalated')) return 'outcome-warning';
    return '';
}

function toggleJourneyCycle(cycleId) {
    closeTimelineEventMenus();
    const cycleNode = document.getElementById(cycleId);
    const body = document.getElementById(cycleId + '-body');
    if (!body || !cycleNode) return;
    const header = cycleNode.querySelector(':scope > .journey-cycle-header .journey-cycle-toggle');
    const isCollapsed = body.classList.contains('collapsed');
    body.classList.toggle('collapsed');
    if (header) header.textContent = isCollapsed ? '\u25be' : '\u25b8';
}

function toggleArtifactPopover(runIndex, cycleIndex, issueNumber) {
    closeTimelineEventMenus();
    // Close any existing popover
    const existing = document.querySelector('.journey-artifact-popover');
    if (existing) {
        const existingParent = existing.closest('.journey-cycle');
        existing.remove();
        if (existingParent && existingParent.id === `journey-cycle-${runIndex}-${cycleIndex}`) return; // Toggle off
    }

    const cycleId = `journey-cycle-${runIndex}-${cycleIndex}`;
    const cycleEl = document.getElementById(cycleId);
    if (!cycleEl || !issueDetailData) return;

    const allRuns = filterRuns(issueDetailData.runs || [], journeyFilter);
    const runData = allRuns[runIndex];
    const cycleData = runData?.cycles?.[cycleIndex];
    if (!cycleData) return;

    const artifacts = cycleData.artifacts || {};
    let items = '';

    const cycleRunDir = cycleData.run_dir || null;

    if (artifacts.log_url) {
        items += `<a href="${escapeHtml(artifacts.log_url)}" target="_blank" rel="noopener noreferrer">View log transcript</a>`;
    } else if (issueNumber) {
        const runDirArg = cycleRunDir ? `${JSON.stringify(String(cycleRunDir))}, ` : 'null, ';
        items += `<a href="#" onclick="event.preventDefault(); closeArtifactPopover(); openAgentLogAction(${issueNumber}, ${runDirArg}'Cycle Session Recording')">View session transcript</a>`;
    }

    if (artifacts.pr_url) {
        const prLabel = artifacts.pr_number ? `PR #${artifacts.pr_number}` : 'Pull Request';
        items += `<a href="${escapeHtml(artifacts.pr_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(prLabel)}</a>`;
    }

    if (artifacts.has_review_feedback && issueNumber) {
        items += `<a href="#" onclick="event.preventDefault(); closeArtifactPopover(); openReviewFeedback(${issueNumber})">Review feedback</a>`;
    }

    if (issueNumber) {
        const diagnoseArg = cycleRunDir ? `, ${JSON.stringify(String(cycleRunDir))}` : '';
        items += `<a href="#" onclick="event.preventDefault(); closeArtifactPopover(); openDiagnoseFromCycle(${issueNumber}${diagnoseArg})">Diagnose</a>`;
    }

    if (!items) return;

    const popover = document.createElement('div');
    popover.className = 'journey-artifact-popover';
    popover.innerHTML = items;
    cycleEl.querySelector('.journey-cycle-header').appendChild(popover);

    // Close on click outside
    setTimeout(() => {
        document.addEventListener('click', _closePopoverOnClickOutside, { once: true });
    }, 0);
    document.addEventListener('keydown', _closePopoverOnEscape);
}

function _closePopoverOnClickOutside(e) {
    const popover = document.querySelector('.journey-artifact-popover');
    if (popover && !popover.contains(e.target)) {
        popover.remove();
    }
    document.removeEventListener('keydown', _closePopoverOnEscape);
}

function _closePopoverOnEscape(e) {
    if (e.key === 'Escape') {
        closeArtifactPopover();
    }
}

function closeArtifactPopover() {
    const popover = document.querySelector('.journey-artifact-popover');
    if (popover) popover.remove();
    document.removeEventListener('keydown', _closePopoverOnEscape);
}

function openDiagnoseFromCycle(issueNumber, runDir = null) {
    // Route to run-scoped diagnostics when available.
    if (runDir) {
        openSessionManifest(issueNumber, runDir);
        return;
    }
    openTimelineModal(issueNumber);
}

function setJourneyFilter(filter) {
    journeyFilter = filter;
    if (issueDetailData) {
        const journeyEl = document.getElementById('issueDetailJourney');
        if (journeyEl) renderJourneyTimeline(journeyEl, issueDetailData);
    }
}

async function setTimelineView(view) {
    timelineView = view;
    if (issueDetailData) {
        const issueNumber = issueDetailData.issue_number;
        try {
            const res = await fetch(`/api/issue-detail/${issueNumber}?view=${view}`);
            if (res.ok) {
                issueDetailData = await res.json();
                renderIssueDetail();
            }
        } catch (err) {
            console.error('Failed to switch timeline view:', err);
        }
    }
}

function copyJourneyTimeline() {
    if (!issueDetailData) return;

    const runs = filterRuns(issueDetailData.runs || [], journeyFilter);
    if (runs.length === 0) {
        showToast('No timeline to copy', true);
        return;
    }
    const issueNum = issueDetailData.issue_number;
    const title = issueDetailData.title || '';
    let text = `Issue #${issueNum}: ${title}\n`;
    for (const run of runs) {
        text += `\nRun ${run.run_number || '?'} \u2014 ${run.outcome || 'In progress'}  ${run.time_label || ''}\n`;
        for (const c of (run.cycles || [])) {
            const agent = c.agent ? ` (${c.agent})` : '';
            const cycleNum = c.cycle_in_run || c.cycle || '?';
            text += `  Cycle ${cycleNum}${agent} \u2014 ${c.outcome || 'In progress'}  ${c.time_label || ''}\n`;
            for (const s of (c.steps || [])) {
                const time = s.time_label || '';
                const narrative = s.narrative || s.event || '';
                text += `    ${time}  ${narrative}\n`;
                if (s.detail) text += `      ${s.detail}\n`;
            }
        }
    }
    const timelineText = text.trim();
    if (!timelineText) {
        showToast('No timeline to copy', true);
        return;
    }

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(timelineText).then(
            () => showToast('Timeline copied'),
            () => fallbackCopyJourneyTimeline(timelineText)
        );
        return;
    }

    fallbackCopyJourneyTimeline(timelineText);
}

function fallbackCopyJourneyTimeline(timelineText) {
    const textarea = document.createElement('textarea');
    textarea.value = timelineText;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'absolute';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.select();
    textarea.setSelectionRange(0, 99999);

    try {
        const ok = document.execCommand('copy');
        showToast(ok ? 'Timeline copied' : 'Failed to copy', !ok);
    } catch (_err) {
        showToast('Failed to copy', true);
    } finally {
        document.body.removeChild(textarea);
    }
}

function formatJourneyDay(dayStr) {
    try {
        const d = new Date(dayStr + 'T00:00:00');
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        const diff = Math.floor((today - d) / 86400000);
        if (diff === 0) return 'Today';
        if (diff === 1) return 'Yesterday';
        return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
    } catch (e) {
        return dayStr;
    }
}

function _matchesReviewFeedbackContext(evt, context) {
    if (!context || !context.feedback_event) return true;
    if (String(evt.event || '') !== String(context.feedback_event || '')) return false;
    if (context.event_timestamp && String(evt.timestamp || '') !== String(context.event_timestamp || '')) return false;
    if (context.round_index != null && Number(evt.round_index || 0) !== Number(context.round_index)) return false;
    return true;
}

async function openReviewFeedback(issueNumber, context = null) {
    document.getElementById('modalTitle').textContent = `Review Feedback #${issueNumber}`;
    document.getElementById('modalBody').innerHTML = '<div class="timeline-loading">Loading review feedback...</div>';
    document.getElementById('modalOverlay').classList.add('visible');

    try {
        const res = await fetch(`/api/failure-diagnosis/${issueNumber}`);
        const data = await res.json();
        const feedback = data.review_feedback || [];
        if (feedback.length > 0) {
            let html = '';
            for (const fb of feedback) {
                html += `<div style="margin-bottom:12px;">
                    <div style="font-weight:600;font-size:12px;margin-bottom:4px;">Cycle ${escapeHtml(String(fb.cycle || '?'))}</div>
                    <pre style="font-size:11px;white-space:pre-wrap;background:var(--bg);padding:10px;border-radius:4px;margin:0;max-height:300px;overflow:auto;">${escapeHtml(fb.content || '')}</pre>
                </div>`;
            }
            document.getElementById('modalBody').innerHTML = html;
            return;
        }

        // No local feedback — use timeline events for the requested issue.
        let detailForIssue = null;
        if (issueDetailData && issueDetailData.issue_number === issueNumber) {
            detailForIssue = issueDetailData;
        } else {
            // Always fetch with ops view — review.comment_added is ops-only,
            // so Story view would omit review comment evidence.
            const detailRes = await fetch(`/api/issue-detail/${issueNumber}?view=ops`);
            if (detailRes.ok) {
                detailForIssue = await detailRes.json();
            }
        }

        // Fall back to currently loaded drawer detail if it matches and fetch did not resolve.
        if (!detailForIssue && issueDetailData && issueDetailData.issue_number === issueNumber) {
            detailForIssue = issueDetailData;
        }

        // Try to show the reviewer's completion summary from events
        let html = '';
        if (detailForIssue) {
            const events = detailForIssue.events || [];
            const reviewEvents = events.filter(e =>
                (
                    e.event === 'review.changes_requested' ||
                    e.event === 'review.approved' ||
                    e.event === 'review.comment_added'
                ) &&
                _matchesReviewFeedbackContext(e, context)
            );
            // Review exchange round events carry per-round reviewer feedback
            const exchangeRoundEvents = events.filter(e =>
                e.event === 'review_exchange.round_completed' &&
                e.reviewer_response_text &&
                _matchesReviewFeedbackContext(e, context)
            );
            if (exchangeRoundEvents.length > 0) {
                html += '<div style="margin-bottom:8px;font-size:12px;color:var(--text-muted);">Review exchange rounds:</div>';
                for (const evt of exchangeRoundEvents) {
                    const roundNum = evt.round_index || '?';
                    const respType = evt.reviewer_response_type || 'unknown';
                    const label = respType === 'ok' ? 'Approved' : respType === 'changes_requested' ? 'Changes Requested' : respType;
                    const time = evt.timestamp ? new Date(evt.timestamp).toLocaleString() : '';
                    html += `<div style="margin-bottom:10px;padding:8px;background:var(--bg);border-radius:4px;">
                        <div style="font-weight:600;font-size:12px;">Round ${escapeHtml(String(roundNum))}: ${escapeHtml(label)} ${time ? `<span style="font-weight:400;color:var(--text-muted);">${escapeHtml(time)}</span>` : ''}</div>
                        <pre style="font-size:11px;white-space:pre-wrap;background:var(--surface);padding:8px;border-radius:4px;margin:4px 0 0;max-height:200px;overflow:auto;">${escapeHtml(evt.reviewer_response_text)}</pre>
                    </div>`;
                }
            }
            if (reviewEvents.length > 0) {
                if (exchangeRoundEvents.length > 0) {
                    html += '<div style="margin:12px 0 8px;font-size:12px;color:var(--text-muted);">PR review events:</div>';
                } else {
                    html += '<div style="margin-bottom:8px;font-size:12px;color:var(--text-muted);">From timeline events:</div>';
                }
                for (const evt of reviewEvents) {
                    const label =
                        evt.event === 'review.approved' ? 'Approved'
                            : evt.event === 'review.changes_requested' ? 'Changes Requested'
                                : 'Review Comment Posted';
                    const time = evt.timestamp ? new Date(evt.timestamp).toLocaleString() : '';
                    let commentLink = '';
                    if (evt.event === 'review.comment_added') {
                        const reviewComment = (evt.artifacts || []).find(a => a.type === 'review_comment' && a.value);
                        if (reviewComment) {
                            commentLink = `<div style="font-size:12px;margin-top:6px;"><a href="${escapeHtml(reviewComment.value)}" target="_blank" rel="noopener noreferrer">Open review comment on GitHub ↗</a></div>`;
                        }
                    }
                    html += `<div style="margin-bottom:10px;padding:8px;background:var(--bg);border-radius:4px;">
                        <div style="font-weight:600;font-size:12px;">${escapeHtml(label)} ${time ? `<span style="font-weight:400;color:var(--text-muted);">${escapeHtml(time)}</span>` : ''}</div>
                        ${evt.summary ? `<div style="font-size:12px;margin-top:4px;">${escapeHtml(evt.summary)}</div>` : ''}
                        ${evt.detail ? `<div style="font-size:12px;margin-top:4px;color:var(--text-muted);">${escapeHtml(evt.detail)}</div>` : ''}
                        ${commentLink}
                    </div>`;
                }
            }

            // Link to PR if available
            const prEvent = events.find(e => (e.source_event || e.event) === 'issue.pr_created');
            const prArtifact = prEvent && (prEvent.artifacts || []).find(a => a.type === 'pull_request');
            if (prArtifact && prArtifact.value) {
                html += `<div style="margin-top:8px;"><a href="${escapeHtml(prArtifact.value)}" target="_blank" rel="noopener noreferrer" class="issue-action-btn">View PR on GitHub ↗</a></div>`;
            }
        }

        if (!html) {
            html = '<div class="timeline-empty">No review feedback found. Worktree may have been cleaned up.</div>';
        }
        document.getElementById('modalBody').innerHTML = html;
    } catch (err) {
        document.getElementById('modalBody').innerHTML = `<div class="timeline-empty">Failed to load feedback: ${escapeHtml(err.message)}</div>`;
    }
}

async function retryPublishFromDrawer() {
    if (!issueDetailData) return;
    const n = issueDetailData.issue_number;
    const btn = document.getElementById('issueDetailRetryPublishBtn');
    const confirmMsg = `Retry publish for issue #${n}?\n\nThis reuses the latest failed publish attempt. If a matching open PR already exists, the issue will recover to Awaiting Merge immediately. Otherwise the orchestrator will rerun push/PR creation in the background.`;
    if (!await showConfirm(confirmMsg, btn)) return;
    if (btn) btn.disabled = true;
    try {
        const req = uiActionContract.buildRetryPublishRequest(n);
        const resp = await fetch(req.endpoint, {
            method: req.method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req.body),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
            const message = data.status === 'recovered_existing_pr'
                ? `Recovered PR for #${n}`
                : `Queued publish retry for #${n}`;
            showToast(message);
            await refreshViewModel();
            await openIssueDetail(n, btn);
        } else {
            showToast(data.error || `Retry publish failed (${resp.status})`, true);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Retry publish from drawer failed:', e);
        showToast('Retry publish failed: network error', true);
        if (btn) btn.disabled = false;
    }
}

function resetIssueDetailValidation() {
    const validationEl = document.getElementById('issueDetailValidation');
    const validationBtn = document.getElementById('issueDetailValidationBtn');
    const reasonEl = document.getElementById('issueDetailValidationReason');
    const testsEl = document.getElementById('issueDetailValidationTests');
    if (validationEl) validationEl.style.display = 'none';
    if (validationBtn) {
        validationBtn.style.display = 'none';
        validationBtn.disabled = false;
        validationBtn.onclick = null;
    }
    if (reasonEl) reasonEl.textContent = '';
    if (testsEl) testsEl.innerHTML = '';
}

function renderIssueDetailValidation(detail) {
    const validationEl = document.getElementById('issueDetailValidation');
    const validationBtn = document.getElementById('issueDetailValidationBtn');
    const reasonEl = document.getElementById('issueDetailValidationReason');
    const testsEl = document.getElementById('issueDetailValidationTests');
    const summary = detail && typeof detail.summary === 'object' ? detail.summary : {};
    const diagnostic = summary && typeof summary.run_diagnostic === 'object' ? summary.run_diagnostic : null;
    const actions = Array.isArray(detail.actions) ? detail.actions : [];
    const validationAction = actions.find((action) => action && action.id === 'open_validation_failure');

    if (!validationEl || !reasonEl || !testsEl || !diagnostic) {
        resetIssueDetailValidation();
        return;
    }

    validationEl.style.display = '';
    const reason = diagnostic.reason || 'Validation failed';
    const command = diagnostic.command ? `Command: ${diagnostic.command}` : '';
    reasonEl.textContent = command ? `${reason} • ${command}` : reason;

    const preview = Array.isArray(diagnostic.failed_tests_preview)
        ? diagnostic.failed_tests_preview
        : [];
    const totalFailed = Array.isArray(diagnostic.failed_tests)
        ? diagnostic.failed_tests.length
        : preview.length;
    if (preview.length > 0) {
        const extraCount = Math.max(0, totalFailed - preview.length);
        const items = preview.map((nodeid) => `<li><code>${escapeHtml(String(nodeid))}</code></li>`);
        if (extraCount > 0) {
            items.push(`<li>${extraCount} more failing test${extraCount === 1 ? '' : 's'}…</li>`);
        }
        testsEl.innerHTML = items.join('');
    } else {
        testsEl.innerHTML = '<li>No failed test names extracted from validation output.</li>';
    }

    if (validationBtn && validationAction && validationAction.run_dir) {
        validationBtn.style.display = '';
        validationBtn.textContent = validationAction.label || 'Validation Details';
        validationBtn.onclick = () => openValidationFailure(detail.issue_number, validationAction.run_dir, 'inline');
    } else if (validationBtn) {
        validationBtn.style.display = 'none';
    }
}

function renderIssueDetail() {
    if (!issueDetailData) return;
    const d = issueDetailData;
    const title = d.title || `Issue #${d.issue_number}`;
    document.getElementById('issueDetailTitle').textContent = title;
    document.getElementById('issueDetailGitHubBtn').href = d.issue_url || '#';
    document.getElementById('issueDetailFocusBtn').onclick = () => openTimelineModal(d.issue_number);

    // Show Unblock button only for blocked issues
    const unblockBtn = document.getElementById('issueDetailUnblockBtn');
    const summary = d.summary || {};
    const hasBlockedDetail = Boolean(d.blocked_detail);
    const isBlocked = hasBlockedDetail || (summary.status || '').toLowerCase().includes('blocked');
    if (unblockBtn) {
        unblockBtn.style.display = isBlocked ? '' : 'none';
        unblockBtn.disabled = false;
    }
    const retryPublishBtn = document.getElementById('issueDetailRetryPublishBtn');
    const actions = Array.isArray(d.actions) ? d.actions : [];
    const retryPublishAction = actions.find((action) => action && action.id === 'retry_publish');
    if (retryPublishBtn) {
        retryPublishBtn.style.display = retryPublishAction ? '' : 'none';
        retryPublishBtn.disabled = false;
        retryPublishBtn.textContent = retryPublishAction && retryPublishAction.label
            ? String(retryPublishAction.label)
            : 'Retry Publish';
        retryPublishBtn.onclick = () => retryPublishFromDrawer();
    }

    // Status explanation with color-coded border
    const statusEl = document.getElementById('issueDetailStatus');
    const statusText = d.status_explanation || `Status: ${summary.status || 'unknown'}`;
    statusEl.textContent = statusText;
    statusEl.className = 'issue-detail-status';
    if (isBlocked) statusEl.classList.add('status-blocked');
    else if ((summary.status || '').toLowerCase().includes('done') || (summary.status || '').toLowerCase().includes('completed')) statusEl.classList.add('status-done');
    else if ((summary.status || '').toLowerCase().includes('running') || (summary.status || '').toLowerCase().includes('in_progress')) statusEl.classList.add('status-running');
    else statusEl.classList.add('status-queued');

    renderIssueDetailValidation(d);

    // Journey timeline with "Last run / All" filter
    const journeyEl = document.getElementById('issueDetailJourney');
    renderJourneyTimeline(journeyEl, d);

    // Previous runs (collapsed)
    const prevSection = document.getElementById('issueDetailPrevCycles');
    const prevCycles = d.previous_runs || [];
    const prevCount = d.previous_runs_count || prevCycles.length;
    if (prevCount > 0) {
        prevSection.style.display = '';
        document.getElementById('issueDetailPrevCyclesSummary').textContent = `Previous runs (${prevCount})`;
        document.getElementById('issueDetailPrevCyclesBody').innerHTML = prevCycles.map(c => `
            <div class="prev-cycle-card">
                <strong>Cycle ${escapeHtml(String(c.cycle || '?'))}</strong>
                ${c.duration_label ? ` · ${escapeHtml(c.duration_label)}` : ''}
                ${c.outcome ? ` · ${formatStatus(c.outcome)}` : ''}
                ${c.pr_url ? ` · <a href="${escapeHtml(c.pr_url)}" target="_blank" rel="noopener noreferrer">PR</a>` : ''}
                ${c.summary ? `<div class="timeline-summary">${escapeHtml(c.summary)}</div>` : ''}
            </div>
        `).join('');
    } else {
        prevSection.style.display = 'none';
    }

    // Raw events (collapsed, lazy-rendered on open)
    const rawSection = document.getElementById('issueDetailRawEvents');
    const rawBody = document.getElementById('issueDetailRawEventsBody');
    const rawCount = d.raw_events_count || (d.events || []).length;
    document.getElementById('issueDetailRawEventsSummary').textContent = `Raw events (${rawCount})`;
    rawBody.innerHTML = '';
    rawSection.ontoggle = function () {
        if (!rawSection.open || rawBody.children.length > 0) return;
        const events = d.events || [];
        rawBody.innerHTML = events.map(evt => `
            <div class="timeline-event ${evt.status || ''}">
                <div class="timeline-event-header">
                    <span>${escapeHtml(formatStepLabel(evt.step || evt.event || 'event'))}</span>
                    <span>${formatStatus(evt.status)}</span>
                </div>
                <div class="timeline-time">${escapeHtml(formatTimestamp(evt.timestamp || ''))}</div>
                ${evt.summary ? `<div class="timeline-summary">${escapeHtml(evt.summary)}</div>` : ''}
            </div>
        `).join('') || '<div class="timeline-empty">No events recorded.</div>';
    };
}

document.addEventListener('keydown', (event) => {
    if (!issueDetailDrawer || !issueDetailDrawer.classList.contains('visible')) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        closeIssueDetail();
        return;
    }
    if (event.key !== 'Tab') return;
    const focusable = getIssueDetailFocusableElements();
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const current = document.activeElement;
    if (event.shiftKey && current === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && current === last) {
        event.preventDefault();
        first.focus();
    }
});

function renderTimeline(container, events, phaseToc = [], cycles = []) {
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

    const cycleHtml = cycles.length > 0
        ? `<div class=\"timeline-loop-list\">${cycles.map(c => `
            <div class=\"timeline-loop-item\">
                <div class=\"timeline-loop-title\">Cycle ${c.cycle}</div>
                <div class=\"timeline-loop-phases\">${(c.phases || []).map(formatPhaseLabel).join(' → ')}</div>
                <div class=\"timeline-loop-status\">${formatStatus(c.status)}</div>
            </div>
        `).join('')}</div>`
        : '';

    const tocHtml = phaseToc.length > 0
        ? `<div class=\"timeline-toc\">${phaseToc.map(item => `<span class=\"timeline-toc-item\">${escapeHtml(item.label || item.phase || '')}</span>`).join('')}</div>`
        : '';

    const continuumHtml = groups.map(group => {
        const phaseLabel = formatPhaseLabel(group.phase);
        const items = group.events.map(evt => {
            const stepLabel = formatStepLabel(evt.step);
            const summary = evt.summary ? `<div class="timeline-summary">${escapeHtml(evt.summary)}</div>` : '';
            const time = evt.timestamp ? `<div class="timeline-time">${formatTimestamp(evt.timestamp)}</div>` : '';
            const artifacts = renderTimelineArtifacts(evt.artifacts || []);
            const actions = renderTimelineEventActions(evt.actions || []);
            const children = (evt.children && evt.children.length > 0)
                ? renderTimelineChildren(evt.children)
                : '';
            return `
                <div class="timeline-event ${evt.status || ''}">
                    <div class="timeline-event-header">
                        <span class="timeline-step">${escapeHtml(stepLabel)}</span>
                        <span class="timeline-status">${formatStatus(evt.status)}</span>
                    </div>
                    ${actions}
                    ${time}
                    ${summary}
                    ${artifacts}
                    ${children}
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

    const affordanceHint = '<div class="timeline-actions-hint">Use the ⋯ button on any event for actions and diagnostics.</div>';
    container.innerHTML = `${tocHtml}${cycleHtml}${affordanceHint}<div class=\"timeline-continuum\">${continuumHtml}</div>`;
    if (!container.dataset.timelineBound) {
        container.addEventListener('click', (event) => {
            const target = event.target.closest('.timeline-artifact');
            if (target && target.dataset.path) {
                openPath(target.dataset.path);
            }
            const actionTarget = event.target.closest('.timeline-action-btn, .timeline-more-item');
            if (actionTarget && actionTarget.dataset.action) {
                try {
                    const action = JSON.parse(actionTarget.dataset.action);
                    runTimelineEventAction(action);
                } catch (err) {
                    console.error('Failed to parse timeline action:', err);
                    showToast('Unable to execute timeline action', 'error');
                }
            }
        });
        container.dataset.timelineBound = 'true';
    }
}

function renderTimelineArtifacts(artifacts) {
    if (!artifacts || artifacts.length === 0) return '';
    const items = artifacts.map(artifact => {
        const label = escapeHtml(artifact.label || artifact.type || 'Artifact');
        const value = artifact.value || '';
        if (value.startsWith('http://') || value.startsWith('https://')) {
            return `<a class="timeline-artifact" href="${escapeAttr(value)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
        }
        return `<button class="timeline-artifact" type="button" data-path="${escapeAttr(value)}">${label}</button>`;
    }).join('');
    return `<div class="timeline-artifacts">${items}</div>`;
}

function renderTimelineChildren(children) {
    if (!children || children.length === 0) return '';
    const items = children.map(child => {
        const stepLabel = formatStepLabel(child.step);
        const summary = child.summary ? `<span class="timeline-child-summary">${escapeHtml(child.summary)}</span>` : '';
        const time = child.timestamp ? `<span class="timeline-child-time">${formatTimestamp(child.timestamp)}</span>` : '';
        return `
            <div class="timeline-child-event ${child.status || ''}">
                <span class="timeline-child-step">${escapeHtml(stepLabel)}</span>
                <span class="timeline-child-status">${formatStatus(child.status)}</span>
                ${time}
                ${summary}
            </div>
        `;
    }).join('');
    return `
        <details class="timeline-children">
            <summary class="timeline-children-toggle">${children.length} orchestrator event${children.length !== 1 ? 's' : ''}</summary>
            <div class="timeline-children-list">${items}</div>
        </details>
    `;
}

function renderTimelineEventActions(actions) {
    if (!actions || actions.length === 0) return '';
    const renderBtn = (action, label, cssClass = 'timeline-action-btn') => {
        const payload = escapeAttr(JSON.stringify(action));
        return `<button type="button" class="${cssClass}" data-action="${payload}">${escapeHtml(label)}</button>`;
    };
    const items = actions.map(action => ({
        action,
        label: _timelineActionShortLabel(action),
    }));
    const primaryTypes = [
        'open_validation_failure',
        'open_agent_log',
        'open_review_feedback',
        'open_review_transcript',
    ];
    const primary = [];
    const used = new Set();
    for (const type of primaryTypes) {
        const item = items.find(candidate => String(candidate.action?.type || '') === type);
        if (!item) continue;
        primary.push(item);
        used.add(item);
    }
    const secondary = items.filter(item => !used.has(item));

    let html = '<div class="timeline-event-menu-items">';
    for (const item of primary) {
        html += renderBtn(item.action, item.label);
    }
    if (secondary.length > 0) {
        html += '<details class="timeline-more-menu">';
        html += '<summary class="timeline-more-trigger">More ▾</summary>';
        html += '<div class="timeline-more-items">';
        for (const item of secondary) {
            html += renderBtn(item.action, item.label, 'timeline-more-item');
        }
        html += '</div></details>';
    }
    html += '</div>';
    return html;
}

function _timelineActionShortLabel(action) {
    if (!action) return 'Action';
    const type = String(action.type || '');
    const label = String(action.label || '').trim();
    if (type === 'open_agent_log') return 'Session Recording';
    if (type === 'open_review_transcript') return 'Review Transcript';
    if (type === 'open_validation_failure') return 'Validation Details';
    if (type === 'view_claude_log') return 'Claude Log';
    if (type === 'open_review_feedback') return 'Review Feedback';
    if (type === 'open_orchestrator_log') return 'Issue-Scoped Orchestrator Log';
    if (type === 'open_session_diagnostics') return label || 'Diagnostics';
    if (type === 'show_actions_error') return 'What is missing?';
    if (type === 'open_path') {
        const normalized = label.replace(/^Open\s+/i, '').replace(/\s+↗$/, '').trim();
        if (/^completion$/i.test(normalized)) return 'Completion Record';
        if (/^validation$/i.test(normalized)) return 'Validation Record';
        if (/^run dir$/i.test(normalized)) return 'Run Directory';
        return normalized || 'Path';
    }
    return label || 'Action';
}

function closeTimelineEventMenus(exceptMenu = null) {
    document.querySelectorAll('.timeline-event-menu[open]').forEach(menu => {
        if (exceptMenu && menu === exceptMenu) return;
        menu.removeAttribute('open');
    });
    document.querySelectorAll('.timeline-more-menu[open]').forEach(menu => {
        menu.removeAttribute('open');
    });
}

function runTimelineEventAction(action) {
    if (!action || !action.type) return;
    if (action.type === 'open_path' && action.path) {
        openPath(action.path);
        return;
    }
    if (action.type === 'open_url' && action.url) {
        window.open(action.url, '_blank', 'noopener,noreferrer');
        return;
    }
    if (action.type === 'open_review_feedback' && action.issue_number) {
        openReviewFeedback(action.issue_number, action);
        return;
    }
    if (action.type === 'open_validation_failure' && action.issue_number) {
        openValidationFailure(action.issue_number, action.run_dir || null, 'inline');
        return;
    }
    if (action.type === 'open_review_transcript' && action.issue_number) {
        openReviewTranscript(action.issue_number, action.run_dir || null, action);
        return;
    }
    if (action.type === 'open_agent_log' && action.issue_number) {
        const label = action.label ? String(action.label).replace(/^View\s+/, '') : 'Session Recording';
        openAgentLogAction(action.issue_number, action.run_dir || null, label, 'toast', action);
        return;
    }
    if (action.type === 'copy_agent_log' && action.issue_number) {
        copyAgentLogAction(action.issue_number, action.run_dir || null);
        return;
    }
    if (action.type === 'view_claude_log' && action.issue_number) {
        viewClaudeLog(action.issue_number, action.run_dir || null);
        return;
    }
    if (action.type === 'open_orchestrator_log' && action.issue_number) {
        openFilteredOrchestratorLog(action.issue_number, action.run_dir || null);
        return;
    }
    if (action.type === 'open_session_diagnostics' && action.issue_number) {
        openSessionManifest(action.issue_number, action.run_dir || null);
        return;
    }
    if (action.type === 'show_actions_error') {
        const rawMessages = Array.isArray(action.error_messages) ? action.error_messages : [];
        const normalized = rawMessages
            .filter(msg => typeof msg === 'string' && msg.trim().length > 0)
            .map(msg => msg.trim());
        const fallback = typeof action.error_message === 'string' ? action.error_message.trim() : '';
        if (normalized.length === 0 && fallback) normalized.push(fallback);
        if (normalized.length === 0) {
            showToast('No missing artifact details available', 'warning');
            return;
        }
        const issueSuffix = action.issue_number ? ` #${action.issue_number}` : '';
        const lines = normalized.map(msg => `<li>${escapeHtml(msg)}</li>`).join('');
        openModal(`What is missing${issueSuffix}`, `<ul class="diag-actions-list">${lines}</ul>`);
        return;
    }
    showToast(`Unsupported timeline action: ${action.type}`, 'error');
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

function formatJourneyHeaderTimestamp(timestamp, fallback = '') {
    if (!timestamp) return fallback;
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return fallback || timestamp;
    return date.toLocaleString();
}

function formatJourneyStepTimestamp(timestamp, fallback = '') {
    if (!timestamp) return fallback;
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return fallback || timestamp;
    const now = new Date();
    const sameDay = date.getFullYear() === now.getFullYear()
        && date.getMonth() === now.getMonth()
        && date.getDate() === now.getDate();
    if (sameDay) return date.toLocaleTimeString();
    return date.toLocaleString();
}

function openPhaseDetails() {
    if (currentPhaseData && currentPhaseData.run_dir) {
        // Open the session manifest modal with this specific run
        openSessionManifest(currentPhaseIssue, currentPhaseData.run_dir);
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

    const confirmMsg = `Full reset and requeue ${issueNumbers.length} issue${issueNumbers.length > 1 ? 's' : ''} from scratch?\n\nThis will DELETE:\n• Local worktrees\n• Remote branches\n• Orchestrator labels\n\nNext launch will force NEW branches from base (main), not prior issue branch history.`;
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

/**
 * Show a positioned confirmation popover near an anchor element.
 * Returns a Promise that resolves to true (confirm) or false (cancel).
 */
function showConfirm(message, anchorEl) {
    return new Promise(resolve => {
        // Remove any existing confirm popover
        const existing = document.getElementById('confirmPopover');
        if (existing) { existing.remove(); resolve(false); return; }

        const overlay = document.createElement('div');
        overlay.id = 'confirmPopover';
        overlay.className = 'confirm-overlay';

        const box = document.createElement('div');
        box.className = 'confirm-box';
        box.innerHTML = `
            <div class="confirm-message">${escapeHtml(message)}</div>
            <div class="confirm-actions">
                <button class="confirm-btn confirm-btn-cancel">Cancel</button>
                <button class="confirm-btn confirm-btn-ok">Confirm</button>
            </div>`;

        overlay.appendChild(box);
        document.body.appendChild(overlay);
        box.style.position = 'fixed';
        box.style.transform = 'none';
        box.style.maxWidth = `min(320px, ${Math.max(220, window.innerWidth - 16)}px)`;

        // Position near anchor element or pointer location.
        if (anchorEl) {
            const rect = box.getBoundingClientRect();
            const boxW = rect.width || 280;
            const boxH = rect.height || 120;
            let top = 8;
            let left = 8;

            if (typeof anchorEl.getBoundingClientRect === 'function') {
                const anchorRect = anchorEl.getBoundingClientRect();
                top = anchorRect.bottom + 6;
                left = anchorRect.left + anchorRect.width / 2 - boxW / 2;
                if (top + boxH > window.innerHeight - 8) top = anchorRect.top - boxH - 6;
            } else {
                const point = normalizeToClientPoint(anchorEl);
                if (point) {
                    top = point.y + 6;
                    left = point.x - (boxW / 2);
                    if (top + boxH > window.innerHeight - 8) top = point.y - boxH - 6;
                }
            }

            const clamped = clampClientPoint(left, top, boxW, boxH, 8);
            box.style.top = `${clamped.top}px`;
            box.style.left = `${clamped.left}px`;
        }

        function cleanup(result) {
            overlay.remove();
            resolve(result);
        }

        overlay.addEventListener('click', e => { if (e.target === overlay) cleanup(false); });
        box.querySelector('.confirm-btn-cancel').addEventListener('click', () => cleanup(false));
        box.querySelector('.confirm-btn-ok').addEventListener('click', () => cleanup(true));
        box.querySelector('.confirm-btn-ok').focus();
    });
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
                option.style.color = 'var(--text-muted)';
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
const REPO_ROOT = window.dashboardData?.repoRoot
    || new URLSearchParams(window.location.search).get('repo_root')
    || '';
const CONFIG_NAME = window.dashboardData?.configName
    || new URLSearchParams(window.location.search).get('config_name')
    || '';

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
        const res = await fetch(`/control/e2e/status?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
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
                body: JSON.stringify({ repo_root: REPO_ROOT, config_name: CONFIG_NAME })
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
            body: JSON.stringify({ repo_root: REPO_ROOT, config_name: CONFIG_NAME })
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
            body: JSON.stringify({ repo_root: REPO_ROOT, config_name: CONFIG_NAME })
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
        const res = await fetch(`/control/e2e/logs/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&tail=200`);
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
        const res = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
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
        const res = await fetch(`/control/e2e/summary/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
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
        const res = await fetch(`/control/e2e/diagnosis/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
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
                <p style="color: var(--text-muted);">Log file: <code>${escapeHtml(diagnosis.log_path)}</code>
                    <button class="btn-secondary btn-sm" onclick="openPath('${escapeHtml(diagnosis.log_path)}')">Open</button>
                </p>
                <p style="color: var(--warn);">${diagnosis.log_exists ? 'Log content not loaded' : 'Log file not found'}</p>
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
    if (!REPO_ROOT) {
        content.innerHTML = '<div style="color: var(--danger);">Error: no repository selected for E2E stats.</div>';
        modal.classList.add('visible');
        return;
    }

    content.innerHTML = '<div class="loading-spinner">Loading stats...</div>';
    modal.classList.add('visible');

    try {
        const res = await fetch(`/control/e2e/stats?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
        const data = await res.json();

        if (!res.ok) {
            content.innerHTML = `<div style="color: var(--danger);">Error: ${escapeHtml(data.error || data.detail || 'Failed to load stats')}</div>`;
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
        content.innerHTML = `<div style="color: var(--danger);">Error: ${escapeHtml(err.message)}</div>`;
    }
}

function closeE2EStatsModal() {
    document.getElementById('e2eStatsModal').classList.remove('visible');
}

async function showFlakyTestsList() {
    // Close stats modal and show flaky tests in a simple alert for now
    closeE2EStatsModal();
    if (!REPO_ROOT) {
        openModal('Flaky Analysis', '<p>No repository selected for E2E flaky analysis.</p>');
        return;
    }

    try {
        const res = await fetch(`/control/e2e/flaky-tests?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
        const data = await res.json();

        if (!res.ok) {
            openModal('Flaky Analysis', `<p>Failed to load flaky tests: ${escapeHtml(data.error || 'unknown error')}</p>`);
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
        const res = await fetch(`/control/e2e/test/${e2eLastRun.id}?nodeid=${encodeURIComponent(nodeid)}&repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
        const data = await res.json();

        if (!res.ok) {
            content.innerHTML = `<div style="color: var(--danger);">Error: ${escapeHtml(data.error || data.detail || 'Failed to load test details')}</div>`;
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
        content.innerHTML = `<div style="color: var(--danger);">Failed to load test details: ${escapeHtml(err.message)}</div>`;
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
            if (h.outcome === 'passed') return '<span style="color: var(--ok);">✓</span>';
            if (h.outcome === 'failed' || h.outcome === 'error') return '<span style="color: var(--danger);">✗</span>';
            return '<span style="color: var(--text-muted);">○</span>';
        }).reverse().join(' ');

        const summary = data.history_summary;
        let passRateText = '';
        if (summary.pass_rate !== null) {
            passRateText = ` (${Math.round(summary.pass_rate * 100)}% pass rate)`;
        }

        let flakyWarning = '';
        if (data.category === 'flaky') {
            flakyWarning = `<span style="color: var(--warn); margin-left: 8px;">⚠ Flaky (${data.flip_rate_percent}% flip rate)</span>`;
        } else if (data.category === 'consistently_failing') {
            flakyWarning = `<span style="color: var(--danger); margin-left: 8px;">⚠ Consistently failing</span>`;
        } else if (data.category === 'new_failure') {
            flakyWarning = `<span style="color: var(--accent); margin-left: 8px;">● New failure</span>`;
        } else if (data.category === 'recovered') {
            flakyWarning = `<span style="color: var(--ok); margin-left: 8px;">↑ Recovered</span>`;
        }

        historyHtml = `
            <div class="test-failure-section" style="background: var(--bg); padding: 12px; border-radius: 6px; margin-bottom: 16px;">
                <div style="font-size: 13px; color: var(--text-muted); margin-bottom: 4px;">History (last ${data.history.length} runs):</div>
                <div style="font-size: 16px; letter-spacing: 2px;">${icons}${passRateText}</div>
                ${flakyWarning}
            </div>
        `;
    }

    // Existing issue link
    let existingIssueHtml = '';
    if (data.existing_issue) {
        existingIssueHtml = `
            <div class="test-failure-section" style="background: var(--status-running-bg); padding: 12px; border-radius: 6px; margin-bottom: 16px; border: 1px solid var(--status-running-border);">
                <span style="color: var(--ok);">✓</span>
                <span>Issue already exists: </span>
                <a href="https://github.com/${window.dashboardData.githubOwner}/${window.dashboardData.githubRepo}/issues/${data.existing_issue.github_issue_number}"
                   target="_blank" style="color: var(--accent);">#${data.existing_issue.github_issue_number}</a>
                ${data.existing_issue.resolution ? `<span style="color: var(--text-muted);"> (${data.existing_issue.resolution})</span>` : ''}
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
            <div class="test-failure-error">${test.longrepr ? escapeHtml(test.longrepr) : '<span style="color: var(--text-muted);">No error details available</span>'}</div>
        </div>
    `;

    // Log excerpt (expandable)
    if (data.log_excerpt) {
        const lineCount = data.log_excerpt.split('\\n').length;
        html += `
            <details class="test-failure-section">
                <summary style="cursor: pointer; color: var(--accent); font-size: 14px; font-weight: 600;">
                    Test Logs (${lineCount} lines)
                </summary>
                <pre class="test-failure-traceback" style="margin-top: 8px;">${escapeHtml(data.log_excerpt)}</pre>
            </details>
        `;
    }

    // "What to do" section with Diagnose button
    html += `
        <div class="test-failure-section" style="margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--border);">
            <h3>What To Do</h3>
            <p style="color: var(--text-muted); font-size: 13px; margin-bottom: 12px;">
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
        const res = await fetch(`/control/e2e/create-issues/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
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
        const res = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
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
        const res = await fetch(`/control/e2e/diagnosis/${runId}/issue?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
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
        const res = await fetch(`/control/e2e/run/${runId}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&enhanced=false`);
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
                config_name: CONFIG_NAME,
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
        const res = await fetch(`/control/e2e/triage/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
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

async function triageE2ERun(runId = null) {
    if (runId) {
        e2eLastRun = { ...(e2eLastRun || {}), id: runId };
    }
    return showE2ETriage();
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
            <div class="triage-summary-stat" style="${consistentFailures.length > 0 ? 'color: var(--danger);' : ''}">
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
                        ${failure.category === 'consistently_failing' ? '<span class="triage-badge" style="background: var(--danger); color: var(--tab-badge-text);">Consistent</span>' : ''}
                        ${failure.category === 'new_failure' ? '<span class="triage-badge" style="background: var(--accent); color: var(--tab-badge-text);">New failure</span>' : ''}
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
        const res = await fetch(`/control/e2e/create-issues/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
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
        const res = await fetch(`/control/e2e/sync-issues/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
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
            fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`),
            fetch(`/control/e2e/flaky-tests?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&threshold=3&window=10`)
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
            const addRes = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
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
            const removeRes = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
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
        // Fetch run details and timeline in parallel
        const [detailsRes, timelineRes] = await Promise.all([
            fetch(`/control/e2e/run/${runId}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&enhanced=true`),
            fetch(`/control/e2e/run/${runId}/timeline?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`),
        ]);
        const data = await detailsRes.json();

        let timelineData = null;
        if (timelineRes.ok) {
            const tl = await timelineRes.json();
            timelineData = {
                events: tl.events || [],
                phase_toc: tl.phase_toc || [],
                cycles: tl.cycles || [],
            };
        }

        if (!detailsRes.ok) {
            content.innerHTML = `<div style="color: var(--danger); padding: 20px;">Error: ${escapeHtml(data.error || data.detail || 'Failed to load run details')}</div>`;
            return;
        }

        unifiedRunData = data;
        unifiedRunData._timeline = timelineData;
        renderUnifiedRunView(data, runId);
    } catch (err) {
        content.innerHTML = `<div style="color: var(--danger); padding: 20px;">Failed to load run details: ${escapeHtml(err.message)}</div>`;
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

    const tl = data._timeline || {};
    const hasTimeline = tl.events && tl.events.length > 0;

    // Build header with run info, summary, and tab switcher
    let html = `
        <div class="unified-run-view">
        <div class="unified-run-header">
            <div class="run-meta">
                ${run.commit_sha ? `<span class="commit">Commit: <code>${run.commit_sha.substring(0, 7)}</code></span>` : ''}
                <span class="stat">${summary.total} tests</span>
                ${summary.passed > 0 ? `<span class="stat passed">${summary.passed} passed</span>` : ''}
                ${summary.untriaged + summary.has_issue > 0 ? `<span class="stat failed">${summary.untriaged + summary.has_issue} failed</span>` : ''}
            </div>
            ${hasTimeline ? `
            <div class="e2e-run-tabs">
                <button class="e2e-run-tab active" onclick="switchE2ERunTab('tests', this)" data-tab="tests">Tests</button>
                <button class="e2e-run-tab" onclick="switchE2ERunTab('timeline', this)" data-tab="timeline">Timeline</button>
            </div>` : ''}
        </div>
    `;

    // Tests tab panel
    html += '<div id="e2eRunTestsTab" class="e2e-run-tab-panel">';

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

    // Close tests tab panel
    html += '</div>';

    // Timeline tab panel (hidden by default, populated on tab switch)
    if (hasTimeline) {
        html += '<div id="e2eRunTimelineTab" class="e2e-run-tab-panel" style="display: none;"></div>';
    }

    // Close the unified-run-view wrapper
    html += '</div>';

    content.innerHTML = html;

    // Pre-render timeline if available — pass phase_toc and cycles from
    // the shared endpoint for richer rendering (phase grouping, cycle cards)
    if (hasTimeline) {
        const timelineContainer = document.getElementById('e2eRunTimelineTab');
        renderTimeline(timelineContainer, tl.events, tl.phase_toc || [], tl.cycles || []);
    }
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
/**
 * Switch between Tests and Timeline tabs in the E2E run detail view.
 */
function switchE2ERunTab(tabName, btn) {
    // Update tab buttons
    const tabs = document.querySelectorAll('.e2e-run-tab');
    tabs.forEach(t => t.classList.remove('active'));
    if (btn) btn.classList.add('active');

    // Toggle panels
    const testsPanel = document.getElementById('e2eRunTestsTab');
    const timelinePanel = document.getElementById('e2eRunTimelineTab');
    if (testsPanel) testsPanel.style.display = tabName === 'tests' ? '' : 'none';
    if (timelinePanel) timelinePanel.style.display = tabName === 'timeline' ? '' : 'none';
}

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
        const res = await fetch(`/control/e2e/create-issues/${unifiedRunData.run.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
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
        const res = await fetch(`/control/e2e/close-issue/${issueNumber}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
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
        const res = await fetch(`/control/e2e/create-issues/${unifiedRunData.run.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
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
