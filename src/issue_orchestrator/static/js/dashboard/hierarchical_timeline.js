// Shared renderer for hierarchical timeline rows.
//
// Dashboard issue timelines, plugin timelines, and E2E run history all use
// native ``<details>/<summary>`` disclosure rows.  This module owns only that
// generic shell plus a small host-capability registry.  Orchestrator-specific
// run/cycle/event rendering lives in ``plugins/agent_context.js``.

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

function readHierarchicalOutcomeBadge(outcome, fallbackLabel = 'In progress') {
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
    return { label: label || fallbackLabel, tone: normalizedTone, toneClass };
}

function hierarchicalToneGlyph(tone, options = {}) {
    if (tone === 'failed') return '✕';
    if (tone === 'error') return '⚠';
    if (tone === 'in_progress') return options.inProgress || '…';
    if (tone === 'neutral') return '·';
    return '✓';
}

const _hierarchicalTimelineHostCapabilities = Object.create(null);

function registerHierarchicalTimelineHostCapability(name, resolver) {
    if (typeof name !== 'string' || !name) {
        throw new Error('registerHierarchicalTimelineHostCapability: name must be a non-empty string');
    }
    if (typeof resolver !== 'function') {
        throw new Error(`registerHierarchicalTimelineHostCapability: resolver for ${name} must be a function`);
    }
    _hierarchicalTimelineHostCapabilities[name] = resolver;
}

function registerHierarchicalTimelineHostCapabilities(capabilities) {
    if (!capabilities || typeof capabilities !== 'object') return;
    for (const [name, resolver] of Object.entries(capabilities)) {
        registerHierarchicalTimelineHostCapability(name, resolver);
    }
}

function getHierarchicalTimelineHostCapability(name) {
    const resolver = _hierarchicalTimelineHostCapabilities[name];
    return typeof resolver === 'function' ? resolver() : null;
}

function runHierarchicalTimelineHostCapability(name, ...args) {
    const capability = getHierarchicalTimelineHostCapability(name);
    return typeof capability === 'function' ? capability(...args) : null;
}

function _resetHierarchicalTimelineHostCapabilitiesForTests() {
    for (const key of Object.keys(_hierarchicalTimelineHostCapabilities)) {
        delete _hierarchicalTimelineHostCapabilities[key];
    }
}

if (typeof window !== 'undefined') {
    window.renderHierarchicalTimelineNode = renderHierarchicalTimelineNode;
    window.renderHierarchicalTimelineList = renderHierarchicalTimelineList;
    window.readHierarchicalOutcomeBadge = readHierarchicalOutcomeBadge;
    window.hierarchicalToneGlyph = hierarchicalToneGlyph;
    window.registerHierarchicalTimelineHostCapability = registerHierarchicalTimelineHostCapability;
    window.registerHierarchicalTimelineHostCapabilities = registerHierarchicalTimelineHostCapabilities;
    window.getHierarchicalTimelineHostCapability = getHierarchicalTimelineHostCapability;
    window.runHierarchicalTimelineHostCapability = runHierarchicalTimelineHostCapability;
    window._resetHierarchicalTimelineHostCapabilitiesForTests = _resetHierarchicalTimelineHostCapabilitiesForTests;
}
