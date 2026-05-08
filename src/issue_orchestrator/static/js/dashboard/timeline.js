const timelineEventDetailsById = new Map();
let timelineEventDetailsSequence = 0;

function renderTimeline(container, events, phaseToc = [], cycles = []) {
    _clearTimelineEventDetails(container);
    if (!events || events.length === 0) {
        container.innerHTML = '<div class="timeline-empty">No timeline events recorded yet.</div>';
        return;
    }

    const detailIds = [];
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
            const actions = renderTimelineEventActions(evt.actions || [], evt, detailIds);
            // E2E test events carry issue_affordances — render as clickable links
            // that open the issue detail drawer routed to the explicit
            // /api/e2e-run/{run_id}/issue-detail/{N} endpoint (no base-repo
            // fallback). Each affordance is {issue_number, run_id, label?, branch_name?}.
            // When a compact label is present we show "label (N)" and put
            // the full branch name in the title attribute for hover;
            // otherwise fall back to bare "#N".
            const issueLinks = (Array.isArray(evt.issue_affordances) && evt.issue_affordances.length > 0)
                ? `<div class="timeline-issue-links">Issues: ${evt.issue_affordances.map(a => {
                    const text = a.label
                        ? `${escapeHtml(a.label)} (${a.issue_number})`
                        : `#${a.issue_number}`;
                    const title = a.branch_name
                        ? ` title="${escapeAttr(a.branch_name)}"`
                        : '';
                    return `<a href="#"${title} onclick="event.preventDefault(); openIssueDetail(${a.issue_number}, null, {e2eRunId: ${a.run_id}})">${text}</a>`;
                  }).join(' ')}</div>`
                : '';
            // Surface pytest longrepr on failed/error e2e.test_completed
            // rows so users can see WHY a test failed without leaving
            // the run drawer. The e2e worker truncates at 4000 chars,
            // so the <pre> is bounded. Expanded by default on
            // failed rows; collapsed for everything else to keep the
            // timeline scannable.
            // Render the inline failure block ONLY on the terminal row
            // (e2e.test_completed). Both test_started and test_completed
            // share the same nodeid, so if the backend ever broadens the
            // backfill to attach longrepr to test_started rows too, this
            // guard prevents the failure from being rendered twice.
            const isTerminalTestEvent = evt.event === 'e2e.test_completed';
            const failureDetail = (isTerminalTestEvent && evt.longrepr && (evt.outcome === 'failed' || evt.status === 'error'))
                ? `<details class="timeline-failure-detail" open>
                        <summary class="timeline-failure-summary">Failure: ${escapeHtml((String(evt.longrepr).split('\\n').pop() || 'Test failed').trim())}</summary>
                        <pre class="timeline-failure-longrepr">${escapeHtml(String(evt.longrepr))}</pre>
                    </details>`
                : '';
            const children = (evt.children && evt.children.length > 0)
                ? renderTimelineChildren(evt.children, detailIds)
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
                    ${failureDetail}
                    ${issueLinks}
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

    const affordanceHint = '<div class="timeline-actions-hint">Use visible row buttons for common artifacts; ⋯ opens details and additional diagnostics.</div>';
    container.innerHTML = `${tocHtml}${cycleHtml}${affordanceHint}<div class=\"timeline-continuum\">${continuumHtml}</div>`;
    if (detailIds.length > 0) {
        container.dataset.timelineDetailIds = detailIds.join(' ');
    }
    if (!container.dataset.timelineBound) {
        container.addEventListener('click', handleTimelineEventActionsClick);
        container.dataset.timelineBound = 'true';
    }
}

