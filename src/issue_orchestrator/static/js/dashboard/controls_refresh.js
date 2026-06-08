let isPaused = window.dashboardData.paused;

function setPauseBadgeState(paused, text = null) {
    const desiredText = text || (paused ? 'Paused' : 'Running');
    const desiredClass = paused ? 'status-paused' : 'status-running';
    const stateClasses = ['status-paused', 'status-running', 'status-starting'];
    document.querySelectorAll('.status-badge').forEach(badge => {
        if (badge.textContent !== desiredText) {
            badge.textContent = desiredText;
        }
        const currentState = stateClasses.find((cls) => badge.classList.contains(cls)) || null;
        if (currentState !== desiredClass) {
            // Atomic single-write class swap; see comment in core.js
            // updateStatusBadgeFromViewModel for rationale.
            const others = Array.from(badge.classList).filter(
                (cls) => !stateClasses.includes(cls)
            );
            badge.className = [...others, desiredClass].join(' ');
        }
    });
    updatePauseMenuFromViewModel({ paused });
}

async function togglePause() {
    const menu = document.getElementById('settingsMenu');

    // Close the menu
    menu.classList.remove('show');

    if (isPaused) {
        // Resume
        setPauseBadgeState(false, 'Resuming...');

        const res = await fetch('/api/resume', { method: 'POST' });
        if (!res.ok) {
            setPauseBadgeState(true);
            const message = await readActionError(res);
            console.error('Resume failed:', message);
            showToast(`Resume failed: ${message}`, true);
        }
        await refreshViewModel({ reloadOnListChange: false });
    } else {
        // Pause
        setPauseBadgeState(true, 'Pausing...');

        const res = await fetch('/api/pause', { method: 'POST' });
        if (!res.ok) {
            setPauseBadgeState(false);
            const message = await readActionError(res);
            console.error('Pause failed:', message);
            showToast(`Pause failed: ${message}`, true);
        }
        await refreshViewModel({ reloadOnListChange: false });
    }
}

async function readActionError(response) {
    try {
        const body = await response.json();
        return body.error || body.detail || `HTTP ${response.status}`;
    } catch (_) {
        return `HTTP ${response.status}`;
    }
}
async function refreshFromGitHub() {
    hideSettingsMenu();
    try {
        const res = await fetch('/api/refresh', { method: 'POST' });
        if (res.ok) {
            const data = await res.json();
            if (data.refresh?.requested || data.refresh?.in_progress) {
                const statusText = document.getElementById('refreshStatusText');
                if (statusText) {
                    statusText.textContent = data.refresh?.in_progress ? 'Refreshing from GitHub...' : 'Refresh requested...';
                }
            }
            showToast('Refresh requested from GitHub');
            setTimeout(() => refreshViewModel({ reloadOnListChange: false }), 1200);
        } else {
            showToast('Refresh failed', true);
        }
    } catch (e) {
        console.error('Refresh failed:', e);
        showToast('Refresh failed', true);
    }
}

function deriveFlowConfig(strategy) {
    const normalizedMode = ['aggressive', 'balanced', 'economy'].includes(strategy.freshnessMode)
        ? strategy.freshnessMode
        : 'balanced';
    const normalizedBudget = ['low', 'medium', 'high'].includes(strategy.apiBudget)
        ? strategy.apiBudget
        : 'medium';
    const normalizedAttention = ['strict', 'normal'].includes(strategy.attentionPriority)
        ? strategy.attentionPriority
        : 'strict';
    const preset = FLOW_FRESHNESS_PRESETS[normalizedMode];
    const multiplier = FLOW_BUDGET_MULTIPLIER[normalizedBudget];
    const hasCustomStale = Number.isFinite(Number(strategy.flowStaleSeconds));
    const hasCustomCooldown = Number.isFinite(Number(strategy.flowCooldownSeconds));
    const staleBase = hasCustomStale ? Number(strategy.flowStaleSeconds) : preset.staleSeconds * multiplier;
    const cooldownBase = hasCustomCooldown ? Number(strategy.flowCooldownSeconds) : preset.cooldownSeconds * multiplier;
    return {
        enabled: strategy.flowLazyEnabled ?? Boolean(preset.enabled),
        staleSeconds: Math.max(60, Math.round(staleBase)),
        cooldownSeconds: Math.max(0, Math.round(cooldownBase)),
        freshnessMode: normalizedMode,
        apiBudget: normalizedBudget,
        attentionPriority: normalizedAttention,
    };
}

