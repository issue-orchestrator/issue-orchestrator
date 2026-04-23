let unifiedRunData = null;  // Stores data for the current unified run view

function _emptyE2EResultCategories() {
    return {
        untriaged: [],
        has_issue: [],
        flaky: [],
        fixed: [],
        passed: [],
        quarantined: [],
        skipped: [],
    };
}

async function _fetchE2ERunDetail(runId, view = 'user') {
    const response = await fetch(`/api/e2e-run-detail/${runId}?view=${encodeURIComponent(view)}`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
        const message = payload && typeof payload === 'object'
            ? payload.error || payload.detail || 'Failed to load run details'
            : 'Failed to load run details';
        throw new Error(String(message));
    }
    if (!payload || typeof payload !== 'object') {
        throw new Error('Run detail payload was not an object');
    }
    return payload;
}

/**
 * Show the unified run view for any E2E run.
 * This is the main entry point - called when clicking any run row.
 *
 * @param {number} runId - The E2E run ID to display
 */
async function showUnifiedRunView(runId, options) {
    options = options || {};
    const modal = document.getElementById('e2eDiagnosisModal');
    const content = document.getElementById('e2eDiagnosisContent');
    const modalTitle = modal.querySelector('.modal-header h2');

    modalTitle.textContent = `E2E Run #${runId}`;
    content.innerHTML = '<div class="loading-spinner">Loading run details...</div>';
    modal.classList.add('visible');

    try {
        unifiedRunData = await _fetchE2ERunDetail(runId, 'user');
        renderUnifiedRunView(unifiedRunData, runId, options);
    } catch (err) {
        content.innerHTML = `<div style="color: var(--danger); padding: 20px;">Failed to load run details: ${escapeHtml(err.message)}</div>`;
    }
}

function normalizeE2ETimelineData(timelineData) {
    timelineData = timelineData || {};
    const lifecycle = timelineData.lifecycle && typeof timelineData.lifecycle === 'object'
        ? timelineData.lifecycle
        : null;
    return {
        events: Array.isArray(timelineData.events) ? timelineData.events : [],
        phase_toc: Array.isArray(timelineData.phase_toc) ? timelineData.phase_toc : [],
        cycles: Array.isArray(timelineData.cycles) ? timelineData.cycles : [],
        issue_affordances: Array.isArray(timelineData.issue_affordances) ? timelineData.issue_affordances : [],
        lifecycle,
        error: timelineData.error || timelineData.detail || '',
    };
}

function _resultCategories(data) {
    if (!data || typeof data !== 'object') return _emptyE2EResultCategories();
    const payload = data.results_by_category && typeof data.results_by_category === 'object'
        ? data.results_by_category
        : {};
    return {
        untriaged: Array.isArray(payload.untriaged) ? payload.untriaged : [],
        has_issue: Array.isArray(payload.has_issue) ? payload.has_issue : [],
        flaky: Array.isArray(payload.flaky) ? payload.flaky : [],
        fixed: Array.isArray(payload.fixed) ? payload.fixed : [],
        passed: Array.isArray(payload.passed) ? payload.passed : [],
        quarantined: Array.isArray(payload.quarantined) ? payload.quarantined : [],
        skipped: Array.isArray(payload.skipped) ? payload.skipped : [],
    };
}

function _allResultCases(data) {
    return Object.values(_resultCategories(data)).flatMap(items => Array.isArray(items) ? items : []);
}

function _findResultCase(nodeid) {
    return _allResultCases(unifiedRunData).find(test => test && test.nodeid === nodeid) || null;
}

function _humanizeSnakeCase(value) {
    const text = String(value || '').trim();
    if (!text) return '';
    return text
        .replace(/_/g, ' ')
        .replace(/\b\w/g, char => char.toUpperCase());
}

function _runStatusClass(status) {
    const normalized = String(status || '').toLowerCase();
    if (normalized === 'passed') return 'passed';
    if (normalized === 'warning') return 'warning';
    if (normalized === 'running') return 'running';
    if (normalized === 'failed' || normalized === 'error') return 'failed';
    return 'muted';
}

function _formatRunnerLabel(run) {
    const runnerKind = String(run && run.runner_kind || '').trim();
    if (!runnerKind) return 'Unknown runner';
    return runnerKind === 'pytest' ? 'Pytest' : _humanizeSnakeCase(runnerKind);
}

