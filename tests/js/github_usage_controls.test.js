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
        this.textContent = '';
        this.focusCalls = [];
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

function loadControls({ isEmbedded = false, storedPrefs = null } = {}) {
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
            querySelectorAll() {
                return [];
            },
        },
        window: {
            dashboardData: { paused: false, githubUsage: {} },
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
        networkSyncTimer: null,
        isEmbedded,
        hideSettingsMenu() {
            elements.settingsMenu.classList.remove('visible');
        },
        fetch: async () => ({ ok: true, json: async () => ({}) }),
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