function serverRefreshStrategy() {
    const refresh = window.dashboardData?.refresh || {};
    return {
        flowLazyEnabled: Boolean(refresh.flowLazyEnabled),
        flowStaleSeconds: Number(refresh.flowStaleSeconds || 900),
        flowCooldownSeconds: Number(refresh.flowCooldownSeconds || 120),
        freshnessMode: String(refresh.freshnessMode || 'balanced'),
        apiBudget: String(refresh.apiBudget || 'medium'),
        attentionPriority: String(refresh.attentionPriority || 'strict'),
    };
}

function currentRefreshConfig() {
    const server = deriveFlowConfig(serverRefreshStrategy());
    const override = getFlowRefreshOverride();
    if (!override?.enabled) {
        return { ...server, source: 'yaml' };
    }
    const combined = deriveFlowConfig({
        flowLazyEnabled: override.flowLazyEnabled,
        flowStaleSeconds: Number(override.flowStaleSeconds),
        flowCooldownSeconds: Number(override.flowCooldownSeconds),
        freshnessMode: override.freshnessMode || server.freshnessMode,
        apiBudget: override.apiBudget || server.apiBudget,
        attentionPriority: override.attentionPriority || server.attentionPriority,
    });
    return { ...combined, source: 'override' };
}

function getFlowRefreshOverride() {
    const raw = localStorage.getItem(FLOW_REFRESH_OVERRIDE_KEY);
    if (!raw) return null;
    try {
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object') return null;
        return {
            enabled: Boolean(parsed.enabled),
            flowLazyEnabled: Boolean(parsed.flowLazyEnabled),
            flowStaleSeconds: Math.max(60, Number(parsed.flowStaleSeconds || 900)),
            flowCooldownSeconds: Math.max(0, Number(parsed.flowCooldownSeconds || 120)),
            freshnessMode: ['aggressive', 'balanced', 'economy'].includes(parsed.freshnessMode) ? parsed.freshnessMode : 'balanced',
            apiBudget: ['low', 'medium', 'high'].includes(parsed.apiBudget) ? parsed.apiBudget : 'medium',
            attentionPriority: ['strict', 'normal'].includes(parsed.attentionPriority) ? parsed.attentionPriority : 'strict',
        };
    } catch {
        return null;
    }
}

function getNetworkSyncOverride() {
    const raw = localStorage.getItem(NETWORK_SYNC_OVERRIDE_KEY);
    if (!raw) return null;
    try {
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object') return null;
        const seconds = Math.max(5, Number(parsed.seconds || 0));
        if (!Number.isFinite(seconds)) return null;
        return { enabled: Boolean(parsed.enabled), seconds };
    } catch {
        return null;
    }
}

function currentNetworkSyncCadence() {
    const server = Number(window.dashboardData?.refresh?.networkSyncSeconds || 60);
    const override = getNetworkSyncOverride();
    if (!override?.enabled) {
        return { seconds: Math.max(5, server), source: 'config' };
    }
    return { seconds: Math.max(5, Number(override.seconds || server)), source: 'override' };
}

async function requestNetworkSyncSilently() {
    try {
        await fetch('/api/refresh', { method: 'POST' });
    } catch (err) {
        console.error('Background network sync request failed:', err);
    }
}

function applyNetworkSyncScheduler() {
    if (networkSyncTimer) {
        window.clearInterval(networkSyncTimer);
        networkSyncTimer = null;
    }
    const cadence = currentNetworkSyncCadence();
    if (cadence.source !== 'override') {
        return;
    }
    networkSyncTimer = window.setInterval(() => {
        requestNetworkSyncSilently();
    }, cadence.seconds * 1000);
}

function formatResetLabel(resetEpochSeconds) {
    if (!resetEpochSeconds || !Number.isFinite(resetEpochSeconds)) return '-';
    const delta = Math.max(0, Math.floor(resetEpochSeconds - Date.now() / 1000));
    const mins = Math.floor(delta / 60);
    const secs = delta % 60;
    return `${mins}m ${secs}s`;
}

function getGitHubUsagePrefs() {
    const raw = localStorage.getItem(GH_USAGE_UI_PREF_KEY);
    if (!raw) {
        return { hidden: false, expanded: false };
    }
    try {
        const parsed = JSON.parse(raw);
        return {
            hidden: Boolean(parsed.hidden),
            expanded: Boolean(parsed.expanded),
        };
    } catch {
        return { hidden: false, expanded: false };
    }
}

function saveGitHubUsagePrefs(prefs) {
    localStorage.setItem(GH_USAGE_UI_PREF_KEY, JSON.stringify({
        hidden: Boolean(prefs.hidden),
        expanded: Boolean(prefs.expanded),
    }));
}

