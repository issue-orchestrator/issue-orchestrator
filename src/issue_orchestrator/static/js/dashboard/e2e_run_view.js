let unifiedRunData = null;  // Stores data for the current unified run view
const E2E_LABEL_OVERRIDES = Object.freeze({
    pytest: 'Pytest',
    command: 'Command',
    junit_xml: 'JUnit XML',
    html_report: 'HTML Report',
    json_report: 'JSON Report',
});

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
    const normalized = text.toLowerCase();
    if (Object.prototype.hasOwnProperty.call(E2E_LABEL_OVERRIDES, normalized)) {
        return E2E_LABEL_OVERRIDES[normalized];
    }
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
    return _humanizeSnakeCase(runnerKind);
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
    return `<button class="${cssClass}" data-artifact-path="${escapeAttr(path)}" onclick="openE2EArtifactFromButton(this); event.stopPropagation();">${escapeHtml(label)}</button>`;
}

function openE2EArtifactFromButton(button) {
    const path = button && button.dataset ? String(button.dataset.artifactPath || '').trim() : '';
    if (!path) {
        throw new Error('Artifact button missing data-artifact-path');
    }
    if (!window || typeof window.openPath !== 'function') {
        throw new Error('openPath is unavailable');
    }
    window.openPath(path);
}

function _datasetButtonAttr(name, value) {
    if (value === null || value === undefined || value === '') return '';
    return ` data-${name}="${escapeAttr(String(value))}"`;
}

function _e2eRowActionButton(label, options) {
    options = options || {};
    const action = String(options.action || '').trim();
    if (!action) {
        throw new Error('E2E row action button requires an action');
    }
    const cssClass = options.cssClass || 'action-btn';
    const disabled = options.disabled ? ' disabled aria-disabled="true"' : '';
    const title = options.title ? ` title="${escapeAttr(options.title)}"` : '';
    return `<button class="${cssClass}" data-e2e-action="${escapeAttr(action)}"${_datasetButtonAttr('nodeid', options.nodeid)}${_datasetButtonAttr('issue-number', options.issueNumber)}${_datasetButtonAttr('agent', options.agent)}${title}${disabled} onclick="runE2ERowActionFromButton(this); event.stopPropagation();">${escapeHtml(label)}</button>`;
}

