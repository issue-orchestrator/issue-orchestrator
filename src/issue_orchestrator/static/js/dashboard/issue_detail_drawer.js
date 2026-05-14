let currentIssueDetailE2ERunId = null;
let currentIssueDetailFocus = null;

function openIssueTimeline(issueNumber, triggerEl = null, opts = {}) {
    return openIssueDetail(issueNumber, triggerEl, {...opts, focus: 'timeline'});
}

function getIssueDetailFocusableElements() {
    if (!issueDetailDrawer) return [];
    return Array.from(
        issueDetailDrawer.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
    ).filter((el) => !el.hasAttribute('disabled') && el.offsetParent !== null);
}

async function openIssueDetail(issueNumber, triggerEl = null, opts = {}) {
    if (!issueDetailDrawer) return;
    lastIssueDetailTrigger = triggerEl || document.activeElement;
    currentIssueDetailFocus = opts && opts.focus === 'timeline' ? 'timeline' : null;
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
    const closeBtn = document.getElementById('issueDetailCloseBtn');
    if (closeBtn) closeBtn.focus();

    // Explicit routing: when this was launched from an E2E run drawer
    // affordance, opts.e2eRunId tells us to fetch the issue's timeline
    // directly from the e2e-worktree for that run. Otherwise fall
    // through to the main orchestrator's issue-detail endpoint.
    const requestedE2ERunId = opts && opts.e2eRunId !== undefined && opts.e2eRunId !== null
        ? Number(opts.e2eRunId)
        : null;
    currentIssueDetailE2ERunId = Number.isInteger(requestedE2ERunId) && requestedE2ERunId > 0
        ? requestedE2ERunId
        : null;
    const url = currentIssueDetailE2ERunId
        ? `/api/e2e-run/${currentIssueDetailE2ERunId}/issue-detail/${issueNumber}?view=${timelineView}`
        : `/api/issue-detail/${issueNumber}?view=${timelineView}`;

    try {
        const res = await fetch(url);
        if (!res.ok) {
            document.getElementById('issueDetailStatus').textContent = 'Issue detail unavailable.';
            return;
        }
        issueDetailData = await res.json();
        if (issueDetailData.e2e_run_id) {
            currentIssueDetailE2ERunId = Number(issueDetailData.e2e_run_id);
        }
        renderIssueDetail();
    } catch (err) {
        console.error('Failed to load issue detail:', err);
        document.getElementById('issueDetailStatus').textContent = 'Failed to load issue detail.';
    }
}