function handleTimelineEventActionsClick(event) {
    const clickTarget = event.target instanceof Element
        ? event.target
        : event.target?.parentElement;
    if (!clickTarget) return;
    const target = clickTarget.closest('.timeline-artifact');
    if (target && target.dataset.path) {
        openPath(target.dataset.path);
    }
    const actionTarget = clickTarget.closest('.timeline-action-btn, .timeline-menu-item');
    if (actionTarget && actionTarget.dataset.action) {
        try {
            const action = JSON.parse(actionTarget.dataset.action);
            closeTimelineEventMenus();
            runTimelineEventAction(action);
        } catch (err) {
            console.error('Failed to parse timeline action:', err);
            showToast('Unable to execute timeline action', 'error');
        }
        return;
    }
    const menuTrigger = clickTarget.closest('.timeline-event-menu-trigger');
    if (menuTrigger) {
        const ownerMenu = menuTrigger.closest('.timeline-event-menu');
        event.preventDefault();
        event.stopPropagation();
        toggleTimelineEventMenu(ownerMenu);
        return;
    }
    if (!clickTarget.closest('.timeline-event-menu')) {
        closeTimelineEventMenus();
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

function renderTimelineChildren(children, detailIds = null) {
    if (!children || children.length === 0) return '';

    // Group children by phase for visual separation (same as main timeline)
    const groups = [];
    for (const child of children) {
        const phase = child.phase || 'system';
        let group = groups[groups.length - 1];
        if (!group || group.phase !== phase) {
            group = { phase, events: [] };
            groups.push(group);
        }
        group.events.push(child);
    }

    const groupsHtml = groups.map(group => {
        const phaseLabel = formatPhaseLabel(group.phase);
        const items = group.events.map(evt => {
            const stepLabel = formatStepLabel(evt.step);
            const summary = evt.summary ? `<div class="timeline-summary">${escapeHtml(evt.summary)}</div>` : '';
            const detail = evt.detail ? `<div class="timeline-detail">${escapeHtml(evt.detail)}</div>` : '';
            const time = evt.timestamp ? `<div class="timeline-time">${formatTimestamp(evt.timestamp)}</div>` : '';
            const artifacts = renderTimelineArtifacts(evt.artifacts || []);
            const actions = renderTimelineEventActions(evt.actions || [], evt, detailIds);
            return `
                <div class="timeline-event ${evt.status || ''}">
                    <div class="timeline-event-header">
                        <span class="timeline-step">${escapeHtml(stepLabel)}</span>
                        <span class="timeline-status">${formatStatus(evt.status)}</span>
                    </div>
                    ${actions}
                    ${time}
                    ${summary}
                    ${detail}
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

    return `
        <details class="timeline-children">
            <summary class="timeline-children-toggle">${children.length} orchestrator event${children.length !== 1 ? 's' : ''}</summary>
            <div class="timeline-children-list"><div class="timeline-continuum">${groupsHtml}</div></div>
        </details>
    `;
}

function renderTimelineEventActions(actions, eventDetail = null, detailIds = null) {
    const hasActions = Array.isArray(actions) && actions.length > 0;
    const detailsAction = eventDetail
        ? {
            type: 'show_event_details',
            label: 'Event Details',
            detail_id: _registerTimelineEventDetails(eventDetail, detailIds),
        }
        : null;
    if (!hasActions && !detailsAction) return '';
    const renderBtn = (action, label, cssClass = 'timeline-action-btn') => {
        const payload = escapeAttr(JSON.stringify(action));
        return `<button type="button" class="${cssClass}" data-action="${payload}">${escapeHtml(label)}</button>`;
    };
    const items = (actions || []).map(action => ({
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

    const menuHasItems = Boolean(detailsAction) || secondary.length > 0;
    let html = '<div class="timeline-event-actions">';
    for (const item of primary) {
        html += renderBtn(item.action, item.label);
    }
    if (!menuHasItems) {
        html += '</div>';
        return html;
    }
    html += '<details class="timeline-event-menu">';
    html += '<summary class="timeline-event-menu-trigger" aria-label="More actions for this event" title="More actions for this event">⋯</summary>';
    html += '<div class="timeline-event-menu-items" role="menu">';
    if (detailsAction) {
        html += renderBtn(detailsAction, 'Event Details', 'timeline-menu-item timeline-detail-action');
    }
    for (const item of secondary) {
        html += renderBtn(item.action, item.label, 'timeline-menu-item');
    }
    html += '</div></details></div>';
    return html;
}

function _timelineEventDetailsPayload(evt) {
    if (!evt || typeof evt !== 'object') return {};
    const payload = {};
    for (const [key, value] of Object.entries(evt)) {
        if (key === 'actions') continue;
        if (value === undefined) continue;
        payload[key] = value;
    }
    return payload;
}

function _registerTimelineEventDetails(evt, detailIds = null) {
    timelineEventDetailsSequence += 1;
    const detailId = `timeline-event-detail-${timelineEventDetailsSequence}`;
    timelineEventDetailsById.set(detailId, _timelineEventDetailsPayload(evt));
    if (Array.isArray(detailIds)) detailIds.push(detailId);
    return detailId;
}

function _clearTimelineEventDetails(container) {
    const detailIds = String(container.dataset.timelineDetailIds || '').split(' ').filter(Boolean);
    for (const detailId of detailIds) {
        timelineEventDetailsById.delete(detailId);
    }
    delete container.dataset.timelineDetailIds;
}

function _timelineActionShortLabel(action) {
    if (!action) return 'Action';
    const type = String(action.type || '');
    const label = String(action.label || '').trim();
    if (type === 'show_event_details') return 'Event Details';
    if (type === 'open_agent_log') {
        if (/Reviewer Session Recording/i.test(label)) return 'Reviewer Recording';
        if (/Coding Session Recording/i.test(label)) return 'Coding Recording';
        if (/Rework Session Recording/i.test(label)) return 'Rework Recording';
        return 'Session Recording';
    }
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
}

function toggleTimelineEventMenu(menu) {
    if (!menu) return;
    const wasOpen = menu.hasAttribute('open');
    closeTimelineEventMenus(menu);
    if (wasOpen) {
        menu.removeAttribute('open');
        return;
    }
    menu.setAttribute('open', '');
    positionTimelineEventMenu(menu);
}

function _timelineEventMenuFixedOffset(items) {
    const offsetParent = items.offsetParent;
    if (
        !offsetParent
        || offsetParent === document.body
        || offsetParent === document.documentElement
    ) {
        return { top: 0, left: 0 };
    }
    if (!(offsetParent instanceof Element)) {
        return { top: 0, left: 0 };
    }
    const rect = offsetParent.getBoundingClientRect();
    return { top: rect.top, left: rect.left };
}

// Place the overflow popover below-right of its trigger using viewport
// coordinates. position: fixed lets the popover escape ancestor overflow
// containers (e.g. the e2e diagnosis modal's scrolling body) so it isn't
// clipped or made unclickable when the trigger sits near a clipped edge.
function positionTimelineEventMenu(menu) {
    if (!menu) return;
    const trigger = menu.querySelector('.timeline-event-menu-trigger');
    const items = menu.querySelector('.timeline-event-menu-items');
    if (!trigger || !items) return;
    const margin = 8;
    const triggerRect = trigger.getBoundingClientRect();
    const itemsRect = items.getBoundingClientRect();
    let top = triggerRect.bottom + 4;
    if (top + itemsRect.height > window.innerHeight - margin) {
        // Flip above the trigger if the popover would overflow the viewport bottom.
        top = Math.max(margin, triggerRect.top - itemsRect.height - 4);
    }
    let left = triggerRect.right - itemsRect.width;
    if (left < margin) left = margin;
    if (left + itemsRect.width > window.innerWidth - margin) {
        left = window.innerWidth - itemsRect.width - margin;
    }
    const fixedOffset = _timelineEventMenuFixedOffset(items);
    items.style.top = `${Math.round(top - fixedOffset.top)}px`;
    items.style.left = `${Math.round(left - fixedOffset.left)}px`;
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
    if (action.type === 'show_event_details') {
        const detailId = String(action.detail_id || '');
        const details = timelineEventDetailsById.get(detailId);
        if (!details) {
            showToast('Event details are no longer available', 'warning');
            return;
        }
        openTimelineEventDetails(details);
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

function openTimelineEventDetails(details) {
    const eventName = String(details.event || details.step || 'event');
    const rows = _renderTimelineEventDetailRows(details);
    const rawJson = escapeHtml(JSON.stringify(details, null, 2));
    modalOverlay.classList.add('timeline-event-detail-overlay');
    openModal(`Timeline Event: ${eventName}`, `
        <div class="timeline-event-detail-modal">
            ${rows}
            <details class="timeline-event-raw-json">
                <summary>Raw event JSON</summary>
                <pre>${rawJson}</pre>
            </details>
        </div>
    `);
}

function _renderTimelineEventDetailRows(details) {
    const preferred = [
        'event',
        'step',
        'status',
        'phase',
        'timestamp',
        'summary',
        'detail',
        'nodeid',
        'outcome',
        'duration',
        'issue_number',
        'run_id',
        'run_dir',
    ];
    const keys = [];
    for (const key of preferred) {
        if (Object.prototype.hasOwnProperty.call(details, key)) keys.push(key);
    }
    for (const key of Object.keys(details).sort()) {
        if (!keys.includes(key)) keys.push(key);
    }
    const rows = keys.map(key => {
        const value = _timelineEventDetailValue(details[key]);
        return `
            <div class="timeline-event-detail-row">
                <dt>${escapeHtml(key)}</dt>
                <dd><pre>${escapeHtml(value)}</pre></dd>
            </div>
        `;
    }).join('');
    return `<dl class="timeline-event-detail-list">${rows}</dl>`;
}

function _timelineEventDetailValue(value) {
    if (value === null || value === undefined) return '';
    if (typeof value === 'object') return JSON.stringify(value, null, 2);
    return String(value);
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
