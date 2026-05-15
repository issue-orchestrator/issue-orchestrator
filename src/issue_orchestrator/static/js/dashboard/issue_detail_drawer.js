let currentIssueDetailE2ERunId = null;
let currentIssueDetailFocus = null;

function _lazyDashboardFunction(name) {
    return () => {
        const root = typeof window !== 'undefined' ? window : globalThis;
        const fn = root && root[name];
        return typeof fn === 'function' ? fn : null;
    };
}

if (typeof registerHierarchicalTimelineHostCapabilities === 'function') {
    registerHierarchicalTimelineHostCapabilities({
        formatHeaderTimestamp: _lazyDashboardFunction('formatJourneyHeaderTimestamp'),
        formatStepTimestamp: _lazyDashboardFunction('formatJourneyStepTimestamp'),
        renderEventActions: _lazyDashboardFunction('renderTimelineEventActions'),
        renderCanonicalValidationViewer: _lazyDashboardFunction('renderCanonicalValidationViewer'),
        renderValidationFailureActionSections: _lazyDashboardFunction('renderValidationFailureActionSections'),
        enhanceCanonicalValidationViewerAccessibility: _lazyDashboardFunction(
            'enhanceCanonicalValidationViewerAccessibility',
        ),
    });
}

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
    const actionsEl = document.getElementById('issueDetailActions');
    if (actionsEl) actionsEl.style.display = 'none';
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
    _renderJourneyRuns(container, data.runs || [], data || {});
}

function _collectRunIdsFromJourneyRuns(runs) {
    const ids = new Set();
    const add = (value) => {
        if (value !== undefined && value !== null && String(value) !== '') {
            ids.add(String(value));
        }
    };
    for (const run of runs || []) {
        add(run && run.run_id);
        const cycles = Array.isArray(run && run.cycles) ? run.cycles : [];
        for (const cycle of cycles) {
            add(cycle && cycle.run_id);
            for (const sessionRunId of (cycle && cycle.session_run_ids) || []) {
                add(sessionRunId);
            }
        }
    }
    return ids;
}

function _rawEventBelongsToSelectedRuns(evt, selectedRunIds) {
    if (!selectedRunIds || selectedRunIds.size === 0) return true;
    const runId = evt && evt.run_id !== undefined && evt.run_id !== null
        ? String(evt.run_id)
        : '';
    return !runId || selectedRunIds.has(runId);
}

function renderIssueRawTimelineEvents(data, selectedRuns) {
    const events = Array.isArray(data && data.events) ? data.events : [];
    const selectedRunIds = _collectRunIdsFromJourneyRuns(selectedRuns || []);
    const visibleEvents = journeyFilter === 'all'
        ? events
        : events.filter((evt) => _rawEventBelongsToSelectedRuns(evt, selectedRunIds));
    if (visibleEvents.length === 0) {
        return '<div class="timeline-empty">No raw events recorded.</div>';
    }
    return `<div class="journey-raw-events" role="list" aria-label="Raw timeline events">${
        visibleEvents.map((evt) => {
            const eventName = formatStepLabel(String(evt.step || evt.event || evt.source_event || 'event'));
            const status = evt.status ? formatStatus(evt.status) : '';
            const timestamp = formatTimestamp(evt.timestamp || '');
            const summary = evt.summary || evt.detail || '';
            return `<div class="journey-raw-event ${escapeAttr(evt.status || '')}" role="listitem">
                <div class="journey-raw-event-header">
                    <span>${escapeHtml(eventName)}</span>
                    <span>${escapeHtml(status)}</span>
                </div>
                ${timestamp ? `<div class="journey-raw-event-meta">${escapeHtml(timestamp)}</div>` : ''}
                ${summary ? `<div class="journey-raw-event-summary">${escapeHtml(summary)}</div>` : ''}
            </div>`;
        }).join('')
    }</div>`;
}