function closeIssueDetail() {
    if (!issueDetailDrawer) return;
    currentIssueDetailFocus = null;
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
        const runLabel = run.run_label || `Run ${run.run_number || (runIndex + 1)}`;
        html += `<details class="journey-run unified-timeline-node" id="${runId}"${runExpanded ? ' open' : ''} ${_journeyDisclosureCommandAttr(runId)} ontoggle="runLifecycleCommandFromToggle(this)">
            <summary class="journey-cycle-header unified-timeline-summary">
                <span class="journey-cycle-toggle">${runToggle}</span>
                <span class="journey-cycle-label">${escapeHtml(runLabel)}</span>
                <span class="journey-cycle-outcome ${_readOutcomeBadge(run.outcome).toneClass}">\u2014 ${escapeHtml(_readOutcomeBadge(run.outcome).label || 'In progress')}</span>
                <span class="journey-cycle-time">${escapeHtml(formatJourneyHeaderTimestamp(run.timestamp || '', run.time_label || ''))}</span>
            </summary>
            <div class="journey-cycle-body${runBodyClass}" id="${runId}-body">`;

        const cycles = run.cycles || [];
        for (let cycleIndex = 0; cycleIndex < cycles.length; cycleIndex++) {
            const c = cycles[cycleIndex];
            const cycleId = `journey-cycle-${runIndex}-${cycleIndex}`;
            const cycleExpanded = journeyFilter === 'latest-run' ? true : Boolean(c.expanded);
            const toggle = cycleExpanded ? '\u25be' : '\u25b8';
            const bodyClass = cycleExpanded ? '' : ' collapsed';
            const displayCycleNumber = c.cycle_in_run || c.cycle || (cycleIndex + 1);
            const cycleLabel = c.cycle_label || `Cycle ${displayCycleNumber}`;
            const agentPill = c.agent ? `<span class="journey-cycle-agent">(${escapeHtml(c.agent)})</span>` : '';
            const retryInfo = c.retry_count > 0 ? `<span class="journey-cycle-retries">${c.retry_count} ${c.retry_count === 1 ? 'retry' : 'retries'}</span>` : '';
            const cycleOutcomeBadge = _readOutcomeBadge(c.outcome);
            const outcomeClass = cycleOutcomeBadge.toneClass;
            const artifacts = c.artifacts || {};
            const hasArtifacts = artifacts.log_url || artifacts.pr_url || artifacts.has_review_feedback;
            const validationBadge = _renderCycleValidationBadge(c.validation, issueNum);
            const artifactButton = hasArtifacts
                ? `<button type="button" class="journey-cycle-artifacts-btn" onclick="event.preventDefault(); event.stopPropagation(); toggleArtifactPopover(${runIndex}, ${cycleIndex}, ${issueNum})" title="Cycle artifacts" aria-label="Open artifacts for ${escapeAttr(cycleLabel)}">\ud83d\udcce</button>`
                : '';

            html += `<details class="journey-cycle unified-timeline-node" id="${cycleId}"${cycleExpanded ? ' open' : ''} ${_journeyDisclosureCommandAttr(cycleId)} ontoggle="runLifecycleCommandFromToggle(this)">
            <summary class="journey-cycle-header unified-timeline-summary">
                <span class="journey-cycle-toggle">${toggle}</span>
                <span class="journey-cycle-label">${escapeHtml(cycleLabel)}</span>
                ${agentPill}
                ${retryInfo}
                <span class="journey-cycle-outcome ${outcomeClass}">\u2014 ${escapeHtml(cycleOutcomeBadge.label || 'In progress')}</span>
                ${validationBadge}
                <span class="journey-cycle-time">${escapeHtml(formatJourneyHeaderTimestamp(c.timestamp || '', c.time_label || ''))}</span>
                ${artifactButton}
            </summary>
            <div class="journey-cycle-body${bodyClass}" id="${cycleId}-body">`;

            const phaseGroups = Array.isArray(c.phase_groups) && c.phase_groups.length > 0
                ? c.phase_groups
                : [{ key: 'events', label: '', steps: c.steps || [] }];
            // Run-dir context for the cycle's validation events — sourced
            // from the first step that carries one.  Validation events
            // emitted later in the cycle inherit it via the same lookup.
            const cycleRunDir = _findCycleRunDir(c);
            let stepCounter = 0;
            for (const group of phaseGroups) {
                html += `<div class="journey-phase-group">`;
                if (group.label) {
                    html += `<div class="journey-phase-header">${escapeHtml(group.label)}</div>`;
                }
                const steps = group.steps || [];
                for (const s of steps) {
                    const stepIdx = stepCounter++;
                    const stepId = `${cycleId}-step-${stepIdx}`;
                    const statusClass = s.status ? 'status-' + escapeHtml(s.status) : '';
                    const detail = s.detail ? `<div class="journey-detail">${escapeHtml(s.detail)}</div>` : '';
                    const stepActions = _filterStepActions(s);
                    const actions = renderTimelineEventActions(stepActions);

                    // Phase B: validation events become inline-expandable
                    // rows.  Expanding triggers a lazy fetch from the
                    // dialog endpoint, then renders the canonical viewer
                    // beneath the step.  No more modal popover — the
                    // detail lives in the same scroll context as the
                    // journey it belongs to.
                    if (_isValidationStep(s)) {
                        const stepRunDir = String(s.run_dir || cycleRunDir || '');
                        html += `<div class="journey-step journey-step-validation ${statusClass}" id="${stepId}">
                            <div class="journey-step-row" onclick="toggleValidationEventInline('${stepId}', ${issueNum}, ${JSON.stringify(stepRunDir)})">
                                <span class="journey-step-caret">▸</span>
                                <span class="journey-time">${escapeHtml(formatJourneyStepTimestamp(s.timestamp || '', s.time_label || ''))}</span>
                                <div class="journey-main">
                                    <div class="journey-summary-row">
                                        <span class="journey-narrative">${escapeHtml(s.narrative || s.event || '')}</span>
                                        ${actions}
                                    </div>
                                    ${detail}
                                </div>
                            </div>
                            <div class="journey-step-validation-body collapsed" id="${stepId}-body" data-loaded="0" data-run-dir="${escapeAttr(stepRunDir)}"></div>
                        </div>`;
                    } else {
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
                }
                html += `</div>`;
            }

            html += `</div></details>`;
        }
        html += `</div></details>`;
    }

    container.innerHTML = html;

    // Wire up delegated click handler for action buttons inside journey steps
    if (!container.dataset.journeyActionsBound) {
        container.addEventListener('click', handleTimelineEventActionsClick);
        container.dataset.journeyActionsBound = 'true';
    }
}