function getGitHubUsageWidgets() {
    return [
        {
            wrap: document.getElementById('ghUsageWrap'),
            panel: document.getElementById('ghUsagePanel'),
            pill: document.getElementById('ghUsagePill'),
        },
        {
            wrap: document.getElementById('ghUsageWrapEmbedded'),
            panel: document.getElementById('ghUsagePanelEmbedded'),
            pill: document.getElementById('ghUsagePillEmbedded'),
        },
    ].filter(widget => widget.wrap && widget.panel && widget.pill);
}

function applyGitHubUsagePrefs() {
    const prefs = getGitHubUsagePrefs();
    const widgets = getGitHubUsageWidgets();
    if (!widgets.length) return;
    const desiredDisplay = prefs.hidden ? 'none' : '';
    const desiredVisible = !prefs.hidden && prefs.expanded;
    const desiredExpandedAttr = desiredVisible ? 'true' : 'false';
    // Same-value writes still trigger style invalidation and (for class
    // toggles) MutationObserver. Guard each so this function — invoked on
    // every refresh — doesn't contribute to the periodic header flash.
    widgets.forEach(({ wrap, panel, pill }) => {
        if (wrap.style.display !== desiredDisplay) wrap.style.display = desiredDisplay;
        if (panel.classList.contains('visible') !== desiredVisible) {
            panel.classList.toggle('visible', desiredVisible);
        }
        if (pill.getAttribute('aria-expanded') !== desiredExpandedAttr) {
            pill.setAttribute('aria-expanded', desiredExpandedAttr);
        }
    });
}

function toggleGitHubUsagePanel() {
    const prefs = getGitHubUsagePrefs();
    prefs.hidden = false;
    prefs.expanded = !prefs.expanded;
    saveGitHubUsagePrefs(prefs);
    applyGitHubUsagePrefs();
}

function setGitHubUsageHidden(hidden) {
    const prefs = getGitHubUsagePrefs();
    prefs.hidden = Boolean(hidden);
    if (prefs.hidden) {
        prefs.expanded = false;
    }
    saveGitHubUsagePrefs(prefs);
    applyGitHubUsagePrefs();
}

function focusPreferredGitHubUsagePill() {
    const embedded = typeof isEmbedded !== 'undefined' && isEmbedded;
    const preferredId = embedded ? 'ghUsagePillEmbedded' : 'ghUsagePill';
    const fallbackId = embedded ? 'ghUsagePill' : 'ghUsagePillEmbedded';
    const pill = document.getElementById(preferredId) || document.getElementById(fallbackId);
    if (!pill || typeof pill.focus !== 'function') return;
    try {
        pill.focus({ preventScroll: true });
    } catch (_) {
        pill.focus();
    }
}

function showGitHubUsage() {
    const prefs = getGitHubUsagePrefs();
    prefs.hidden = false;
    prefs.expanded = true;
    saveGitHubUsagePrefs(prefs);
    applyGitHubUsagePrefs();
    hideSettingsMenu();
    focusPreferredGitHubUsagePill();
}

function renderGitHubUsage() {
    const usage = window.dashboardData?.githubUsage || {};
    const rate = usage.last_rate_limit_from_headers || {};
    const remaining = Number(rate.remaining);
    const limit = Number(rate.limit);
    const callsPerMinute = Number(usage.calls_per_minute || 0);
    const totalCalls = Number(usage.total_calls || 0);
    const errors = Number(usage.errors || 0);

    // Same-value textContent writes replace the underlying text node and
    // fire a childList MutationObserver event, which on every periodic
    // view-model refresh stacks 6 spans into a visible header flash.
    // Guard each write with a value check.
    const setText = (el, value) => {
        if (el && el.textContent !== value) el.textContent = value;
    };
    const setTextForIds = (ids, value) => {
        ids.forEach((id) => setText(document.getElementById(id), value));
    };

    setTextForIds(['ghUsageSummary', 'ghUsageSummaryEmbedded'], `${callsPerMinute}/min`);
    setTextForIds(['ghUsageCallsPerMinute', 'ghUsageCallsPerMinuteEmbedded'], callsPerMinute.toLocaleString());
    setTextForIds(['ghUsageTotalCalls', 'ghUsageTotalCallsEmbedded'], totalCalls.toLocaleString());
    setTextForIds(['ghUsageErrors', 'ghUsageErrorsEmbedded'], errors.toLocaleString());
    let limitText;
    if (Number.isFinite(remaining) && Number.isFinite(limit) && limit > 0) {
        const used = Number.isFinite(Number(rate.used)) ? Number(rate.used) : Math.max(0, limit - remaining);
        const resource = rate.resource ? ` (${String(rate.resource)})` : '';
        limitText = `${used.toLocaleString()} used · ${remaining.toLocaleString()} left${resource}`;
    } else {
        limitText = 'No rate header yet';
    }
    setTextForIds(['ghUsageRateLimit', 'ghUsageRateLimitEmbedded'], limitText);
    setTextForIds(['ghUsageReset', 'ghUsageResetEmbedded'], formatResetLabel(Number(rate.reset || 0)));
}