function _formatRunCommand(run) {
    const command = Array.isArray(run && run.command) ? run.command : [];
    if (command.length > 0) return command.join(' ');
    const pytestArgs = Array.isArray(run && run.pytest_args) ? run.pytest_args : [];
    if (pytestArgs.length > 0) return ['pytest', ...pytestArgs].join(' ');
    return '';
}

function _formatDurationSeconds(value) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return '';
    return `${value.toFixed(1)}s`;
}

function _artifactButton(path, label, cssClass = 'issue-action-btn') {
    if (!path) return '';
    return `<button class="${cssClass}" onclick="openPath('${escapeAttr(path)}'); event.stopPropagation();">${escapeHtml(label)}</button>`;
}

function _primaryRunReport(data) {
    const reports = Array.isArray(data && data.reports) ? data.reports : [];
    return reports.find(report => report && report.kind === 'html_report')
        || reports.find(report => report && report.kind === 'junit_xml')
        || reports[0]
        || null;
}

function _renderRunArtifactButtons(data) {
    const run = data && data.run ? data.run : {};
    const reports = Array.isArray(data && data.reports) ? data.reports : [];
    const artifacts = Array.isArray(data && data.artifacts) ? data.artifacts : [];
    const html = [];

    if (run.log_path) {
        html.push(_artifactButton(run.log_path, 'Raw Output'));
    }
    for (const report of reports) {
        if (!report || !report.path) continue;
        html.push(_artifactButton(report.path, report.label || _humanizeSnakeCase(report.kind)));
    }
    for (const artifact of artifacts) {
        if (!artifact || !artifact.path) continue;
        if (reports.includes(artifact)) continue;
        if (artifact.kind === 'raw_log') continue;
        html.push(_artifactButton(artifact.path, artifact.label || artifact.path, 'issue-action-btn subtle'));
    }
    return html.join('');
}

function _normalizeE2ERunLifecycle(data) {
    const lifecycle = data && typeof data.lifecycle === 'object' ? data.lifecycle : null;
    if (!lifecycle || lifecycle.kind !== 'e2e_suite') return null;
    const runIteration = Array.isArray(lifecycle.runs) ? lifecycle.runs[0] : null;
    const e2eRun = runIteration && typeof runIteration === 'object' && runIteration.e2e_run && typeof runIteration.e2e_run === 'object'
        ? runIteration.e2e_run
        : null;
    if (!e2eRun) return null;
    return { container: lifecycle, runIteration, e2eRun };
}

function _lifecycleSessionCommand(recording) {
    if (!recording || typeof recording !== 'object') return null;
    if (recording.kind !== 'available') return null;
    return recording.command && typeof recording.command === 'object' ? recording.command : null;
}

function _lifecycleValidationCommand(issueNumber, cycle) {
    const coder = cycle && cycle.coder && typeof cycle.coder === 'object' ? cycle.coder : null;
    const validation = coder && coder.validation && typeof coder.validation === 'object'
        ? coder.validation
        : null;
    if (!validation || validation.kind !== 'failed' || !validation.details_command) return null;
    return {
        kind: 'open_validation_details',
        issue_number: issueNumber,
        run_dir: validation.details_command.run_dir,
        label: validation.details_command.label || 'Validation Details',
    };
}

function _lifecycleReviewTranscriptCommand(issueNumber, cycle) {
    const review = cycle && cycle.review && typeof cycle.review === 'object' ? cycle.review : null;
    const reviewSession = _lifecycleSessionCommand(review && review.session_recording);
    if (!review || review.kind !== 'review_approved') return null;
    if (!review.transcript || review.transcript.kind !== 'available') return null;
    if (!reviewSession || !reviewSession.run_dir) return null;
    return {
        kind: 'open_review_transcript',
        issue_number: issueNumber,
        run_dir: reviewSession.run_dir,
        round_index: reviewSession.round_index || null,
        transcript_role: 'reviewer',
        label: 'Review Transcript',
    };
}

function _renderLifecycleCommandButton(command, fallbackLabel = null, cssClass = 'issue-action-btn') {
    if (!command || typeof command !== 'object') return '';
    const payload = escapeAttr(JSON.stringify(command));
    const label = fallbackLabel || command.label || _humanizeSnakeCase(command.kind || 'Action');
    return `<button class="${cssClass}" data-lifecycle-command="${payload}" onclick="runE2ELifecycleCommandFromButton(this); event.stopPropagation();">${escapeHtml(label)}</button>`;
}

