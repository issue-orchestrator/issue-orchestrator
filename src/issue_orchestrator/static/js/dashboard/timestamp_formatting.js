const DASHBOARD_LOCAL_TIMESTAMP_OPTIONS = Object.freeze({
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    timeZoneName: 'short',
});
const DASHBOARD_LOCAL_TIMESTAMP_FORMATTER = new Intl.DateTimeFormat(
    undefined,
    DASHBOARD_LOCAL_TIMESTAMP_OPTIONS,
);

function dashboardTimestampDate(value) {
    if (value === null || value === undefined || value === '') return null;
    if (value instanceof Date) {
        return Number.isNaN(value.getTime()) ? null : value;
    }
    if (typeof value === 'number') {
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? null : date;
    }
    const raw = String(value).trim();
    if (!raw) return null;
    const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:/.test(raw)
        ? raw.replace(' ', 'T')
        : raw;
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? null : date;
}

function formatLocalTimestamp(timestamp, fallback = '') {
    const date = dashboardTimestampDate(timestamp);
    if (!date) return fallback || (timestamp ? String(timestamp) : '');
    return DASHBOARD_LOCAL_TIMESTAMP_FORMATTER.format(date);
}

function formatTimestamp(timestamp, fallback = '') {
    return formatLocalTimestamp(timestamp, fallback);
}

function formatJourneyHeaderTimestamp(timestamp, fallback = '') {
    return formatLocalTimestamp(timestamp, fallback);
}

function formatJourneyStepTimestamp(timestamp, fallback = '') {
    return formatLocalTimestamp(timestamp, fallback);
}

function formatDashboardTimestampElement(element) {
    if (!element) return;
    const raw = element.dataset
        ? element.dataset.dashboardTimestamp
        : element.getAttribute('data-dashboard-timestamp');
    const fallback = element.dataset
        ? (element.dataset.dashboardTimestampFallback || '')
        : (element.getAttribute('data-dashboard-timestamp-fallback') || '');
    const formatted = formatTimestamp(raw, fallback);
    element.textContent = formatted;
    if (formatted && typeof element.setAttribute === 'function') {
        element.setAttribute('title', formatted);
    }
}

function formatDashboardTimestamps(root) {
    const scope = root || (typeof document !== 'undefined' ? document : null);
    if (!scope) return;
    const nodes = [];
    if (typeof scope.matches === 'function' && scope.matches('[data-dashboard-timestamp]')) {
        nodes.push(scope);
    }
    if (typeof scope.querySelectorAll === 'function') {
        scope.querySelectorAll('[data-dashboard-timestamp]').forEach((node) => nodes.push(node));
    }
    nodes.forEach(formatDashboardTimestampElement);
}

if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => formatDashboardTimestamps(document));
    } else {
        formatDashboardTimestamps(document);
    }
}