function updateRefreshStatusFromViewModel(vm) {
    const refresh = vm?.dashboard_data?.refresh;
    if (!refresh) return;
    window.dashboardData = window.dashboardData || {};
    window.dashboardData.refresh = refresh;

    // Guard each write so periodic view-model refreshes don't replace the
    // text node when the value is unchanged (every replace is a childList
    // mutation that compounds with sibling refreshes into a visible flash).
    const setText = (el, value) => {
        if (el && el.textContent !== value) el.textContent = value;
    };

    const statusText = document.getElementById('refreshStatusText');
    if (statusText) {
        let textValue;
        if (refresh.inProgress) {
            textValue = 'Refreshing from GitHub...';
        } else if (refresh.requested) {
            textValue = 'Refresh requested...';
        } else {
            textValue = `Last GitHub sync: ${refresh.lastRefreshLabel || 'unknown'}`;
        }
        setText(statusText, textValue);
    }
    const statusMeta = document.getElementById('refreshStatusMeta');
    if (statusMeta) {
        const cfg = currentRefreshConfig();
        const flowSource = cfg.source === 'override' ? 'override' : 'config';
        const network = currentNetworkSyncCadence();
        let metaValue;
        if (cfg.enabled) {
            metaValue = `· ${cfg.freshnessMode}/${cfg.apiBudget}/${cfg.attentionPriority} · stale>${cfg.staleSeconds}s (${flowSource}) · network ${network.seconds}s (${network.source})`;
        } else {
            metaValue = `· lazy visible refresh off (${flowSource}) · network ${network.seconds}s (${network.source})`;
        }
        setText(statusMeta, metaValue);
    }
}

function updateIssueCardFreshness(issueNumber, freshness) {
    const cards = document.querySelectorAll(
        `.issue-card[data-issue="${issueNumber}"], .attention-item[data-issue="${issueNumber}"], .history-item[data-issue="${issueNumber}"]`
    );
    cards.forEach((card) => {
        if (typeof freshness.last_refreshed_age_seconds === 'number') {
            card.dataset.lastRefreshAgeSeconds = String(freshness.last_refreshed_age_seconds);
        }
        const showStaleBadge = Boolean(freshness.show_stale_badge);
        card.dataset.stale = freshness.is_stale ? 'true' : 'false';
        card.dataset.showStaleBadge = showStaleBadge ? 'true' : 'false';
        const actionRow = card.querySelector('.card-head-actions') || card.querySelector('.attention-actions');
        let staleDot = card.querySelector('.stale-dot');
        if (showStaleBadge) {
            if (!staleDot && actionRow) {
                staleDot = document.createElement('span');
                staleDot.className = 'stale-dot';
                actionRow.prepend(staleDot);
            }
            if (staleDot && freshness.stale_reason) {
                staleDot.title = freshness.stale_reason;
                staleDot.setAttribute('aria-label', freshness.stale_reason);
            }
        } else if (staleDot) {
            staleDot.remove();
        }
    });
}

async function refreshIssueCard(issueNumber, triggerEl = null, options = {}) {
    const now = Date.now();
    const cfg = currentRefreshConfig();
    const cooldownMs = Math.max(0, cfg.cooldownSeconds) * 1000;
    const lastAttempt = issueRefreshLastAttempt.get(issueNumber) || 0;
    if (!options.force && now - lastAttempt < cooldownMs) {
        return;
    }
    if (issueRefreshInFlight.has(issueNumber)) {
        return;
    }

    issueRefreshLastAttempt.set(issueNumber, now);
    issueRefreshInFlight.add(issueNumber);
    if (triggerEl) {
        triggerEl.disabled = true;
    }

    try {
        const res = await fetch(`/api/issues/${issueNumber}/refresh`, { method: 'POST' });
        const data = await res.json();
        if (!res.ok) {
            if (!options.silent) {
                showToast(data.error || `Refresh failed for #${issueNumber}`, true);
            }
            return;
        }
        updateIssueCardFreshness(issueNumber, {
            is_stale: false,
            show_stale_badge: false,
            stale_reason: '',
            last_refreshed_age_seconds: 0,
            last_refreshed_label: 'just now',
        });
        if (!options.silent) {
            showToast(`Refreshed issue #${issueNumber}`);
        }
    } catch (error) {
        if (!options.silent) {
            showToast(`Refresh failed for #${issueNumber}`, true);
        }
    } finally {
        issueRefreshInFlight.delete(issueNumber);
        if (triggerEl) {
            triggerEl.disabled = false;
        }
    }
}