function runE2ELifecycleCommandFromButton(button) {
    if (!button || !button.dataset) return;
    const raw = button.dataset.lifecycleCommand || '';
    if (!raw) return;
    try {
        runE2ELifecycleCommand(JSON.parse(raw));
    } catch (err) {
        showToast(`Failed to decode lifecycle command: ${err instanceof Error ? err.message : String(err)}`, 'error');
    }
}

function runE2ELifecycleCommand(command) {
    if (!command || typeof command !== 'object') return;
    const kind = String(command.kind || '').trim();
    if (!kind) return;
    if (kind === 'open_issue_timeline' && command.issue_number) {
        const opts = command.scope_kind === 'e2e_run' && command.e2e_run_id
            ? { e2eRunId: command.e2e_run_id }
            : {};
        openIssueTimeline(command.issue_number, null, opts);
        return;
    }
    if (kind === 'open_session_recording' && command.issue_number && command.run_dir) {
        const label = command.label ? String(command.label) : 'Session Recording';
        openAgentLogAction(command.issue_number, command.run_dir, label, 'toast', {
            round_index: command.round_index || null,
            session_role: command.session_role || null,
        });
        return;
    }
    if (kind === 'open_review_transcript' && command.issue_number && command.run_dir) {
        openReviewTranscript(command.issue_number, command.run_dir, {
            round_index: command.round_index || null,
            transcript_role: command.transcript_role || null,
        }, 'toast');
        return;
    }
    if (kind === 'open_validation_details' && command.issue_number) {
        openValidationFailure(command.issue_number, command.run_dir || null, 'toast');
        return;
    }
    if (kind === 'open_completion_record' && command.path) {
        openPath(command.path);
        return;
    }
    showToast(`Unsupported lifecycle command: ${kind}`, 'warning');
}

function _renderLinkedIssueCycles(issueLifecycle) {
    const cycles = Array.isArray(issueLifecycle && issueLifecycle.cycles) ? issueLifecycle.cycles : [];
    if (!cycles.length) {
        return '<div class="e2e-empty-note">No logical cycles were projected for this linked issue.</div>';
    }
    return `
        <div class="e2e-linked-cycle-list">
            ${cycles.map(cycle => `
                <span class="e2e-lifecycle-chip">
                    Cycle ${escapeHtml(cycle.cycle_number)} · ${escapeHtml(_humanizeSnakeCase(cycle.outcome || 'unknown'))}
                </span>
            `).join('')}
        </div>
    `;
}

function _renderLinkedIssueLifecycle(issueLifecycle, runId, label = '') {
    const cycles = Array.isArray(issueLifecycle && issueLifecycle.cycles) ? issueLifecycle.cycles : [];
    const latestCycle = cycles.length ? cycles[cycles.length - 1] : null;
    const issueNumber = Number(issueLifecycle.issue_number);
    if (!Number.isInteger(issueNumber) || issueNumber <= 0) return '';

    const timelineCommand = {
        kind: 'open_issue_timeline',
        issue_number: issueNumber,
        scope_kind: 'e2e_run',
        e2e_run_id: runId,
        label: `Issue #${issueNumber}`,
    };
    const coderSessionCommand = latestCycle ? _lifecycleSessionCommand(latestCycle.coder && latestCycle.coder.session_recording) : null;
    const reviewSessionCommand = latestCycle ? _lifecycleSessionCommand(latestCycle.review && latestCycle.review.session_recording) : null;
    const transcriptCommand = latestCycle ? _lifecycleReviewTranscriptCommand(issueNumber, latestCycle) : null;
    const validationCommand = latestCycle ? _lifecycleValidationCommand(issueNumber, latestCycle) : null;
    const latestCoder = latestCycle && latestCycle.coder ? _humanizeSnakeCase(latestCycle.coder.kind || 'unknown') : 'Unknown';
    const latestReview = latestCycle && latestCycle.review ? _humanizeSnakeCase(latestCycle.review.kind || 'unknown') : 'Unknown';

    return `
        <div class="e2e-linked-issue-row" data-issue-number="${issueNumber}">
            <div class="e2e-linked-issue-copy">
                <div class="e2e-linked-issue-heading">
                    <span class="e2e-linked-issue-number">#${issueNumber}</span>
                    ${label ? `<span class="e2e-linked-issue-label">${escapeHtml(label)}</span>` : ''}
                    ${issueLifecycle.title ? `<span class="e2e-linked-issue-title">${escapeHtml(issueLifecycle.title)}</span>` : ''}
                </div>
                <div class="e2e-linked-issue-summary">
                    ${cycles.length} cycle(s) · latest coder ${escapeHtml(latestCoder)} · latest review ${escapeHtml(latestReview)}
                </div>
                ${_renderLinkedIssueCycles(issueLifecycle)}
            </div>
            <div class="e2e-action-row">
                ${_renderLifecycleCommandButton(timelineCommand, 'Timeline')}
                ${_renderLifecycleCommandButton(coderSessionCommand, 'Coder Session')}
                ${_renderLifecycleCommandButton(reviewSessionCommand, 'Review Session')}
                ${_renderLifecycleCommandButton(transcriptCommand, 'Review Transcript')}
                ${_renderLifecycleCommandButton(validationCommand, 'Validation')}
            </div>
        </div>
    `;
}