function _journeyDisclosureCommandAttr(targetId) {
    const command = {
        kind: 'sync_journey_disclosure',
        label: 'Sync Timeline Disclosure',
        target_id: String(targetId || ''),
    };
    return _renderLifecycleCommandAttr(command);
}

function syncJourneyDisclosureState(disclosure) {
    if (!disclosure) return;
    if (typeof closeTimelineEventMenus === 'function') closeTimelineEventMenus();
    const isOpen = !!disclosure.open;
    const body = disclosure.querySelector(':scope > .journey-cycle-body');
    const toggle = disclosure.querySelector(':scope > .journey-cycle-header .journey-cycle-toggle');
    if (toggle) toggle.textContent = isOpen ? '\u25be' : '\u25b8';
    if (body) {
        if (isOpen) body.classList.remove('collapsed');
        else body.classList.add('collapsed');
    }
}

// Phase B: validation events are inline-expandable within their cycle.
// Detect the four canonical validation event names (the same set the
// shared CycleValidationBadge derivation uses) so the row renders with
// a caret + lazy-loaded body instead of a flat narrative.
const _VALIDATION_EVENT_NAMES = new Set([
    'validation.passed',
    'validation.failed',
    'session.validation_passed',
    'session.validation_failed',
]);
function _isValidationStep(step) {
    if (!step || typeof step !== 'object') return false;
    const name = String(step.event || '').toLowerCase();
    return _VALIDATION_EVENT_NAMES.has(name);
}

// Phase B (reviewer Blocker 2 on PR #6315): a validation step's row IS
// the expansion trigger, so the legacy ``open_validation_failure``
// action (which the timeline action dispatcher routes to the modal)
// is both redundant and a regression back to the modal path.  Filter
// it out for validation steps so the row has exactly one owner for
// the detail-view interaction.  Non-validation steps pass through
// unchanged.
function _filterStepActions(step) {
    const raw = Array.isArray(step && step.actions) ? step.actions : [];
    if (!_isValidationStep(step)) return raw;
    return raw.filter((a) => !a || a.type !== 'open_validation_failure');
}

// The journey payload puts ``run_dir`` on individual step events when
// available.  For a validation event that doesn't carry one (older
// payloads or aggregated views), fall back to the cycle's first event
// with a run_dir — same run, same validation record.
function _findCycleRunDir(cycle) {
    if (!cycle) return '';
    if (cycle.artifacts && typeof cycle.artifacts === 'object' && typeof cycle.artifacts.run_dir === 'string') {
        return cycle.artifacts.run_dir;
    }
    const phaseGroups = Array.isArray(cycle.phase_groups) ? cycle.phase_groups : [];
    for (const group of phaseGroups) {
        for (const s of (group.steps || [])) {
            if (s && typeof s.run_dir === 'string' && s.run_dir) return s.run_dir;
        }
    }
    for (const s of (cycle.steps || [])) {
        if (s && typeof s.run_dir === 'string' && s.run_dir) return s.run_dir;
    }
    return '';
}