function maybeRefreshVisibleCard(card) {
    const issueNumber = Number(card.dataset.issue);
    if (!Number.isInteger(issueNumber)) {
        return;
    }
    if (card.dataset.stale !== 'true') {
        return;
    }
    if (card.dataset.showStaleBadge === 'false') {
        return;
    }
    const cfg = currentRefreshConfig();
    if (!cfg.enabled) {
        return;
    }
    refreshIssueCard(issueNumber, null, { silent: true });
}

function initFlowLazyVisibleRefresh() {
    if (flowRefreshObserver) {
        flowRefreshObserver.disconnect();
        flowRefreshObserver = null;
    }
    const cfg = currentRefreshConfig();
    if (!cfg.enabled) return;
    const selectors = ['#panel-flow .issue-card[data-issue]'];
    if (cfg.attentionPriority === 'strict') {
        selectors.push('#panel-attention .attention-item[data-issue]');
    }
    const cards = document.querySelectorAll(selectors.join(', '));
    if (!cards.length) return;
    flowRefreshObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
            if (entry.isIntersecting && entry.intersectionRatio >= 0.4) {
                maybeRefreshVisibleCard(entry.target);
            }
        }
    }, { threshold: [0.4] });
    cards.forEach((card) => flowRefreshObserver.observe(card));
}

function setFlowRefreshInputsEnabled(enabled) {
    const ids = [
        'flowRefreshEnabled',
        'flowRefreshStaleSeconds',
        'flowRefreshCooldownSeconds',
        'flowFreshnessMode',
        'flowApiBudget',
        'flowAttentionPriority',
    ];
    ids.forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.disabled = !enabled;
    });
}

function setNetworkSyncInputEnabled(enabled) {
    const overrideEl = document.getElementById('networkSyncOverrideSeconds');
    if (overrideEl) {
        overrideEl.disabled = !enabled;
    }
}

function setFullScanInputEnabled(enabled) {
    const fullScanEl = document.getElementById('fullScanConfigSeconds');
    if (fullScanEl) {
        fullScanEl.disabled = !enabled;
    }
}

function updateCadencePreferenceInputState() {
    const cadenceSourceEl = document.getElementById('networkSyncCadenceSource');
    const saveCadenceEl = document.getElementById('saveCadenceToConfig');
    if (!cadenceSourceEl || !saveCadenceEl) return;
    const saveToConfig = Boolean(saveCadenceEl.checked);
    setNetworkSyncInputEnabled(saveToConfig || cadenceSourceEl.value === 'override');
    setFullScanInputEnabled(saveToConfig);
}

async function saveCadenceSettingsToConfig(networkSyncSeconds, fullScanIntervalSeconds) {
    const response = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            concurrency: {
                fetch_layer_network_sync_seconds: networkSyncSeconds,
                fetch_layer_full_scan_interval_seconds: fullScanIntervalSeconds,
            },
        }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
        const detail = typeof result.error === 'string' ? result.error : 'Failed to save cadence settings';
        throw new Error(detail);
    }
    return result;
}

function openSettingsForRefreshPrefs() {
    closeFlowRefreshPrefs();
    window.location.href = '/settings';
}