function renderE2ELinkedIssueLifecycles(data) {
    const lifecycleInfo = _normalizeE2ERunLifecycle(data);
    const run = data && data.run ? data.run : {};
    const affordances = Array.isArray(data && data.issue_affordances) ? data.issue_affordances : [];
    const labelsByIssue = new Map(
        affordances
            .filter(item => item && Number.isInteger(Number(item.issue_number)))
            .map(item => [Number(item.issue_number), item.label ? String(item.label) : ''])
    );
    const issueLifecycles = lifecycleInfo && Array.isArray(lifecycleInfo.e2eRun.linked_issue_lifecycles)
        ? lifecycleInfo.e2eRun.linked_issue_lifecycles
        : [];
    if (!issueLifecycles.length) {
        return `
            <section class="e2e-results-section" aria-labelledby="e2eLinkedLifecycleHeading">
                <div class="e2e-results-section-header">
                    <h3 class="e2e-results-title" id="e2eLinkedLifecycleHeading">Linked issue lifecycles</h3>
                    <div class="e2e-results-subtitle">Agentic coding/review cycles and session recordings stay visible here when the run exercised issues.</div>
                </div>
                <div class="e2e-empty-note">No linked issue lifecycles for this run.</div>
            </section>
        `;
    }
    return `
        <section class="e2e-results-section" aria-labelledby="e2eLinkedLifecycleHeading">
            <div class="e2e-results-section-header">
                <h3 class="e2e-results-title" id="e2eLinkedLifecycleHeading">Linked issue lifecycles</h3>
                <div class="e2e-results-subtitle">Logical cycles, coder sessions, reviewer sessions, and validation stay one click away from the run results.</div>
            </div>
            <div class="e2e-linked-issue-list">
                ${issueLifecycles.map(issueLifecycle => _renderLinkedIssueLifecycle(issueLifecycle, Number(run.id || 0), labelsByIssue.get(Number(issueLifecycle.issue_number)) || '')).join('')}
            </div>
        </section>
    `;
}

function renderE2ERunEvidenceSection(data) {
    const run = data && data.run ? data.run : {};
    const command = _formatRunCommand(run);
    const buttons = _renderRunArtifactButtons(data);
    const primaryReport = _primaryRunReport(data);
    const reportHint = primaryReport && primaryReport.label
        ? `<div class="e2e-results-subtitle">Primary report: ${escapeHtml(primaryReport.label)}</div>`
        : '';
    return `
        <section class="e2e-results-section e2e-run-evidence-section" aria-labelledby="e2eRunEvidenceHeading">
            <div class="e2e-results-section-header">
                <h3 class="e2e-results-title" id="e2eRunEvidenceHeading">Run evidence</h3>
                <div class="e2e-results-subtitle">Always-visible debugging surfaces for any framework: command, raw output, and structured reports.</div>
            </div>
            <div class="e2e-run-evidence-grid">
                <div class="e2e-run-evidence-row">
                    <span class="label">Runner</span>
                    <span class="value">${escapeHtml(_formatRunnerLabel(run))}</span>
                </div>
                <div class="e2e-run-evidence-row">
                    <span class="label">Status</span>
                    <span class="value"><span class="e2e-run-status ${_runStatusClass(run.status)}">${escapeHtml(_humanizeSnakeCase(run.status || 'unknown'))}</span></span>
                </div>
                <div class="e2e-run-evidence-row">
                    <span class="label">Started</span>
                    <span class="value">${escapeHtml(formatTimestamp(run.started_at) || 'Unknown')}</span>
                </div>
                <div class="e2e-run-evidence-row">
                    <span class="label">Duration</span>
                    <span class="value">${escapeHtml(_formatDurationSeconds(run.duration_seconds) || '—')}</span>
                </div>
                <div class="e2e-run-evidence-row e2e-run-command-row">
                    <span class="label">Command</span>
                    <span class="value">${command ? `<code class="e2e-run-command">${escapeHtml(command)}</code>` : 'Unavailable'}</span>
                </div>
            </div>
            ${buttons ? `<div class="e2e-action-row e2e-run-evidence-actions">${buttons}</div>` : '<div class="e2e-empty-note">No run-scoped logs or reports were captured for this run.</div>'}
            ${reportHint}
        </section>
    `;
}