function runE2ERowActionFromButton(button) {
    const dataset = button && button.dataset ? button.dataset : {};
    const action = String(dataset.e2eAction || '').trim();
    const nodeid = String(dataset.nodeid || '').trim();
    switch (action) {
    case 'close_issue': {
        const issueNumber = Number.parseInt(String(dataset.issueNumber || '').trim(), 10);
        if (!Number.isInteger(issueNumber) || !nodeid) {
            throw new Error('Close-issue action missing issue number or nodeid');
        }
        void closeE2EIssue(issueNumber, nodeid);
        return;
    }
    case 'create_issue_dropdown':
        if (!nodeid) {
            throw new Error('Create-issue action missing nodeid');
        }
        showCreateIssueDropdown(button, nodeid);
        return;
    case 'quarantine_test':
        if (!nodeid) {
            throw new Error('Quarantine action missing nodeid');
        }
        void quarantineSingleTest(nodeid);
        return;
    case 'copy_test_error':
        if (!nodeid) {
            throw new Error('Copy-error action missing nodeid');
        }
        copyTestErrorFromRun(nodeid);
        return;
    case 'create_issue_with_agent': {
        const agent = String(dataset.agent || '').trim();
        if (!nodeid || !agent) {
            throw new Error('Create-issue-with-agent action missing nodeid or agent');
        }
        void createSingleIssueWithAgent(nodeid, agent);
        return;
    }
    default:
        throw new Error(`Unknown E2E row action: ${action}`);
    }
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

// ``_renderLifecycleCommandButton``, ``runE2ELifecycleCommandFromButton``,
// and ``runE2ELifecycleCommand`` moved to the shared
// ``static/js/dashboard/lifecycle_commands.js`` module (loaded before
// this file and before ``issue_detail_drawer.js``), so the drawer and
// the E2E run view share a single Command renderer/dispatcher.  See
// issue #6310 review feedback on PR #6312.

function _flattenTestsByCategory(data) {
    const categories = _resultCategories(data);
    const order = ['untriaged', 'has_issue', 'flaky', 'fixed', 'passed', 'quarantined', 'skipped'];
    return order.flatMap(key => Array.isArray(categories[key]) ? categories[key] : []);
}

function _lifecyclesByIssueNumber(data) {
    const lifecycleInfo = _normalizeE2ERunLifecycle(data);
    const list = lifecycleInfo && Array.isArray(lifecycleInfo.e2eRun.linked_issue_lifecycles)
        ? lifecycleInfo.e2eRun.linked_issue_lifecycles
        : [];
    const map = new Map();
    for (const lifecycle of list) {
        const issueNumber = Number(lifecycle && lifecycle.issue_number);
        if (Number.isInteger(issueNumber) && issueNumber > 0) {
            map.set(issueNumber, lifecycle);
        }
    }
    return map;
}

function _e2eCapturedOutputUrl(test, runId) {
    // Only JUnit-sourced rows have captured output to serve, and only when
    // we have a positive run id. Returning '' means "no captured-output
    // placeholder for this row" — the shared renderer respects that.
    if (!Number.isFinite(runId) || runId === null) return '';
    const sourceKey = String(test && test.result_source || '').toLowerCase();
    if (!sourceKey.includes('junit')) return '';
    const nodeid = String(test && test.nodeid || '');
    if (!nodeid) return '';
    return `/api/e2e-run/${runId}/test-output?nodeid=${encodeURIComponent(nodeid)}`;
}

function _renderE2ETestRowActions(test) {
    const category = _testResultCategory(test);
    const outcomeState = _testOutcomeState(test);
    const hasErrorText = Boolean(_testErrorText(test));
    const copyErrorButton = _e2eRowActionButton(hasErrorText ? 'Copy Error' : 'No Error Text', {
        action: 'copy_test_error',
        cssClass: 'action-btn',
        nodeid: test.nodeid,
        disabled: !hasErrorText,
        title: hasErrorText ? 'Copy the failure text for this test' : 'No failure text was recorded for this test',
    });
    if (test.existing_issue) {
        const issueNum = test.existing_issue.number;
        const issueStatus = test.existing_issue.status;
        const ghLink = `<a href="https://github.com/${window.dashboardData.githubOwner}/${window.dashboardData.githubRepo}/issues/${issueNum}" target="_blank" class="issue-link-inline" onclick="event.stopPropagation();">#${issueNum} <span class="issue-status ${issueStatus}">${issueStatus}</span></a>`;
        if (category === 'fixed' && issueStatus === 'open') {
            return `${ghLink}${_e2eRowActionButton(`Close #${issueNum}`, { action: 'close_issue', cssClass: 'action-btn success', issueNumber: issueNum, nodeid: test.nodeid })}`;
        }
        return outcomeState === 'failed' ? `${ghLink}${copyErrorButton}` : ghLink;
    }
    if (_testNeedsAction(test) && outcomeState === 'failed') {
        return [
            _e2eRowActionButton('Create Issue ▼', { action: 'create_issue_dropdown', cssClass: 'action-btn primary', nodeid: test.nodeid }),
            _e2eRowActionButton('Quarantine', { action: 'quarantine_test', cssClass: 'action-btn warning', nodeid: test.nodeid }),
            copyErrorButton,
        ].join('');
    }
    if (outcomeState === 'failed') return copyErrorButton;
    return '';
}

function _renderE2EIssueLifecycleBlock(test, lifecycle, runId) {
    if (!test || !test.existing_issue || !lifecycle) return '';
    const issueNumber = Number(lifecycle.issue_number);
    const cycles = Array.isArray(lifecycle.cycles) ? lifecycle.cycles : [];
    const latestCycle = cycles.length ? cycles[cycles.length - 1] : null;
    const timelineCommand = {
        kind: 'open_issue_timeline',
        issue_number: issueNumber,
        scope_kind: 'e2e_run',
        e2e_run_id: Number.isFinite(runId) ? Number(runId) : 0,
        label: `Issue #${issueNumber}`,
    };
    const coderCmd = latestCycle ? _lifecycleSessionCommand(latestCycle.coder && latestCycle.coder.session_recording) : null;
    const reviewCmd = latestCycle ? _lifecycleSessionCommand(latestCycle.review && latestCycle.review.session_recording) : null;
    const transcriptCmd = latestCycle ? _lifecycleReviewTranscriptCommand(issueNumber, latestCycle) : null;
    const validationCmd = latestCycle ? _lifecycleValidationCommand(issueNumber, latestCycle) : null;
    const cycleChips = cycles.map(c => `<span class="e2e-lifecycle-chip">Cycle ${escapeHtml(c.cycle_number)} · ${escapeHtml(_humanizeSnakeCase(c.outcome || 'unknown'))}</span>`).join('');
    return `
        <div class="trr-lifecycle">
            <div class="trr-lifecycle-heading">Related issue activity · Issue #${issueNumber}${lifecycle.title ? ` — ${escapeHtml(lifecycle.title)}` : ''}</div>
            <div class="trr-lifecycle-cycles">${cycleChips || '<span class="e2e-empty-note">No cycles projected.</span>'}</div>
            <div class="trr-lifecycle-actions">
                ${_renderLifecycleCommandButton(timelineCommand, 'Timeline', 'action-btn primary')}
                ${_renderLifecycleCommandButton(coderCmd, 'Coder Session', 'action-btn subtle')}
                ${_renderLifecycleCommandButton(reviewCmd, 'Review Session', 'action-btn subtle')}
                ${_renderLifecycleCommandButton(transcriptCmd, 'Review Transcript', 'action-btn subtle')}
                ${_renderLifecycleCommandButton(validationCmd, 'Validation', 'action-btn subtle')}
            </div>
        </div>
    `;
}

function renderE2EResultsPanel(data) {
    const tests = _flattenTestsByCategory(data);
    const lifecycleMap = _lifecyclesByIssueNumber(data);
    const counts = {
        all: tests.length,
        action_needed: tests.filter(t => _testFilterGroup(t) === 'action_needed').length,
        tracked: tests.filter(t => _testFilterGroup(t) === 'tracked').length,
        passed_on_retry: tests.filter(t => _testFilterGroup(t) === 'passed_on_retry').length,
        passed: tests.filter(t => _testFilterGroup(t) === 'passed').length,
        skipped: tests.filter(t => _testFilterGroup(t) === 'skipped').length,
        quarantined: tests.filter(t => _testFilterGroup(t) === 'quarantined').length,
    };
    const activeFilter = counts.action_needed ? 'action_needed' : 'all';

    const runId = data && data.run && Number.isFinite(Number(data.run.id)) ? Number(data.run.id) : null;
    // E2E-specific opts injected into the shared renderer:
    //   - capturedOutputUrl    → the e2e-run endpoint (only when a JUnit row
    //                            has a valid runId; other rows opt out)
    //   - renderRowActions     → Create Issue / Quarantine / Close / Copy
    //                            buttons that only make sense for E2E test
    //                            triage
    //   - renderLifecycleBlock → "Related issue activity" cluster keyed on
    //                            test.existing_issue and the lifecycle map
    // Keeping these here means test_results_panel.js stays framework-agnostic
    // and a validation-modal consumer can use the same renderer with its
    // own opts (or no opts at all).
    const opts = {
        runId,
        capturedOutputUrl: (test) => _e2eCapturedOutputUrl(test, runId),
        renderRowActions: _renderE2ETestRowActions,
        renderLifecycleBlock: (test, lifecycle) => _renderE2EIssueLifecycleBlock(test, lifecycle, runId),
    };
    const rowsHtml = tests.length
        ? tests.map(test => {
            const lifecycle = test.existing_issue ? lifecycleMap.get(Number(test.existing_issue.number)) : null;
            return _renderTestRow(test, lifecycle, activeFilter, opts);
        }).join('')
        : '<div class="empty-state">No test cases recorded for this run.</div>';

    const untriaged = (data.results_by_category && data.results_by_category.untriaged) || [];
    const bulkBar = untriaged.length ? `
        <div class="bulk-action-bar">
            <span class="bulk-info">${untriaged.length} test${untriaged.length === 1 ? '' : 's'} need action</span>
            <div class="bulk-actions">
                <select id="unifiedRunAgent" class="agent-select">
                    <option value="">Select agent...</option>
                    ${window.dashboardData.agents.map(a => `<option value="${escapeAttr(a)}">${escapeHtml(a)}</option>`).join('')}
                </select>
                <button class="btn-primary" onclick="createIssuesForUntriaged()">Create Issues</button>
            </div>
        </div>
    ` : '';

    return `
        <div class="test-results-panel">
            ${renderTestResultsHeadline(tests)}
            ${renderTestResultsFilters(counts, activeFilter)}
            <div class="test-results-list">${rowsHtml}</div>
            ${bulkBar}
            ${renderRunDetailsDisclosure(data)}
        </div>
    `;
}

function renderRunDetailsDisclosure(data) {
    const run = data && data.run ? data.run : {};
    const command = _formatRunCommand(run);
    const buttons = _renderRunArtifactButtons(data);
    return `
        <details class="run-details-disclosure" id="runDetailsDisclosure">
            <summary>Run details &amp; artifacts<span class="rdd-summary-hint"> · runner, command, suite artifacts, suite timeline</span></summary>
            <div class="rdd-body">
                <div class="rdd-grid">
                    <div class="rdd-row"><span class="rdd-label">Runner</span><span class="rdd-value">${escapeHtml(_formatRunnerLabel(run))}</span></div>
                    <div class="rdd-row"><span class="rdd-label">Status</span><span class="rdd-value"><span class="e2e-run-status ${_runStatusClass(run.status)}">${escapeHtml(_humanizeSnakeCase(run.status || 'unknown'))}</span></span></div>
                    <div class="rdd-row"><span class="rdd-label">Started</span><span class="rdd-value">${escapeHtml(formatTimestamp(run.started_at) || 'Unknown')}</span></div>
                    <div class="rdd-row"><span class="rdd-label">Duration</span><span class="rdd-value">${escapeHtml(_formatDurationSeconds(run.duration_seconds) || '—')}</span></div>
                    ${run.commit_sha ? `<div class="rdd-row"><span class="rdd-label">Commit</span><span class="rdd-value"><code>${escapeHtml(String(run.commit_sha).substring(0, 12))}</code></span></div>` : ''}
                    ${run.branch ? `<div class="rdd-row"><span class="rdd-label">Branch</span><span class="rdd-value"><code>${escapeHtml(run.branch)}</code></span></div>` : ''}
                    <div class="rdd-row rdd-command-row"><span class="rdd-label">Command</span><span class="rdd-value">${command ? `<code class="e2e-run-command">${escapeHtml(command)}</code>` : 'Unavailable'}</span></div>
                </div>
                ${buttons ? `<div class="rdd-artifacts"><div class="rdd-section-title">Suite artifacts</div><div class="e2e-action-row">${buttons}</div></div>` : ''}
                <div class="rdd-timeline">
                    <div class="rdd-section-title">Suite timeline</div>
                    <div class="e2e-timeline-view-switcher">
                        <button class="e2e-view-btn active" onclick="switchE2ETimelineView('user', this); event.stopPropagation();" data-view="user">Story</button>
                        <button class="e2e-view-btn" onclick="switchE2ETimelineView('ops', this); event.stopPropagation();" data-view="ops">Ops</button>
                        <button class="e2e-view-btn" onclick="switchE2ETimelineView('debug', this); event.stopPropagation();" data-view="debug">Debug</button>
                    </div>
                    <div id="e2eTimelineContent"></div>
                </div>
            </div>
        </details>
    `;
}

function openE2ERunTimeline(runId) {
    // Legacy entry point: open run modal and auto-expand the Run details &
    // artifacts disclosure (which holds the suite timeline).
    return showUnifiedRunView(runId, { expandRunDetails: true });
}

/**
 * Render the run modal with a test-centric layout: tests are the headline,
 * run metadata + suite artifacts + suite timeline live in a Run details &
 * artifacts disclosure at the bottom.
 */
function renderUnifiedRunView(data, runId, options) {
    options = options || {};
    const content = document.getElementById('e2eDiagnosisContent');
    const modalTitle = document.getElementById('e2eDiagnosisModal').querySelector('.modal-header h2');
    const run = data && data.run ? data.run : {};
    const runDate = run.started_at ? new Date(run.started_at).toLocaleString() : 'Unknown';
    const commitFragment = run.commit_sha ? ` · ${String(run.commit_sha).substring(0, 7)}` : '';
    modalTitle.textContent = `Run #${run.id || runId} · ${runDate}${commitFragment}`;

    const tl = normalizeE2ETimelineData(data);
    const html = `
        <div class="unified-run-view">
            ${run.note ? `<div class="e2e-run-note-banner">${escapeHtml(run.note)}</div>` : ''}
            ${renderE2EResultsPanel(data)}
        </div>
    `;
    content.innerHTML = html;

    // Failed rows render expanded by default — kick off captured-output fetches
    // for the ones the user can see. Skip rows hidden by the initial filter
    // (a tracked-failure-heavy run defaults to "Action needed" and would
    // otherwise spam dozens of fetches for off-screen tracked rows). Collapsed
    // rows and filtered-in-later rows defer until toggle / filter dispatch.
    _autoLoadVisibleCapturedOutput(content);

    const timelineContainer = document.getElementById('e2eTimelineContent');
    if (timelineContainer) {
        renderE2ETimeline(timelineContainer, tl);
    }

    if (options.expandRunDetails) {
        const disclosure = document.getElementById('runDetailsDisclosure');
        if (disclosure) disclosure.open = true;
    }
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

async function _copyTextToClipboard(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        try {
            await navigator.clipboard.writeText(text);
            return;
        } catch (_err) {
            // Fall back for browser contexts where Clipboard API permission is unavailable.
        }
    }
    const textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.setAttribute('readonly', '');
    textArea.style.position = 'fixed';
    textArea.style.left = '-9999px';
    textArea.style.top = '0';
    document.body.appendChild(textArea);
    textArea.select();
    try {
        const copied = document.execCommand('copy');
        if (!copied) {
            throw new Error('copy command was rejected');
        }
    } finally {
        document.body.removeChild(textArea);
    }
}

function copyTestErrorFromRun(nodeid) {
    if (!unifiedRunData) return;

    const test = _findResultCase(nodeid);
    if (test) {
        const errorText = _testErrorText(test);
        if (!errorText) {
            showToast('No error text was recorded for this test', true);
            return;
        }
        const text = `Test: ${test.nodeid}\n\nResult: ${_testOutcomeState(test)}\n\nError:\n${errorText}`;
        _copyTextToClipboard(text).then(
            () => showToast('Error copied to clipboard'),
            (err) => showToast(`Failed to copy: ${err instanceof Error ? err.message : String(err)}`, true)
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
        showToast('No tests need action', true);
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
            ${window.dashboardData.agents.map(a => _e2eRowActionButton(a, {
                action: 'create_issue_with_agent',
                cssClass: 'dropdown-item',
                nodeid,
                agent: a,
            })).join('')}
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