function openFlowRefreshPrefs() {
    hideSettingsMenu();
    const override = getFlowRefreshOverride();
    const cadence = currentNetworkSyncCadence();
    const refresh = window.dashboardData?.refresh || {};
    const cadenceSourceEl = document.getElementById('networkSyncCadenceSource');
    const cadenceOverrideEl = document.getElementById('networkSyncOverrideSeconds');
    const fullScanConfigEl = document.getElementById('fullScanConfigSeconds');
    const saveCadenceToConfigEl = document.getElementById('saveCadenceToConfig');
    const overrideEnabledEl = document.getElementById('flowRefreshOverrideEnabled');
    const enabledEl = document.getElementById('flowRefreshEnabled');
    const staleEl = document.getElementById('flowRefreshStaleSeconds');
    const cooldownEl = document.getElementById('flowRefreshCooldownSeconds');
    const freshnessModeEl = document.getElementById('flowFreshnessMode');
    const apiBudgetEl = document.getElementById('flowApiBudget');
    const attentionPriorityEl = document.getElementById('flowAttentionPriority');
    if (!cadenceSourceEl || !cadenceOverrideEl || !fullScanConfigEl || !saveCadenceToConfigEl || !overrideEnabledEl || !enabledEl || !staleEl || !cooldownEl || !freshnessModeEl || !apiBudgetEl || !attentionPriorityEl || !flowRefreshPrefsModal) return;

    const useOverride = Boolean(override?.enabled);
    const base = currentRefreshConfig();
    cadenceSourceEl.value = cadence.source;
    cadenceOverrideEl.value = String(cadence.seconds);
    fullScanConfigEl.value = String(Number(refresh.fullScanIntervalSeconds || 1800));
    saveCadenceToConfigEl.checked = false;
    cadenceSourceEl.onchange = () => updateCadencePreferenceInputState();
    saveCadenceToConfigEl.onchange = () => updateCadencePreferenceInputState();
    updateCadencePreferenceInputState();
    overrideEnabledEl.checked = useOverride;
    const server = serverRefreshStrategy();
    enabledEl.checked = useOverride ? Boolean(override.flowLazyEnabled) : Boolean(server.flowLazyEnabled);
    staleEl.value = String(useOverride ? Number(override.flowStaleSeconds) : Number(base.staleSeconds || 900));
    cooldownEl.value = String(useOverride ? Number(override.flowCooldownSeconds) : Number(base.cooldownSeconds || 120));
    freshnessModeEl.value = useOverride ? String(override.freshnessMode || 'balanced') : String(server.freshnessMode || 'balanced');
    apiBudgetEl.value = useOverride ? String(override.apiBudget || 'medium') : String(server.apiBudget || 'medium');
    attentionPriorityEl.value = useOverride ? String(override.attentionPriority || 'strict') : String(server.attentionPriority || 'strict');
    setFlowRefreshInputsEnabled(useOverride);
    flowRefreshPrefsModal.classList.add('visible');
}

function closeFlowRefreshPrefs(e) {
    if (!e || e.target === flowRefreshPrefsModal) {
        flowRefreshPrefsModal?.classList.remove('visible');
    }
}

async function saveFlowRefreshPrefs() {
    const cadenceSourceEl = document.getElementById('networkSyncCadenceSource');
    const cadenceOverrideEl = document.getElementById('networkSyncOverrideSeconds');
    const fullScanConfigEl = document.getElementById('fullScanConfigSeconds');
    const saveCadenceToConfigEl = document.getElementById('saveCadenceToConfig');
    const overrideEnabledEl = document.getElementById('flowRefreshOverrideEnabled');
    const enabledEl = document.getElementById('flowRefreshEnabled');
    const staleEl = document.getElementById('flowRefreshStaleSeconds');
    const cooldownEl = document.getElementById('flowRefreshCooldownSeconds');
    const freshnessModeEl = document.getElementById('flowFreshnessMode');
    const apiBudgetEl = document.getElementById('flowApiBudget');
    const attentionPriorityEl = document.getElementById('flowAttentionPriority');
    if (!cadenceSourceEl || !cadenceOverrideEl || !fullScanConfigEl || !saveCadenceToConfigEl || !overrideEnabledEl || !enabledEl || !staleEl || !cooldownEl || !freshnessModeEl || !apiBudgetEl || !attentionPriorityEl) return;

    const networkSyncSeconds = Math.max(5, Number(cadenceOverrideEl.value || 60));
    const fullScanIntervalSeconds = Math.max(60, Number(fullScanConfigEl.value || 1800));
    const saveCadenceToConfig = Boolean(saveCadenceToConfigEl.checked);

    if (saveCadenceToConfig) {
        try {
            const result = await saveCadenceSettingsToConfig(networkSyncSeconds, fullScanIntervalSeconds);
            localStorage.removeItem(NETWORK_SYNC_OVERRIDE_KEY);
            if (window.dashboardData?.refresh) {
                window.dashboardData.refresh.networkSyncSeconds = networkSyncSeconds;
                window.dashboardData.refresh.fullScanIntervalSeconds = fullScanIntervalSeconds;
            }
            if (viewModel?.dashboard_data?.refresh) {
                viewModel.dashboard_data.refresh.networkSyncSeconds = networkSyncSeconds;
                viewModel.dashboard_data.refresh.fullScanIntervalSeconds = fullScanIntervalSeconds;
            }
            if (result && result.restart_required) {
                showToast('Cadence settings saved; restart required for full effect', 'warning');
            }
        } catch (err) {
            showToast(`Failed to save cadence settings: ${err.message}`, 'error');
            return;
        }
    } else if (cadenceSourceEl.value === 'override') {
        localStorage.setItem(NETWORK_SYNC_OVERRIDE_KEY, JSON.stringify({ enabled: true, seconds: networkSyncSeconds }));
    } else {
        localStorage.removeItem(NETWORK_SYNC_OVERRIDE_KEY);
    }

    if (!overrideEnabledEl.checked) {
        localStorage.removeItem(FLOW_REFRESH_OVERRIDE_KEY);
        updateRefreshStatusFromViewModel(viewModel);
        applyNetworkSyncScheduler();
        initFlowLazyVisibleRefresh();
        closeFlowRefreshPrefs();
        showToast('Refresh preferences saved');
        return;
    }

    const staleSeconds = Math.max(60, Number(staleEl.value || 900));
    const cooldownSeconds = Math.max(0, Number(cooldownEl.value || 120));
    const payload = {
        enabled: true,
        flowLazyEnabled: Boolean(enabledEl.checked),
        flowStaleSeconds: staleSeconds,
        flowCooldownSeconds: cooldownSeconds,
        freshnessMode: ['aggressive', 'balanced', 'economy'].includes(freshnessModeEl.value) ? freshnessModeEl.value : 'balanced',
        apiBudget: ['low', 'medium', 'high'].includes(apiBudgetEl.value) ? apiBudgetEl.value : 'medium',
        attentionPriority: ['strict', 'normal'].includes(attentionPriorityEl.value) ? attentionPriorityEl.value : 'strict',
    };
    localStorage.setItem(FLOW_REFRESH_OVERRIDE_KEY, JSON.stringify(payload));
    updateRefreshStatusFromViewModel(viewModel);
    applyNetworkSyncScheduler();
    initFlowLazyVisibleRefresh();
    closeFlowRefreshPrefs();
    showToast('Refresh preferences saved');
}

