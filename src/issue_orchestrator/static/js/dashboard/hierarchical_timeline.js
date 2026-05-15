// Shared renderer for hierarchical timeline rows.
//
// Dashboard issue timelines and E2E run history both use native
// ``<details>/<summary>`` disclosure rows.  This module owns that shell
// so command wiring, disclosure semantics, and caret behavior stay in one
// place while each caller supplies only its context-specific row content.

function _joinHierarchicalTimelineClasses(...parts) {
    return parts
        .flatMap((part) => String(part || '').split(/\s+/))
        .map((part) => part.trim())
        .filter(Boolean)
        .join(' ');
}

function _renderHierarchicalTimelineExtraAttrs(attrs) {
    if (!attrs || typeof attrs !== 'object') return '';
    const rendered = [];
    for (const [name, value] of Object.entries(attrs)) {
        if (!name || value === null || value === undefined || value === false) continue;
        if (value === true) {
            rendered.push(escapeHtml(name));
            continue;
        }
        rendered.push(`${escapeHtml(name)}="${escapeAttr(String(value))}"`);
    }
    return rendered.join(' ');
}

function renderHierarchicalTimelineNode(node) {
    if (!node || typeof node !== 'object') return '';

    const className = _joinHierarchicalTimelineClasses(node.className);
    const summaryClassName = _joinHierarchicalTimelineClasses(node.summaryClassName);
    const bodyClassName = _joinHierarchicalTimelineClasses(node.bodyClassName);
    const caretClassName = _joinHierarchicalTimelineClasses(
        node.caretClassName,
        'hierarchical-timeline-caret',
    );

    const detailsAttrs = [];
    if (className) detailsAttrs.push(`class="${escapeAttr(className)}"`);
    if (node.id) detailsAttrs.push(`id="${escapeAttr(String(node.id))}"`);
    if (node.open === true) detailsAttrs.push('open');
    if (node.role) detailsAttrs.push(`role="${escapeAttr(String(node.role))}"`);

    const extraAttrs = _renderHierarchicalTimelineExtraAttrs(node.attrs);
    if (extraAttrs) detailsAttrs.push(extraAttrs);
    if (node.command) {
        const commandAttr = _renderLifecycleCommandAttr(node.command);
        if (commandAttr) detailsAttrs.push(commandAttr);
        detailsAttrs.push('ontoggle="runLifecycleCommandFromToggle(this)"');
    }

    const summaryAttrs = summaryClassName ? ` class="${escapeAttr(summaryClassName)}"` : '';
    const bodyAttrs = [];
    if (bodyClassName) bodyAttrs.push(`class="${escapeAttr(bodyClassName)}"`);
    if (node.bodyId) bodyAttrs.push(`id="${escapeAttr(String(node.bodyId))}"`);
    const detailsAttrText = detailsAttrs.length > 0 ? ` ${detailsAttrs.join(' ')}` : '';
    const bodyAttrText = bodyAttrs.length > 0 ? ` ${bodyAttrs.join(' ')}` : '';

    return (
        `<details${detailsAttrText}>` +
        `<summary${summaryAttrs}>` +
        `<span class="${escapeAttr(caretClassName)}" aria-hidden="true"></span>` +
        `${node.summaryHtml || ''}` +
        `</summary>` +
        `<div${bodyAttrText}>${node.bodyHtml || ''}</div>` +
        `</details>`
    );
}

function renderHierarchicalTimelineList(nodes) {
    if (!Array.isArray(nodes)) return '';
    return nodes.map((node) => renderHierarchicalTimelineNode(node)).join('');
}

function _readHierarchicalOutcomeBadge(outcome) {
    const isObj = outcome && typeof outcome === 'object' && typeof outcome.label === 'string';
    const label = isObj ? outcome.label : (outcome ? String(outcome) : '');
    const tone = isObj ? String(outcome.tone || '') : '';
    let toneClass = '';
    if (tone === 'passed') toneClass = 'outcome-success';
    else if (tone === 'failed' || tone === 'error') toneClass = 'outcome-failed';
    else if (tone === 'warning') toneClass = 'outcome-warning';
    const normalizedTone = (
        tone === 'passed' || tone === 'failed' || tone === 'error'
        || tone === 'in_progress' || tone === 'neutral'
    ) ? tone : 'neutral';
    return { label: label || 'In progress', tone: normalizedTone, toneClass };
}