async function toggleValidationEventInline(stepId, issueNumber, runDir) {
    const step = document.getElementById(stepId);
    const body = document.getElementById(`${stepId}-body`);
    if (!step || !body) return;
    const caret = step.querySelector(':scope > .journey-step-row > .journey-step-caret');
    const wasCollapsed = body.classList.contains('collapsed');
    body.classList.toggle('collapsed');
    if (caret) caret.textContent = wasCollapsed ? '▾' : '▸';

    // Lazy-load on first expand.  Cache via data-loaded so subsequent
    // toggles just show/hide the existing DOM.
    if (wasCollapsed && body.dataset.loaded !== '1') {
        body.dataset.loaded = '1';  // optimistic — prevents double-fetch
        body.innerHTML = '<div class="cvv-loading">Loading validation details…</div>';
        try {
            const effectiveRunDir = runDir || body.dataset.runDir || '';
            const params = new URLSearchParams();
            if (effectiveRunDir) params.set('run_dir', effectiveRunDir);
            const suffix = params.toString() ? `?${params.toString()}` : '';
            const res = await fetch(`/api/dialog/validation-failure/${issueNumber}${suffix}`);
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.error) {
                const message = data.error || `Failed to load validation details (HTTP ${res.status})`;
                body.innerHTML = `<div class="cvv-error">${escapeHtml(String(message))}</div>`;
                body.dataset.loaded = 'error';
                return;
            }
            // Inline drawer mount uses the same canonical viewer the
            // modal does, including the artifacts footer (record /
            // output / evidence buttons).  The viewer takes the
            // action-section renderer as an explicit dependency
            // (reviewer Blocker 2 on PR #6314); without passing it
            // here we would silently drop the artifacts footer in the
            // inline path (reviewer Blocker 1 on PR #6315).
            // ``renderValidationFailureActionSections`` lives in
            // session_dialogs.js — both files are loaded as plain
            // script tags into the same global scope, so the symbol
            // is reachable at call time.
            body.innerHTML = renderCanonicalValidationViewer(data, {
                renderActionSections: renderValidationFailureActionSections,
            });
            // Phase D (issue #6310 follow-up): enhance the inline-
            // mounted canonical viewer with ARIA tree semantics +
            // keyboard nav.  The render-time HTML already carries the
            // ARIA roles; the enhancer fills in aria-level /
            // aria-setsize / aria-posinset + roving tabindex + the
            // delegated keydown handler.
            const cvvRoot = body.querySelector('.cvv-root');
            if (cvvRoot) enhanceCanonicalValidationViewerAccessibility(cvvRoot);
        } catch (err) {
            const message = err && err.message ? err.message : String(err);
            body.innerHTML = `<div class="cvv-error">Failed to load validation details: ${escapeHtml(message)}</div>`;
            body.dataset.loaded = 'error';
        }
    }
}

function _renderCycleValidationBadge(badge, _issueNumber) {
    // Phase B (issue #6310 follow-up): the badge is now an in-drawer
    // affordance, not a modal trigger.  Click → expand the cycle +
    // expand the cycle's validation event row + scroll it into view.
    // The inline expansion is the canonical viewer (same content the
    // modal used to show); see ``toggleValidationEventInline``.
    //
    // States:
    //   passed / failed  → clickable button, jumps to the inline detail
    //   not_validated    → amber static span (no detail to jump to)
    //   pending          → no badge (cycle still running)
    if (!badge || typeof badge !== 'object') return '';
    const state = String(badge.state || '').toLowerCase();
    if (state === 'pending') return '';
    if (state === 'not_validated') {
        return `<span class="journey-cycle-validation-badge is-not-validated"
            data-validation-state="not_validated"
            title="No validation recorded for this cycle — coding work without test evidence is an anti-pattern.">⚠ Not validated</span>`;
    }
    if (state !== 'passed' && state !== 'failed') return '';
    const cls = state === 'passed'
        ? 'journey-cycle-validation-badge is-passed'
        : 'journey-cycle-validation-badge is-failed';
    const label = state === 'passed' ? '✓ Validated' : '✗ Failed';
    return `<button type="button" class="${cls}"
        data-validation-state="${escapeAttr(state)}"
        onclick="event.preventDefault(); event.stopPropagation(); _handleCycleValidationBadgeClick(this);"
        title="Jump to validation details for this cycle">${label}</button>`;
}