function renderE2EResultsPanel(data) {
    const tests = _resultCategories(data);
    let html = `
        <div class="e2e-results-layout">
            ${renderE2ERunEvidenceSection(data)}
            ${renderE2ELinkedIssueLifecycles(data)}
            <section class="e2e-results-section" aria-labelledby="e2eCategorizedResultsHeading">
                <div class="e2e-results-section-header">
                    <h3 class="e2e-results-title" id="e2eCategorizedResultsHeading">Categorized results</h3>
                    <div class="e2e-results-subtitle">Framework-neutral case outcomes. Issue creation, quarantine, and close-out actions remain below.</div>
                </div>
    `;

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
        'passed', true);
    if (tests.quarantined.length > 0) {
        html += renderCategorySection('quarantined', 'QUARANTINED', tests.quarantined,
            'Tests excluded from E2E failure counts',
            'quarantined', true);
    }
    if (tests.skipped.length > 0) {
        html += renderCategorySection('skipped', 'SKIPPED', tests.skipped,
            'Tests that were skipped during this run',
            'skipped', true);
    }
    if (tests.untriaged.length > 0) {
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

    html += '</section></div>';
    return html;
}

function openE2ERunTimeline(runId) {
    return showUnifiedRunView(runId, {initialTab: 'timeline'});
}

/**
 * Render the unified run view with results grouped by category.
 */
function renderUnifiedRunView(data, runId, options) {
    options = options || {};
    const content = document.getElementById('e2eDiagnosisContent');
    const modalTitle = document.getElementById('e2eDiagnosisModal').querySelector('.modal-header h2');
    const run = data && data.run ? data.run : {};
    const summary = data && data.results_summary ? data.results_summary : { total: 0, passed: 0, untriaged: 0, has_issue: 0, flaky: 0, fixed: 0, quarantined: 0, skipped: 0 };
    const attentionCount = (summary.untriaged || 0) + (summary.has_issue || 0) + (summary.flaky || 0) + (summary.fixed || 0);

    const runDate = run.started_at ? new Date(run.started_at).toLocaleString() : 'Unknown';
    modalTitle.textContent = `Run #${run.id} - ${runDate}`;

    const tl = normalizeE2ETimelineData(data);
    const activeTab = options.initialTab === 'timeline' ? 'timeline' : 'results';
    const resultsActive = activeTab === 'results';
    const timelineActive = activeTab === 'timeline';

    let html = `
        <div class="unified-run-view">
            <div class="unified-run-header">
                <div class="run-meta">
                    ${run.commit_sha ? `<span class="commit">Commit: <code>${run.commit_sha.substring(0, 7)}</code></span>` : ''}
                    <span class="stat runner">${escapeHtml(_formatRunnerLabel(run))}</span>
                    <span class="stat">${summary.total || 0} results</span>
                    ${summary.passed > 0 ? `<span class="stat passed">${summary.passed} passed</span>` : ''}
                    ${attentionCount > 0 ? `<span class="stat failed">${attentionCount} attention</span>` : ''}
                </div>
                <div class="e2e-run-tabs">
                    <button class="e2e-run-tab ${resultsActive ? 'active' : ''}" onclick="switchE2ERunTab('results', this)" data-tab="results">Results</button>
                    <button class="e2e-run-tab ${timelineActive ? 'active' : ''}" onclick="switchE2ERunTab('timeline', this)" data-tab="timeline">Timeline</button>
                </div>
            </div>
            ${run.note ? `<div class="e2e-run-note-banner">${escapeHtml(run.note)}</div>` : ''}
            <div id="e2eRunResultsTab" class="e2e-run-tab-panel" style="${resultsActive ? '' : 'display: none;'}">
                ${renderE2EResultsPanel(data)}
            </div>
            <div id="e2eRunTimelineTab" class="e2e-run-tab-panel" style="${timelineActive ? '' : 'display: none;'}">
                <div class="e2e-timeline-view-switcher">
                    <button class="e2e-view-btn active" onclick="switchE2ETimelineView('user', this)" data-view="user">Story</button>
                    <button class="e2e-view-btn" onclick="switchE2ETimelineView('ops', this)" data-view="ops">Ops</button>
                    <button class="e2e-view-btn" onclick="switchE2ETimelineView('debug', this)" data-view="debug">Debug</button>
                </div>
                <div id="e2eTimelineContent"></div>
            </div>
        </div>
    `;

    content.innerHTML = html;

    const timelineContainer = document.getElementById('e2eTimelineContent');
    renderE2ETimeline(timelineContainer, tl);
}