function _renderJourneyRuns(container, allRuns, data) {
    const detailData = data || issueDetailData || {};
    const runs = filterRuns(allRuns, journeyFilter);
    const isLatestRun = journeyFilter === 'latest-run';
    const isAll = journeyFilter === 'all';
    const issueNum = issueDetailData ? issueDetailData.issue_number : null;
    const timelineDiagnostic = issueDetailData?.summary?.timeline_diagnostic || null;
    const isRaw = timelineView === 'raw';

    let html = `<div class="journey-filter">
        <span class="journey-filter-group">
            <button class="journey-filter-btn ${isLatestRun ? 'active' : ''}" type="button" aria-pressed="${isLatestRun ? 'true' : 'false'}" onclick="setJourneyFilter('latest-run')" title="Show the current run (all cycles in the latest lifecycle)">Latest run</button>
            <button class="journey-filter-btn ${isAll ? 'active' : ''}" type="button" aria-pressed="${isAll ? 'true' : 'false'}" onclick="setJourneyFilter('all')">All runs</button>
        </span>
        <button class="journey-filter-btn journey-copy-btn" onclick="copyJourneyTimeline()" title="Copy timeline as text">Copy</button>
        <span class="journey-filter-separator"></span>
        <span class="journey-filter-group">
            <button class="journey-filter-btn ${timelineView === 'user' ? 'active' : ''}" type="button" aria-pressed="${timelineView === 'user' ? 'true' : 'false'}" onclick="setTimelineView('user')" title="Show key events: coding, review, outcome">Story</button>
            <button class="journey-filter-btn ${timelineView === 'ops' ? 'active' : ''}" type="button" aria-pressed="${timelineView === 'ops' ? 'true' : 'false'}" onclick="setTimelineView('ops')" title="Show operational events: validation, retries, exchanges">Ops</button>
            <button class="journey-filter-btn ${timelineView === 'debug' ? 'active' : ''}" type="button" aria-pressed="${timelineView === 'debug' ? 'true' : 'false'}" onclick="setTimelineView('debug')" title="Show all internal events">Debug</button>
            <button class="journey-filter-btn ${isRaw ? 'active' : ''}" type="button" aria-pressed="${isRaw ? 'true' : 'false'}" onclick="setTimelineView('raw')" title="Show raw timeline events">Raw events</button>
        </span>
    </div>`;

    if (isRaw) {
        html += renderIssueRawTimelineEvents(detailData, runs);
        container.innerHTML = html;
        return;
    }

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

    html += renderIssueLifecycleTimeline(runs, {
        baseId: 'journey',
        issueNumber: issueNum,
        runExpanded: (run) => journeyFilter === 'latest-run' ? true : Boolean(run.expanded),
        cycleExpanded: (cycle) => journeyFilter === 'latest-run' ? true : Boolean(cycle.expanded),
        renderCycleValidationBadge: _renderCycleValidationBadge,
        renderCycleTrailingActions: (cycle, ctx) => {
            const artifacts = cycle.artifacts || {};
            const hasArtifacts = artifacts.log_url || artifacts.pr_url || artifacts.has_review_feedback;
            if (!hasArtifacts) return '';
            const cycleLabel = cycle.cycle_label || `Cycle ${cycle.cycle_in_run || cycle.cycle || (ctx.cycleIndex + 1)}`;
            return `<button type="button" class="journey-cycle-artifacts-btn" onclick="event.preventDefault(); event.stopPropagation(); toggleArtifactPopover(${ctx.runIndex}, ${ctx.cycleIndex}, ${issueNum})" title="Cycle artifacts" aria-label="Open artifacts for ${escapeAttr(cycleLabel)}">\ud83d\udcce</button>`;
        },
    });

    container.innerHTML = html;

    // Wire up delegated click handler for action buttons inside journey steps.
    if (typeof bindTimelineEventActions === 'function') {
        bindTimelineEventActions(container);
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
        data-issue-number="${escapeAttr(_issueNumber || '')}"
        onclick="event.preventDefault(); event.stopPropagation(); runHierarchicalTimelineHostCapability('handleCycleValidationBadgeClick', this);"
        title="Jump to validation details for this cycle">${label}</button>`;
}

function toggleJourneyCycle(cycleId) {
    closeTimelineEventMenus();
    const cycleNode = document.getElementById(cycleId);
    if (!cycleNode || cycleNode.tagName !== 'DETAILS') return;
    cycleNode.open = !cycleNode.open;
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
        const runLabelText = readHierarchicalOutcomeBadge(run.outcome).label || 'In progress';
        text += `\n${run.run_label || `Run ${run.run_number || '?'}`} \u2014 ${runLabelText}  ${runTime}\n`;
        for (const c of (run.cycles || [])) {
            const agent = c.agent ? ` (${c.agent})` : '';
            const cycleNum = c.cycle_in_run || c.cycle || '?';
            const cycleTime = formatJourneyHeaderTimestamp(c.timestamp || '', c.time_label || '');
            const cycleLabelText = readHierarchicalOutcomeBadge(c.outcome).label || 'In progress';
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

    // Show Unblock button only for blocked issues
    const actionsEl = document.getElementById('issueDetailActions');
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
    if (actionsEl) {
        actionsEl.style.display = isBlocked || Boolean(retryPublishAction) ? '' : 'none';
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
    const timeline = document.getElementById('issueDetailJourney');
    if (!timeline) return;
    timeline.scrollIntoView({block: 'start'});
    timeline.focus({preventScroll: true});
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