function _hierarchicalToneGlyph(tone) {
    if (tone === 'failed') return '✕';
    if (tone === 'error') return '⚠';
    if (tone === 'in_progress') return '…';
    if (tone === 'neutral') return '·';
    return '✓';
}

function _formatHierarchicalHeaderTimestamp(timestamp, fallback = '') {
    return typeof formatJourneyHeaderTimestamp === 'function'
        ? formatJourneyHeaderTimestamp(timestamp, fallback)
        : (fallback || timestamp || '');
}

function _formatHierarchicalStepTimestamp(timestamp, fallback = '') {
    return typeof formatJourneyStepTimestamp === 'function'
        ? formatJourneyStepTimestamp(timestamp, fallback)
        : (fallback || timestamp || '');
}

function _callTimelineOption(options, name, ...args) {
    const fn = options && options[name];
    return typeof fn === 'function' ? fn(...args) : null;
}

const _ISSUE_LIFECYCLE_VALIDATION_EVENT_NAMES = new Set([
    'validation.passed',
    'validation.failed',
    'session.validation_passed',
    'session.validation_failed',
]);

function _defaultIssueLifecycleRunLabel(run, ctx) {
    return run.run_label || `Run ${run.run_number || (ctx.runIndex + 1)}`;
}

function _defaultIssueLifecycleCycleLabel(cycle, ctx) {
    const displayCycleNumber = cycle.cycle_in_run || cycle.cycle || cycle.cycle_number || (ctx.cycleIndex + 1);
    return cycle.cycle_label || `Cycle ${displayCycleNumber}`;
}

function _defaultIssueLifecycleStepActions(step, options) {
    const raw = _callTimelineOption(options, 'filterStepActions', step);
    const actions = Array.isArray(raw)
        ? raw
        : (Array.isArray(step && step.actions) ? step.actions : []);
    const filtered = _isIssueLifecycleValidationStep(step, options)
        ? actions.filter((action) => !action || action.type !== 'open_validation_failure')
        : actions;
    if (typeof renderTimelineEventActions !== 'function') return '';
    return renderTimelineEventActions(filtered);
}

function _isIssueLifecycleValidationStep(step, options) {
    const custom = _callTimelineOption(options, 'isValidationStep', step);
    if (typeof custom === 'boolean') return custom;
    if (!step || typeof step !== 'object') return false;
    const name = String(step.event || '').toLowerCase();
    return _ISSUE_LIFECYCLE_VALIDATION_EVENT_NAMES.has(name);
}

function _findIssueLifecycleCycleRunDir(cycle, options) {
    const custom = _callTimelineOption(options, 'findCycleRunDir', cycle);
    if (typeof custom === 'string') return custom;
    if (!cycle) return '';
    if (cycle.artifacts && typeof cycle.artifacts === 'object' && typeof cycle.artifacts.run_dir === 'string') {
        return cycle.artifacts.run_dir;
    }
    const phaseGroups = Array.isArray(cycle.phase_groups) ? cycle.phase_groups : [];
    for (const group of phaseGroups) {
        for (const step of (group.steps || [])) {
            if (step && typeof step.run_dir === 'string' && step.run_dir) return step.run_dir;
        }
    }
    for (const step of (cycle.steps || [])) {
        if (step && typeof step.run_dir === 'string' && step.run_dir) return step.run_dir;
    }
    return '';
}

function _issueLifecycleValidationToggleAttrs(stepId, bodyId, issueNumber, stepRunDir) {
    const baseAttrs = [
        `id="${escapeAttr(`${stepId}-toggle`)}"`,
        `aria-expanded="false"`,
        `aria-controls="${escapeAttr(bodyId)}"`,
    ];
    if (!Number.isInteger(issueNumber) || issueNumber <= 0) {
        return `${baseAttrs.join(' ')} disabled aria-disabled="true"`;
    }
    const toggleCall = `toggleValidationEventInline(${JSON.stringify(stepId)}, ${issueNumber}, ${JSON.stringify(stepRunDir)})`;
    return `${baseAttrs.join(' ')} onclick="${escapeAttr(toggleCall)}"`;
}