function renderE2ETimeline(container, timelineData) {
    if (!container) return;
    const tl = normalizeE2ETimelineData(timelineData || {});
    applyLifecycleDataset(container, tl.lifecycle);
    container.innerHTML = `
        ${renderE2EIssueTimelineAffordances(tl.issue_affordances)}
        ${tl.error ? `<div class="timeline-empty e2e-timeline-error">${escapeHtml(tl.error)}</div>` : ''}
        <div class="e2e-timeline-events"></div>
    `;
    const eventsContainer = container.querySelector('.e2e-timeline-events');
    renderTimeline(eventsContainer, tl.events, tl.phase_toc, tl.cycles);
}

function renderE2EIssueTimelineAffordances(affordances) {
    const items = (Array.isArray(affordances) ? affordances : [])
        .map((affordance) => {
            const issueNumber = Number(affordance.issue_number);
            const runId = Number(affordance.run_id);
            if (!Number.isInteger(issueNumber) || !Number.isInteger(runId)) return '';
            const label = affordance.label ? String(affordance.label) : '';
            const labelHtml = label
                ? `<span class="e2e-issue-timeline-label">${escapeHtml(label)}</span>`
                : '';
            return `<button class="e2e-issue-timeline-btn" onclick="openIssueTimeline(${issueNumber}, this, {e2eRunId: ${runId}});event.stopPropagation();" title="Open cycle timeline for issue #${issueNumber}" aria-label="Open cycle timeline for issue #${issueNumber}">
                <span class="e2e-issue-timeline-number">#${issueNumber}</span>${labelHtml}
            </button>`;
        })
        .filter(Boolean);
    const body = items.length
        ? `<div class="e2e-issue-timeline-list">${items.join('')}</div>`
        : '<div class="e2e-empty-note">No linked issue timelines for this run.</div>';
    return `<section class="e2e-issue-timeline-affordances" aria-label="Issue timelines from this E2E run">
        <div class="e2e-issue-timeline-title">Issue timelines</div>
        ${body}
    </section>`;
}

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

function _renderResultEvidenceButtons(category) {
    if (!unifiedRunData) return '';
    if (category === 'passed' || category === 'skipped' || category === 'quarantined') return '';
    const run = unifiedRunData.run || {};
    const primaryReport = _primaryRunReport(unifiedRunData);
    const buttons = [];
    if (run.log_path) {
        buttons.push(_artifactButton(run.log_path, 'Run Log', 'action-btn subtle'));
    }
    if (primaryReport && primaryReport.path) {
        buttons.push(_artifactButton(primaryReport.path, primaryReport.label || 'Report', 'action-btn subtle'));
    }
    return buttons.join('');
}

