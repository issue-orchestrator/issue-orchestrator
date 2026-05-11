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

// Pure data → DOM mapping. Takes the dialog payload (the "command result"
// from /api/dialog/validation-failure/) and returns {title, html, runDir}.
// No fetch, no globals beyond rendering helpers — so unit tests can call
// this directly with hand-rolled payloads instead of stubbing the network
// and going through the openValidationFailure entry point.
function renderValidationDialog(data, issueNumber, runDir = null) {
    const actionSections = Array.isArray(data.action_sections) ? data.action_sections : [];
    const resolvedRunDir = runDir || firstRunDirFromActionSections(actionSections);
    const failedTests = Array.isArray(data.failed_tests) ? data.failed_tests : [];
    const stdoutExcerpt = Array.isArray(data.stdout_excerpt) ? data.stdout_excerpt : [];
    const stderrExcerpt = Array.isArray(data.stderr_excerpt) ? data.stderr_excerpt : [];
    const status = data.status === 'passed' ? 'passed' : 'failed';
    const summaryRows = Array.isArray(data.summary_rows) && data.summary_rows.length > 0
        ? data.summary_rows
        : [
            { label: 'Outcome', value: status === 'passed' ? 'Passed' : 'Failed' },
            { label: 'Reason', value: String(data.reason || (status === 'passed' ? 'Validation passed' : 'Validation failed')) },
            { label: 'Suite', value: String(data.suite || '-') },
            { label: 'Command', value: String(data.command || '-') },
            { label: 'Exit Code', value: String(data.exit_code ?? '-') },
            { label: 'Started', value: String(data.started_at || '-') },
            { label: 'Ended', value: String(data.ended_at || '-') },
        ];

    // Header chip row stays as the at-a-glance summary; body delegates
    // to the canonical viewer (``validation_viewer.js``) which renders
    // the rich per-test triage cards + browse-by-file + per-test
    // detail.  Issue #6310 follow-up: this is the Phase-A swap — the
    // entry point still opens a modal, but the body is now the shared
    // viewer that Phase B/C will mount elsewhere too.
    let html = '<div class="diag-modal diag-validation-shell">';
    html += '<div class="diag-header">';
    html += '<div class="diag-header-title">Validation Results</div>';
    html += `<div class="diag-chip-row">${renderValidationFailureChips(status, failedTests, stdoutExcerpt, stderrExcerpt, actionSections)}</div>`;
    html += '</div>';

    html += '<section class="diag-section diag-validation-summary">';
    html += '<div class="diag-section-title">Results</div>';
    html += renderDialogRows(summaryRows, { monospace: true });
    html += '</section>';

    html += '<section class="diag-section">';
    // Pass the action-section renderer explicitly — the viewer no longer
    // reaches into session_dialogs.js globals (issue #6310 follow-up
    // reviewer Blocker 2).
    html += renderCanonicalValidationViewer(data, {
        renderActionSections: renderValidationFailureActionSections,
    });
    html += '</section>';

    html += '<div class="diag-footnote">Validation details come from the run-scoped validation record and log artifacts.</div>';
    html += '</div>';

    return {
        title: data.title || `Validation Failure #${issueNumber}`,
        html,
        runDir: resolvedRunDir,
    };
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

    const rendered = renderValidationDialog(data, issueNumber, runDir);
    currentDiagnosticsRunDir = rendered.runDir;
    openModal(rendered.title, rendered.html);
}

function renderValidationFailureChips(status, failedTests, stdoutExcerpt, stderrExcerpt, actionSections) {
    const artifactCount = (actionSections || []).reduce((count, section) => {
        const sectionActions = Array.isArray(section && section.actions) ? section.actions : [];
        return count + sectionActions.length;
    }, 0);
    const outcomeChipClass = status === 'passed' ? 'is-ok' : 'is-warn';
    const outcomeLabel = status === 'passed' ? 'passed' : 'failed';
    const chips = [
        `<span class="diag-chip ${outcomeChipClass}">${outcomeLabel}</span>`,
        `<span class="diag-chip is-muted">${failedTests.length} failing test${failedTests.length === 1 ? '' : 's'}</span>`,
        `<span class="diag-chip ${stdoutExcerpt.length > 0 ? 'is-ok' : 'is-muted'}">${stdoutExcerpt.length > 0 ? 'stdout excerpt captured' : 'no stdout excerpt'}</span>`,
        `<span class="diag-chip ${stderrExcerpt.length > 0 ? 'is-ok' : 'is-muted'}">${stderrExcerpt.length > 0 ? 'stderr captured' : 'no stderr captured'}</span>`,
    ];
    if (artifactCount > 0) {
        chips.push(`<span class="diag-chip is-ok">${artifactCount} artifact action${artifactCount === 1 ? '' : 's'}</span>`);
    }
    return chips.join('');
}

function firstRunDirFromActionSections(actionSections) {
    for (const section of actionSections || []) {
        const actions = Array.isArray(section && section.actions) ? section.actions : [];
        const runScopedAction = actions.find(action => action && action.run_dir);
        if (runScopedAction && runScopedAction.run_dir) {
            return runScopedAction.run_dir;
        }
    }
    return null;
}

function renderValidationFailureActionSections(actionSections) {
    const sections = Array.isArray(actionSections) ? actionSections : [];
    if (sections.length === 0) {
        return '<div class="diag-empty">No artifact actions available for this validation run.</div>';
    }
    let html = '<div class="diag-validation-action-groups">';
    for (const section of sections) {
        const title = escapeHtml(String((section && section.title) || 'Artifacts'));
        const actions = Array.isArray(section && section.actions) ? section.actions : [];
        if (actions.length === 0) continue;
        html += '<div class="diag-validation-action-group">';
        html += `<div class="diag-validation-action-label">${title}</div>`;
        html += '<div class="diag-validation-action-buttons">';
        for (const action of actions) {
            html += renderDialogActionWithLabel(action);
        }
        html += '</div>';
        html += '</div>';
    }
    html += '</div>';
    return html;
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