function _renderIssueLifecycleRunSummary(run, ctx, options) {
    const outcome = _readHierarchicalOutcomeBadge(run && run.outcome);
    const runLabel = _callTimelineOption(options, 'runLabel', run, ctx) || _defaultIssueLifecycleRunLabel(run, ctx);
    const icon = options && options.showToneIcon
        ? `<span class="cvv-ico cvv-ico-${outcome.tone}" aria-hidden="true">${_hierarchicalToneGlyph(outcome.tone)}</span>`
        : '';
    const meta = _callTimelineOption(options, 'runMeta', run, ctx) || '';
    return `
        ${icon}
        <span class="journey-cycle-label">${escapeHtml(runLabel)}</span>
        <span class="journey-cycle-outcome ${outcome.toneClass}">\u2014 ${escapeHtml(outcome.label)}</span>
        ${meta}
        <span class="journey-cycle-time">${escapeHtml(_formatHierarchicalHeaderTimestamp(run.timestamp || '', run.time_label || ''))}</span>`;
}

function _renderIssueLifecycleCycleSummary(cycle, ctx, options) {
    const cycleLabel = _callTimelineOption(options, 'cycleLabel', cycle, ctx) || _defaultIssueLifecycleCycleLabel(cycle, ctx);
    const outcome = _readHierarchicalOutcomeBadge(cycle && cycle.outcome);
    const agentPill = cycle && cycle.agent ? `<span class="journey-cycle-agent">(${escapeHtml(cycle.agent)})</span>` : '';
    const retryCount = Number(cycle && cycle.retry_count ? cycle.retry_count : 0);
    const retryInfo = retryCount > 0
        ? `<span class="journey-cycle-retries">${retryCount} ${retryCount === 1 ? 'retry' : 'retries'}</span>`
        : '';
    const icon = options && options.showToneIcon
        ? `<span class="cvv-ico cvv-ico-${outcome.tone}" aria-hidden="true">${_hierarchicalToneGlyph(outcome.tone)}</span>`
        : '';
    const validationBadge = _callTimelineOption(
        options,
        'renderCycleValidationBadge',
        cycle && cycle.validation,
        options && options.issueNumber,
        cycle,
        ctx,
    ) || '';
    const summaryExtras = _callTimelineOption(options, 'renderCycleSummaryExtras', cycle, ctx) || '';
    const trailing = _callTimelineOption(options, 'renderCycleTrailingActions', cycle, ctx) || '';
    return `
        ${icon}
        <span class="journey-cycle-label">${escapeHtml(cycleLabel)}</span>
        ${agentPill}
        ${retryInfo}
        <span class="journey-cycle-outcome ${outcome.toneClass}">\u2014 ${escapeHtml(outcome.label)}</span>
        ${validationBadge}
        ${summaryExtras}
        <span class="journey-cycle-time">${escapeHtml(_formatHierarchicalHeaderTimestamp(cycle.timestamp || '', cycle.time_label || ''))}</span>
        ${trailing}`;
}

function _renderIssueLifecycleStep(step, ctx, options) {
    const stepId = `${ctx.cycleId}-step-${ctx.stepIndex}`;
    const statusClass = step && step.status ? 'status-' + escapeAttr(step.status) : '';
    const detail = step && step.detail ? `<div class="journey-detail">${escapeHtml(step.detail)}</div>` : '';
    const actions = _defaultIssueLifecycleStepActions(step, options);
    const narrative = escapeHtml((step && (step.narrative || step.event)) || '');
    const timestamp = escapeHtml(_formatHierarchicalStepTimestamp((step && step.timestamp) || '', (step && step.time_label) || ''));

    if (_isIssueLifecycleValidationStep(step, options)) {
        const bodyId = `${stepId}-body`;
        const stepRunDir = String((step && step.run_dir) || ctx.cycleRunDir || '');
        const issueNumber = Number(options && options.issueNumber);
        const toggleAttrs = _issueLifecycleValidationToggleAttrs(stepId, bodyId, issueNumber, stepRunDir);
        const validationDetail = step && step.detail
            ? `<span class="journey-detail">${escapeHtml(step.detail)}</span>`
            : '';
        return `<div class="journey-step journey-step-validation ${statusClass}" id="${stepId}">
            <div class="journey-step-row">
                <button type="button" class="journey-step-inline-toggle" ${toggleAttrs}>
                    <span class="journey-step-caret" aria-hidden="true">▸</span>
                    <span class="journey-time">${timestamp}</span>
                    <span class="journey-main">
                        <span class="journey-summary-row">
                            <span class="journey-narrative">${narrative}</span>
                        </span>
                        ${validationDetail}
                    </span>
                </button>
                ${actions ? `<div class="journey-step-inline-actions">${actions}</div>` : ''}
            </div>
            <div class="journey-step-validation-body collapsed" id="${bodyId}" data-loaded="0" data-run-dir="${escapeAttr(stepRunDir)}" role="region" aria-labelledby="${escapeAttr(`${stepId}-toggle`)}" aria-hidden="true"></div>
        </div>`;
    }

    return `<div class="journey-step ${statusClass}">
        <span class="journey-time">${timestamp}</span>
        <div class="journey-main">
            <div class="journey-summary-row">
                <span class="journey-narrative">${narrative}</span>
                ${actions}
            </div>
            ${detail}
        </div>
    </div>`;
}