function renderTestRow(test, category) {
    const shortName = test.display_name || test.nodeid.split('::').pop() || test.nodeid;
    const effectiveOutcome = test.retry_outcome || test.outcome;
    const outcomeIcon = effectiveOutcome === 'passed' ? '✓' : effectiveOutcome === 'skipped' ? '○' : '✗';
    const outcomeClass = effectiveOutcome === 'passed' ? 'passed' : effectiveOutcome === 'skipped' ? 'skipped' : 'failed';

    let historyHtml = '';
    if (test.history && test.history.length > 0) {
        const icons = test.history.map(h => {
            if (h.outcome === 'passed') return '<span class="hist-icon pass">✓</span>';
            if (h.outcome === 'failed') return '<span class="hist-icon fail">✗</span>';
            return '<span class="hist-icon skip">○</span>';
        }).reverse().join('');
        historyHtml = `<span class="test-history">${icons}</span>`;
    }

    const flipRateHtml = test.flip_rate_percent && test.flip_rate_percent > 0
        ? `<span class="flip-rate">${test.flip_rate_percent}%</span>`
        : '';
    const durationHtml = test.duration_seconds
        ? `<span class="duration">${test.duration_seconds.toFixed(1)}s</span>`
        : '';
    const suiteHtml = test.suite_name
        ? `<div class="test-suite" title="${escapeHtml(test.suite_name)}">${escapeHtml(test.suite_name)}</div>`
        : '';
    const sourceHtml = test.result_source && test.result_source !== 'runtime'
        ? `<span class="test-source">${escapeHtml(_humanizeSnakeCase(test.result_source))}</span>`
        : '';

    let actionsHtml = _renderResultEvidenceButtons(category);
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
                    ${actionsHtml}
                </div>
            `;
        } else {
            actionsHtml = `
                <a href="https://github.com/${window.dashboardData.githubOwner}/${window.dashboardData.githubRepo}/issues/${issueNum}"
                   target="_blank" class="issue-link-inline" onclick="event.stopPropagation();">
                    → #${issueNum} <span class="issue-status ${issueStatus}">${issueStatus}</span>
                </a>
                ${actionsHtml ? `<div class="test-actions">${actionsHtml}</div>` : ''}
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
                ${actionsHtml}
            </div>
        `;
    } else if (actionsHtml) {
        actionsHtml = `<div class="test-actions">${actionsHtml}</div>`;
    }

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
                <div class="test-row-copy">
                    <span class="test-name" title="${escapeHtml(test.nodeid)}">${escapeHtml(shortName)}</span>
                    ${suiteHtml}
                </div>
                ${sourceHtml}
                ${historyHtml}
                ${flipRateHtml}
                ${durationHtml}
                ${actionsHtml}
            </div>
            ${errorPreviewHtml}
        </div>
    `;
}

function switchE2ERunTab(tabName, btn) {
    const tabs = document.querySelectorAll('.e2e-run-tab');
    tabs.forEach(t => t.classList.remove('active'));
    if (btn) btn.classList.add('active');

    const resultsPanel = document.getElementById('e2eRunResultsTab');
    const timelinePanel = document.getElementById('e2eRunTimelineTab');
    if (resultsPanel) resultsPanel.style.display = tabName === 'results' ? '' : 'none';
    if (timelinePanel) timelinePanel.style.display = tabName === 'timeline' ? '' : 'none';
}

async function switchE2ETimelineView(view, btn) {
    const btns = document.querySelectorAll('.e2e-view-btn');
    btns.forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');

    const runId = unifiedRunData && unifiedRunData.run ? unifiedRunData.run.id : null;
    if (!runId) return;

    const container = document.getElementById('e2eTimelineContent');
    if (!container) return;
    container.innerHTML = '<div class="loading-spinner">Loading...</div>';

    try {
        const data = await _fetchE2ERunDetail(runId, view);
        unifiedRunData = data;
        renderE2ETimeline(container, data);
    } catch (err) {
        container.innerHTML = `<div style="color: var(--danger);">Error: ${escapeHtml(err.message)}</div>`;
    }
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
            const test = _findResultCase(nodeid);
            if (test && test.longrepr) {
                const lines = test.longrepr.split('\n');
                errorText.textContent = lines.slice(0, 2).join('\n');
            }
        }
    } else {
        // Expand: show full error
        preview.classList.add('expanded');
        button.textContent = 'Collapse ▲';
        const errorText = preview.querySelector('.error-text');
        if (errorText && unifiedRunData) {
            const test = _findResultCase(nodeid);
            if (test && test.longrepr) {
                errorText.textContent = test.longrepr;
            }
        }
    }
}

/**
 * Copy error text for a specific test.
 */
function copyTestErrorFromRun(nodeid) {
    if (!unifiedRunData) return;

    const test = _findResultCase(nodeid);
    if (test) {
        const text = `Test: ${test.nodeid}\n\nError:\n${test.longrepr || 'No error details'}`;
        navigator.clipboard.writeText(text).then(
            () => showToast('Error copied to clipboard'),
            () => showToast('Failed to copy', true)
        );
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

    const untriaged = _resultCategories(unifiedRunData).untriaged || [];
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
