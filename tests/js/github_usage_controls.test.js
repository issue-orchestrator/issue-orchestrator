const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const controlsRefreshPath = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard/controls_refresh.js',
);

class FakeClassList {
    constructor(initial = []) {
        this.classes = new Set(initial);
    }

    add(value) {
        this.classes.add(value);
    }

    remove(value) {
        this.classes.delete(value);
    }

    contains(value) {
        return this.classes.has(value);
    }

    toggle(value, force) {
        if (force === true) {
            this.classes.add(value);
            return true;
        }
        if (force === false) {
            this.classes.delete(value);
            return false;
        }
        if (this.classes.has(value)) {
            this.classes.delete(value);
            return false;
        }
        this.classes.add(value);
        return true;
    }
}

class FakeElement {
    constructor(id, initialClasses = []) {
        this.id = id;
        this.style = { display: '' };
        this.classList = new FakeClassList(initialClasses);
        this.attributes = {};
        this.dataset = {};
        this.textContent = '';
        this.focusCalls = [];
        this.children = [];
        this.removed = false;
    }

    getAttribute(name) {
        return this.attributes[name] ?? null;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    focus(options) {
        this.focusCalls.push(options || null);
    }

    addEventListener() {}

    prepend(child) {
        this.children.unshift(child);
    }

    remove() {
        this.removed = true;
    }
}

function makeStorage(initial = null) {
    const values = new Map();
    if (initial !== null) {
        values.set('issue-orchestrator.github-usage.ui.v1', initial);
    }
    return {
        getItem(key) {
            return values.has(key) ? values.get(key) : null;
        },
        setItem(key, value) {
            values.set(key, String(value));
        },
    };
}

function loadControls({
    isEmbedded = false,
    storedPrefs = null,
    querySelectorAll = () => [],
    dashboardData = { paused: false, githubUsage: {} },
    fetch = async () => ({ ok: true, json: async () => ({}) }),
} = {}) {
    const ids = [
        'settingsMenu',
        'ghUsageWrap',
        'ghUsagePanel',
        'ghUsagePill',
        'ghUsageSummary',
        'ghUsageCallsPerMinute',
        'ghUsageTotalCalls',
        'ghUsageErrors',
        'ghUsageRateLimit',
        'ghUsageReset',
        'ghUsageWrapEmbedded',
        'ghUsagePanelEmbedded',
        'ghUsagePillEmbedded',
        'ghUsageSummaryEmbedded',
        'ghUsageCallsPerMinuteEmbedded',
        'ghUsageTotalCallsEmbedded',
        'ghUsageErrorsEmbedded',
        'ghUsageRateLimitEmbedded',
        'ghUsageResetEmbedded',
    ];
    const elements = Object.fromEntries(ids.map(id => [id, new FakeElement(id)]));
    elements.settingsMenu.classList.add('visible');
    const storage = makeStorage(storedPrefs);
    const context = {
        console,
        document: {
            getElementById(id) {
                return elements[id] || null;
            },
            createElement(tagName) {
                const el = new FakeElement(tagName);
                el.tagName = String(tagName).toUpperCase();
                return el;
            },
            querySelectorAll,
            querySelector() {
                return null;
            },
        },
        window: {
            dashboardData,
            clearInterval() {},
            setInterval() {},
        },
        localStorage: storage,
        GH_USAGE_UI_PREF_KEY: 'issue-orchestrator.github-usage.ui.v1',
        NETWORK_SYNC_OVERRIDE_KEY: 'issue-orchestrator.network-sync.override.v1',
        FLOW_REFRESH_OVERRIDE_KEY: 'issue-orchestrator.flow-refresh.override.v1',
        FLOW_FRESHNESS_PRESETS: {
            balanced: { enabled: true, staleSeconds: 900, cooldownSeconds: 120 },
        },
        FLOW_BUDGET_MULTIPLIER: { medium: 1 },
        issueRefreshInFlight: new Set(),
        issueRefreshLastAttempt: new Map(),
        flowRefreshObserver: null,
        networkSyncTimer: null,
        isEmbedded,
        hideSettingsMenu() {
            elements.settingsMenu.classList.remove('visible');
        },
        fetch,
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(controlsRefreshPath, 'utf8'), context);
    return { context, elements, storage };
}

test('showGitHubUsage opens both usage panels and closes the settings menu', () => {
    const { context, elements, storage } = loadControls({
        isEmbedded: true,
        storedPrefs: JSON.stringify({ hidden: true, expanded: false }),
    });

    context.showGitHubUsage();

    assert.deepEqual(
        JSON.parse(storage.getItem('issue-orchestrator.github-usage.ui.v1')),
        { hidden: false, expanded: true },
    );
    for (const id of ['ghUsageWrap', 'ghUsageWrapEmbedded']) {
        assert.equal(elements[id].style.display, '');
    }
    for (const id of ['ghUsagePanel', 'ghUsagePanelEmbedded']) {
        assert.equal(elements[id].classList.contains('visible'), true);
    }
    for (const id of ['ghUsagePill', 'ghUsagePillEmbedded']) {
        assert.equal(elements[id].getAttribute('aria-expanded'), 'true');
    }
    assert.equal(elements.settingsMenu.classList.contains('visible'), false);
    assert.equal(elements.ghUsagePillEmbedded.focusCalls.length, 1);
    assert.equal(elements.ghUsagePill.focusCalls.length, 0);
});

test('setGitHubUsageHidden hides and collapses every usage widget', () => {
    const { context, elements, storage } = loadControls({
        storedPrefs: JSON.stringify({ hidden: false, expanded: true }),
    });

    context.setGitHubUsageHidden(true);

    assert.deepEqual(
        JSON.parse(storage.getItem('issue-orchestrator.github-usage.ui.v1')),
        { hidden: true, expanded: false },
    );
    for (const id of ['ghUsageWrap', 'ghUsageWrapEmbedded']) {
        assert.equal(elements[id].style.display, 'none');
    }
    for (const id of ['ghUsagePanel', 'ghUsagePanelEmbedded']) {
        assert.equal(elements[id].classList.contains('visible'), false);
    }
    for (const id of ['ghUsagePill', 'ghUsagePillEmbedded']) {
        assert.equal(elements[id].getAttribute('aria-expanded'), 'false');
    }
});

test('renderGitHubUsage keeps standalone and embedded values in sync', () => {
    const { context, elements } = loadControls();
    context.window.dashboardData.githubUsage = {
        calls_per_minute: 3,
        total_calls: 1234,
        errors: 2,
        last_rate_limit_from_headers: {
            remaining: 40,
            limit: 100,
            used: 60,
            reset: 0,
            resource: 'core',
        },
    };

    context.renderGitHubUsage();

    for (const id of ['ghUsageSummary', 'ghUsageSummaryEmbedded']) {
        assert.equal(elements[id].textContent, '3/min');
    }
    for (const id of ['ghUsageTotalCalls', 'ghUsageTotalCallsEmbedded']) {
        assert.equal(elements[id].textContent, '1,234');
    }
    for (const id of ['ghUsageErrors', 'ghUsageErrorsEmbedded']) {
        assert.equal(elements[id].textContent, '2');
    }
    for (const id of ['ghUsageRateLimit', 'ghUsageRateLimitEmbedded']) {
        assert.equal(elements[id].textContent, '60 used \u00b7 40 left (core)');
    }
    for (const id of ['ghUsageReset', 'ghUsageResetEmbedded']) {
        assert.equal(elements[id].textContent, '-');
    }
});

test('updateIssueCardFreshness can keep raw stale fact while hiding stale badge', () => {
    const actionRow = new FakeElement('actions');
    const staleDot = new FakeElement('staleDot', ['stale-dot']);
    const card = new FakeElement('card');
    card.querySelector = (selector) => {
        if (selector === '.card-head-actions') return actionRow;
        if (selector === '.attention-actions') return null;
        if (selector === '.stale-dot') return staleDot;
        return null;
    };
    const { context } = loadControls({
        querySelectorAll: (selector) => selector.includes('.issue-card') ? [card] : [],
    });

    context.updateIssueCardFreshness(277, {
        is_stale: true,
        show_stale_badge: false,
        stale_reason: 'Older than 15m stale threshold',
    });

    assert.equal(card.dataset.stale, 'true');
    assert.equal(card.dataset.showStaleBadge, 'false');
    assert.equal(staleDot.removed, true);
});

test('updateIssueCardFreshness shows stale badge when payload requests it', () => {
    const actionRow = new FakeElement('actions');
    const card = new FakeElement('card');
    card.staleDot = null;
    card.querySelector = (selector) => {
        if (selector === '.card-head-actions') return actionRow;
        if (selector === '.attention-actions') return null;
        if (selector === '.stale-dot') return card.staleDot;
        return null;
    };
    actionRow.prepend = (child) => {
        card.staleDot = child;
        actionRow.children.unshift(child);
    };
    const { context } = loadControls({
        querySelectorAll: (selector) => selector.includes('.issue-card') ? [card] : [],
    });

    context.updateIssueCardFreshness(287, {
        is_stale: true,
        show_stale_badge: true,
        stale_reason: 'Needs refresh',
    });

    assert.equal(card.dataset.stale, 'true');
    assert.equal(card.dataset.showStaleBadge, 'true');
    assert.equal(card.staleDot.className, 'stale-dot');
    assert.equal(card.staleDot.title, 'Needs refresh');
    assert.equal(card.staleDot.getAttribute('aria-label'), 'Needs refresh');
});

test('maybeRefreshVisibleCard skips hidden stale badges', () => {
    let fetchCalls = 0;
    const card = new FakeElement('card');
    card.dataset.issue = '277';
    card.dataset.stale = 'true';
    card.dataset.showStaleBadge = 'false';
    const { context } = loadControls({
        dashboardData: {
            paused: false,
            githubUsage: {},
            refresh: { flowLazyEnabled: true },
        },
        fetch: async () => {
            fetchCalls += 1;
            return { ok: true, json: async () => ({}) };
        },
    });

    context.maybeRefreshVisibleCard(card);

    assert.equal(fetchCalls, 0);
});

test('maybeRefreshVisibleCard refreshes visible stale badges', async () => {
    let fetchCalls = 0;
    const card = new FakeElement('card');
    card.dataset.issue = '287';
    card.dataset.stale = 'true';
    card.dataset.showStaleBadge = 'true';
    const { context } = loadControls({
        dashboardData: {
            paused: false,
            githubUsage: {},
            refresh: { flowLazyEnabled: true },
        },
        fetch: async () => {
            fetchCalls += 1;
            return { ok: true, json: async () => ({}) };
        },
    });

    context.maybeRefreshVisibleCard(card);

    assert.equal(fetchCalls, 1);
    await new Promise(resolve => setImmediate(resolve));
});