function _renderIssueLifecycleCycleBody(cycle, ctx, options) {
    const phaseGroups = Array.isArray(cycle && cycle.phase_groups) && cycle.phase_groups.length > 0
        ? cycle.phase_groups
        : [{ key: 'events', label: '', steps: (cycle && cycle.steps) || [] }];
    let html = '';
    let stepCounter = 0;
    for (const group of phaseGroups) {
        html += '<div class="journey-phase-group">';
        if (group.label) {
            html += `<div class="journey-phase-header">${escapeHtml(group.label)}</div>`;
        }
        const steps = Array.isArray(group.steps) ? group.steps : [];
        for (const step of steps) {
            html += _renderIssueLifecycleStep(step, {
                ...ctx,
                stepIndex: stepCounter++,
            }, options);
        }
        html += '</div>';
    }
    return html;
}

function renderIssueLifecycleTimeline(runs, options) {
    options = options || {};
    const rows = Array.isArray(runs) ? runs : [];
    if (rows.length === 0) {
        return options.emptyHtml || '<div class="timeline-empty">No activity recorded.</div>';
    }
    const baseId = String(options.baseId || 'issue-lifecycle');
    const nodes = [];
    for (let runIndex = 0; runIndex < rows.length; runIndex++) {
        const run = rows[runIndex] || {};
        const runId = `${baseId}-run-${runIndex}`;
        let runBodyHtml = '';
        const cycles = Array.isArray(run.cycles) ? run.cycles : [];
        for (let cycleIndex = 0; cycleIndex < cycles.length; cycleIndex++) {
            const cycle = cycles[cycleIndex] || {};
            const cycleId = `${baseId}-cycle-${runIndex}-${cycleIndex}`;
            const ctx = {
                run,
                cycle,
                runIndex,
                cycleIndex,
                runId,
                cycleId,
                cycleRunDir: _findIssueLifecycleCycleRunDir(cycle, options),
            };
            runBodyHtml += renderHierarchicalTimelineNode({
                id: cycleId,
                className: 'journey-cycle unified-timeline-node',
                summaryClassName: 'journey-cycle-header unified-timeline-summary',
                bodyClassName: 'journey-cycle-body',
                bodyId: `${cycleId}-body`,
                caretClassName: 'journey-cycle-toggle',
                open: _callTimelineOption(options, 'cycleExpanded', cycle, ctx) ?? Boolean(cycle.expanded),
                summaryHtml: _renderIssueLifecycleCycleSummary(cycle, ctx, options),
                bodyHtml: _renderIssueLifecycleCycleBody(cycle, ctx, options),
            });
        }
        const runCtx = { run, runIndex, runId, totalRuns: rows.length };
        nodes.push({
            id: runId,
            className: 'journey-run unified-timeline-node',
            summaryClassName: 'journey-cycle-header unified-timeline-summary',
            bodyClassName: 'journey-cycle-body',
            bodyId: `${runId}-body`,
            caretClassName: 'journey-cycle-toggle',
            open: _callTimelineOption(options, 'runExpanded', run, runCtx) ?? Boolean(run.expanded),
            summaryHtml: _renderIssueLifecycleRunSummary(run, runCtx, options),
            bodyHtml: runBodyHtml,
        });
    }
    const rendered = renderHierarchicalTimelineList(nodes);
    return options.listClassName
        ? `<div class="${escapeAttr(options.listClassName)}">${rendered}</div>`
        : rendered;
}

if (typeof window !== 'undefined') {
    window.renderHierarchicalTimelineNode = renderHierarchicalTimelineNode;
    window.renderHierarchicalTimelineList = renderHierarchicalTimelineList;
    window.renderIssueLifecycleTimeline = renderIssueLifecycleTimeline;
}