function _handleCycleValidationBadgeClick(button) {
    if (!button) return;
    const cycle = button.closest('.journey-cycle');
    if (!cycle) return;
    // Ensure the cycle body is expanded so the validation-event row is
    // mounted and clickable.
    const cycleBody = cycle.querySelector(':scope > .journey-cycle-body');
    if (cycleBody && cycleBody.classList.contains('collapsed')) {
        toggleJourneyCycle(cycle.id);
    }
    const stepEl = cycle.querySelector(':scope > .journey-cycle-body .journey-step-validation');
    if (!stepEl) return;
    const stepBody = stepEl.querySelector(':scope > .journey-step-validation-body');
    if (stepBody && stepBody.classList.contains('collapsed')) {
        // Re-derive the runDir from the data attribute (same value the
        // server wrote when rendering the step).
        const runDir = stepBody.dataset.runDir || '';
        const issueNum = issueDetailData ? issueDetailData.issue_number : null;
        toggleValidationEventInline(stepEl.id, issueNum, runDir);
    }
    stepEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// Read the typed ``OutcomeBadge { label, tone }`` shape from a
// JourneyRun / IssueCycle payload (PR #6333).  Returns
// ``{ label, toneClass }`` where ``toneClass`` maps the projection-
// owned tone to the drawer's outcome CSS class.  Path B: no
// string-matching against backend labels at the UI layer — the
// projection already classified, we just read.
function _readOutcomeBadge(outcome) {
    const isObj = outcome && typeof outcome === 'object' && typeof outcome.label === 'string';
    const label = isObj ? outcome.label : (outcome ? String(outcome) : '');
    const tone = isObj ? String(outcome.tone || '') : '';
    let toneClass = '';
    if (tone === 'passed') toneClass = 'outcome-success';
    else if (tone === 'failed') toneClass = 'outcome-failed';
    else if (tone === 'error') toneClass = 'outcome-failed';
    // ``in_progress`` and ``neutral`` deliberately have no class so
    // the row renders without a colored treatment.
    return { label, toneClass };
}

function toggleJourneyCycle(cycleId) {
    closeTimelineEventMenus();
    const cycleNode = document.getElementById(cycleId);
    const body = document.getElementById(cycleId + '-body');
    if (!body || !cycleNode) return;
    const header = cycleNode.querySelector(':scope > .journey-cycle-header .journey-cycle-toggle');
    const isCollapsed = body.classList.contains('collapsed');
    if (cycleNode.tagName === 'DETAILS') {
        cycleNode.open = isCollapsed;
        syncJourneyDisclosureState(cycleNode);
        return;
    }
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
        const e2eRunId = currentIssueDetailE2ERunId || issueDetailData.e2e_run_id || null;
        const url = e2eRunId
            ? `/api/e2e-run/${e2eRunId}/issue-detail/${issueNumber}?view=${view}`
            : `/api/issue-detail/${issueNumber}?view=${view}`;
        try {
            const res = await fetch(url);
            if (res.ok) {
                issueDetailData = await res.json();
                if (issueDetailData.e2e_run_id) {
                    currentIssueDetailE2ERunId = Number(issueDetailData.e2e_run_id);
                }
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
        const runTime = formatJourneyHeaderTimestamp(run.timestamp || '', run.time_label || '');
        const runLabelText = _readOutcomeBadge(run.outcome).label || 'In progress';
        text += `\n${run.run_label || `Run ${run.run_number || '?'}`} \u2014 ${runLabelText}  ${runTime}\n`;
        for (const c of (run.cycles || [])) {
            const agent = c.agent ? ` (${c.agent})` : '';
            const cycleNum = c.cycle_in_run || c.cycle || '?';
            const cycleTime = formatJourneyHeaderTimestamp(c.timestamp || '', c.time_label || '');
            const cycleLabelText = _readOutcomeBadge(c.outcome).label || 'In progress';
            text += `  ${c.cycle_label || `Cycle ${cycleNum}`}${agent} \u2014 ${cycleLabelText}  ${cycleTime}\n`;
            for (const s of (c.steps || [])) {
                const time = formatJourneyStepTimestamp(s.timestamp || '', s.time_label || '');
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

function renderIssueDetail() {
    if (!issueDetailData) return;
    const d = issueDetailData;
    applyLifecycleDataset(issueDetailDrawer, d.lifecycle || null);
    document.getElementById('issueDetailTitle').textContent = formatIssueDetailTitle(d);
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

    // Validation evidence now lives per-cycle in the journey timeline below
    // (see `_renderCycleValidationBadge`); the old top-of-drawer flat list
    // was dropped in favor of cycle-scoped badges that open the validation
    // dialog for the specific cycle's run_dir.

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

    applyIssueDetailInitialFocus();
}

function formatIssueDetailTitle(detail) {
    const issueNumber = detail && detail.issue_number ? `#${detail.issue_number}` : 'Issue';
    const title = detail && detail.title ? String(detail.title).trim() : '';
    if (!title) return issueNumber;
    if (title.startsWith(issueNumber)) return title;
    return `${issueNumber}: ${title}`;
}

function applyIssueDetailInitialFocus() {
    if (currentIssueDetailFocus !== 'timeline') return;
    const timelineHeading = document.getElementById('issueDetailTimelineHeading');
    if (!timelineHeading) return;
    timelineHeading.scrollIntoView({block: 'start'});
    timelineHeading.focus({preventScroll: true});
    currentIssueDetailFocus = null;
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
