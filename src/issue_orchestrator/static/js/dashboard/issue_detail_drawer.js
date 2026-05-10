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
    resetIssueDetailValidation();
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
        html += `<div class="journey-run" id="${runId}">
            <div class="journey-cycle-header" onclick="toggleJourneyCycle('${runId}')">
                <span class="journey-cycle-toggle">${runToggle}</span>
                <span class="journey-cycle-label">${escapeHtml(runLabel)}</span>
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
            const cycleLabel = c.cycle_label || `Cycle ${displayCycleNumber}`;
            const agentPill = c.agent ? `<span class="journey-cycle-agent">(${escapeHtml(c.agent)})</span>` : '';
            const retryInfo = c.retry_count > 0 ? `<span class="journey-cycle-retries">${c.retry_count} ${c.retry_count === 1 ? 'retry' : 'retries'}</span>` : '';
            const outcomeClass = _cycleOutcomeClass(c.outcome || '');
            const artifacts = c.artifacts || {};
            const hasArtifacts = artifacts.log_url || artifacts.pr_url || artifacts.has_review_feedback;

            html += `<div class="journey-cycle" id="${cycleId}">
            <div class="journey-cycle-header" onclick="toggleJourneyCycle('${cycleId}')">
                <span class="journey-cycle-toggle">${toggle}</span>
                <span class="journey-cycle-label">${escapeHtml(cycleLabel)}</span>
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
        container.addEventListener('click', handleTimelineEventActionsClick);
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
        text += `\n${run.run_label || `Run ${run.run_number || '?'}`} \u2014 ${run.outcome || 'In progress'}  ${runTime}\n`;
        for (const c of (run.cycles || [])) {
            const agent = c.agent ? ` (${c.agent})` : '';
            const cycleNum = c.cycle_in_run || c.cycle || '?';
            const cycleTime = formatJourneyHeaderTimestamp(c.timestamp || '', c.time_label || '');
            text += `  ${c.cycle_label || `Cycle ${cycleNum}`}${agent} \u2014 ${c.outcome || 'In progress'}  ${cycleTime}\n`;
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

function resetIssueDetailValidation() {
    const validationEl = document.getElementById('issueDetailValidation');
    const validationBtn = document.getElementById('issueDetailValidationBtn');
    const reasonEl = document.getElementById('issueDetailValidationReason');
    const testsEl = document.getElementById('issueDetailValidationTests');
    const structuredEl = document.getElementById('issueDetailValidationStructured');
    if (validationEl) validationEl.style.display = 'none';
    if (validationBtn) {
        validationBtn.style.display = 'none';
        validationBtn.disabled = false;
        validationBtn.onclick = null;
    }
    if (reasonEl) reasonEl.textContent = '';
    if (testsEl) {
        testsEl.innerHTML = '';
        testsEl.style.display = '';
    }
    if (structuredEl) {
        structuredEl.innerHTML = '';
        structuredEl.style.display = 'none';
    }
}

function _renderIssueValidationStructured(container, junitCases) {
    if (!container) return;
    const cases = Array.isArray(junitCases) ? junitCases : [];
    if (!cases.length) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }
    const counts = {
        all: cases.length,
        failed: cases.filter(c => _testFilterGroup(c) === 'failed').length,
        passed: cases.filter(c => _testFilterGroup(c) === 'passed').length,
        skipped: cases.filter(c => _testFilterGroup(c) === 'skipped').length,
        quarantined: 0,
    };
    const rows = cases
        .map(test => _renderTestRow(test, null))
        .join('');
    container.style.display = '';
    container.innerHTML = `
        <div class="test-results-panel issue-detail-validation-structured-panel">
            ${renderTestResultsHeadline(cases)}
            ${renderTestResultsFilters(counts)}
            <div class="test-results-list">${rows}</div>
        </div>
    `;
}

function renderIssueDetailValidation(detail) {
    const validationEl = document.getElementById('issueDetailValidation');
    const validationBtn = document.getElementById('issueDetailValidationBtn');
    const reasonEl = document.getElementById('issueDetailValidationReason');
    const testsEl = document.getElementById('issueDetailValidationTests');
    const structuredEl = document.getElementById('issueDetailValidationStructured');
    const titleEl = document.querySelector('.issue-detail-validation-title');
    const summary = detail && typeof detail.summary === 'object' ? detail.summary : {};
    const diagnostic = summary && typeof summary.run_diagnostic === 'object' ? summary.run_diagnostic : null;
    const actions = Array.isArray(detail.actions) ? detail.actions : [];
    const validationAction = actions.find((action) => action && action.id === 'open_validation_failure');

    if (!validationEl || !reasonEl || !testsEl || !diagnostic) {
        resetIssueDetailValidation();
        return;
    }

    validationEl.style.display = '';
    // The diagnostic is now surfaced for both passed and failed runs.
    // Reflect the outcome in the section title and a status class so CSS
    // (and accessibility tooling) can distinguish them.
    const passed = diagnostic.state === 'validation_passed';
    validationEl.classList.toggle('is-passed', passed);
    validationEl.classList.toggle('is-failed', !passed);
    if (titleEl) {
        titleEl.textContent = passed ? 'Validation passed' : 'Validation failed';
    }
    const fallbackReason = passed ? 'Validation passed' : 'Validation failed';
    const reason = diagnostic.reason || fallbackReason;
    const command = diagnostic.command ? `Command: ${diagnostic.command}` : '';
    reasonEl.textContent = command ? `${reason} • ${command}` : reason;

    const junitCases = Array.isArray(diagnostic.junit_cases) ? diagnostic.junit_cases : [];
    if (junitCases.length > 0) {
        // Structured JUnit results available — render the test-centric view
        // (same component as the E2E run modal) and hide the simple <ul>.
        testsEl.innerHTML = '';
        testsEl.style.display = 'none';
        _renderIssueValidationStructured(structuredEl, junitCases);
    } else if (passed) {
        // Passed with no JUnit cases — most likely the repo hasn't
        // configured `validation.junit_xml_paths`. Don't render the
        // failure-list fallback (it would say "no failed test names",
        // which is true but pointless on a green run).
        if (structuredEl) {
            structuredEl.innerHTML = '';
            structuredEl.style.display = 'none';
        }
        testsEl.innerHTML = '';
        testsEl.style.display = 'none';
    } else {
        // Failed and no structured cases — fall back to the simple
        // failed-test-name preview list.
        if (structuredEl) {
            structuredEl.innerHTML = '';
            structuredEl.style.display = 'none';
        }
        testsEl.style.display = '';
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
                items.push(`<li>${extraCount} more failed test${extraCount === 1 ? '' : 's'}…</li>`);
            }
            testsEl.innerHTML = items.join('');
        } else {
            testsEl.innerHTML = '<li>No failed test names extracted from validation output.</li>';
        }
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