function resetFlowRefreshPrefs() {
    localStorage.removeItem(NETWORK_SYNC_OVERRIDE_KEY);
    localStorage.removeItem(FLOW_REFRESH_OVERRIDE_KEY);
    updateRefreshStatusFromViewModel(viewModel);
    applyNetworkSyncScheduler();
    initFlowLazyVisibleRefresh();
    closeFlowRefreshPrefs();
    showToast('Flow refresh preferences reset');
}
// Shutdown state - used to cancel polling when "Shutdown now" is clicked
let shutdownInProgress = false;

async function shutdown() {
    // First, check if there are active sessions
    const statusRes = await fetch('/api/status');
    const status = await statusRes.json();
    const activeSessions = status.active_sessions || [];

    if (activeSessions.length > 0) {
        // Show modal with options — make user decide about active work
        showShutdownModal(activeSessions);
    } else {
        // Nothing running — close immediately, no confirmation needed
        showToast('Shutting down...');
        await executeShutdown();
    }
}

function showShutdownModal(activeSessions) {
    const sessionList = activeSessions.map(s => `<li>#${s.issue_number}: ${escapeHtml(s.title || 'Untitled')}</li>`).join('');

    const modal = document.createElement('div');
    modal.id = 'shutdownModal';
    modal.className = 'modal-overlay visible';
    modal.innerHTML = `
        <div class="modal-content" style="max-width: 500px;">
            <div class="modal-header">
                <h2>Shutdown Orchestrator</h2>
                <button class="modal-close" onclick="closeShutdownModal()">&times;</button>
            </div>
            <div class="modal-body" id="shutdownModalBody">
                <p><strong>${activeSessions.length} agent(s) currently working:</strong></p>
                <ul style="margin: 12px 0; padding-left: 20px; color: var(--text);">${sessionList}</ul>
                <p style="color: var(--text-muted); font-size: 0.9em;">
                    "Wait" will stop new work and shutdown when agents finish.<br>
                    "Shutdown now" will interrupt agents immediately.
                </p>
            </div>
            <div class="modal-footer" id="shutdownModalFooter">
                <button class="btn-secondary" onclick="closeShutdownModal()">Cancel</button>
                <button class="btn-secondary" onclick="shutdownWait()">Wait for completion</button>
                <button class="btn-primary" onclick="shutdownNow()">Shutdown now</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

function closeShutdownModal() {
    const modal = document.getElementById('shutdownModal');
    if (modal) modal.remove();
}

async function shutdownWait() {
    // Set shutdown flag (stops new work). The /api/shutdown endpoint
    // now requires a non-empty 'reason' and can return 400/401/503;
    // we MUST check response.ok before switching the modal to the
    // waiting/polling state, otherwise the operator sees "waiting for
    // sessions to complete" while the shutdown request was actually
    // rejected and no shutdown is in progress.
    let response;
    try {
        response = await fetch('/api/shutdown', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                reason: 'dashboard: user clicked "Shutdown and Wait" in Engine controls',
                actor: 'dashboard.shutdown_wait',
            }),
        });
    } catch (networkErr) {
        showToast(`Shutdown request failed: ${networkErr.message || networkErr}`, 'error');
        return;
    }

    if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try {
            const payload = await response.json();
            detail = payload?.error || payload?.detail || detail;
        } catch (_) {
            // Best-effort; fall through with the HTTP status.
        }
        showToast(`Shutdown rejected: ${detail}`, 'error');
        return;
    }

    // Update modal to show waiting state
    const body = document.getElementById('shutdownModalBody');
    const footer = document.getElementById('shutdownModalFooter');

    body.innerHTML = `
        <div style="text-align: center; padding: 20px;">
            <div class="loading-spinner" style="margin: 0 auto 16px;"></div>
            <p id="shutdownWaitStatus">Waiting for sessions to complete...</p>
            <p style="color: var(--text-muted); font-size: 0.9em; margin-top: 12px;">
                No new work will be started. Shutdown will happen automatically when all agents finish.
            </p>
        </div>
    `;
    footer.innerHTML = `
        <button class="btn-primary" onclick="shutdownNow()">Shutdown now</button>
    `;

    // Poll for session completion
    pollForShutdown();
}

async function pollForShutdown() {
    const statusEl = document.getElementById('shutdownWaitStatus');

    const poll = async () => {
        // Check if shutdown was triggered manually
        if (shutdownInProgress) {
            return;
        }

        try {
            const res = await fetch('/api/status');
            const status = await res.json();
            const activeSessions = status.active_sessions || [];

            if (activeSessions.length === 0) {
                if (statusEl) statusEl.textContent = 'All sessions complete. Shutting down...';
                shutdownInProgress = true;
                await executeShutdown();
                return;
            }

            if (statusEl) {
                statusEl.textContent = `Waiting for ${activeSessions.length} session(s) to complete...`;
            }

            // Poll again in 3 seconds (only if not shutting down)
            if (!shutdownInProgress) {
                setTimeout(poll, 3000);
            }
        } catch (err) {
            // Server might already be down
            if (statusEl) statusEl.textContent = 'Server connection lost.';
        }
    };

    poll();
}

async function shutdownNow() {
    shutdownInProgress = true;  // Cancel any polling
    closeShutdownModal();
    await executeShutdown();
}

async function executeShutdown() {
    try {
        await fetch('/control/shutdown', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ stop_orchestrators: true })
        });
    } catch (err) {
        // Expected - server dies before responding
    }
    document.body.innerHTML = '<div style="display:flex;justify-content:center;align-items:center;height:100vh;color:var(--text-muted);flex-direction:column;gap:12px;"><span>Orchestrator stopped.</span><span style="font-size:0.9em;">You can close this tab.</span></div>';
}

// Tab switching
function switchTab(tab) {
    document.body.classList.add('tab-nav-pending');
    document.querySelectorAll('.dashboard-tabs .tab').forEach((btn) => {
        btn.classList.remove('is-loading');
        btn.removeAttribute('aria-busy');
    });
    const targetTab = document.querySelector(`.dashboard-tabs .tab[data-tab="${cssEscape(tab)}"]`);
    if (targetTab) {
        targetTab.classList.add('is-loading');
        targetTab.setAttribute('aria-busy', 'true');
    }
    const url = new URL(window.location.href);
    url.searchParams.set('tab', tab);
    url.searchParams.set('page', '1');  // Reset to page 1 when switching tabs
    window.location.href = url.toString();
}

// Keyboard navigation for tabs (accessibility)
const tabButtons = Array.from(document.querySelectorAll('.dashboard-tabs .tab'));
const tabOrder = tabButtons
    .map((btn) => btn.dataset.tab)
    .filter((tabName) => typeof tabName === 'string' && tabName.length > 0);
tabButtons.forEach(tabBtn => {
    tabBtn.addEventListener('keydown', (e) => {
        const currentTab = tabBtn.dataset.tab || tabBtn.id.replace('tab-', '');
        const currentIndex = tabOrder.indexOf(currentTab);
        let newIndex = currentIndex;

        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
            e.preventDefault();
            newIndex = (currentIndex + 1) % tabOrder.length;
        } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
            e.preventDefault();
            newIndex = (currentIndex - 1 + tabOrder.length) % tabOrder.length;
        } else if (e.key === 'Home') {
            e.preventDefault();
            newIndex = 0;
        } else if (e.key === 'End') {
            e.preventDefault();
            newIndex = tabOrder.length - 1;
        }

        if (newIndex !== currentIndex) {
            const newTabBtn = document.querySelector(`.dashboard-tabs .tab[data-tab="${tabOrder[newIndex]}"]`);
            if (newTabBtn) {
                newTabBtn.focus();
            }
        }

        // Enter or Space activates the tab
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            switchTab(currentTab);
        }
    });
});

// ── Blocked triage: viewed state (localStorage) ──
