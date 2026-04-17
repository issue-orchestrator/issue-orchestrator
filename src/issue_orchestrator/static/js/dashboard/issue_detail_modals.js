let blockedIssuesData = [];
const blockedModal = document.getElementById('blockedModal');
const blockedList = document.getElementById('blockedList');
const blockedSelectAll = document.getElementById('blockedSelectAll');
const blockedSelectAllLabel = document.getElementById('blockedSelectAllLabel');
const blockedWarning = document.getElementById('blockedWarning');
const blockedWarningText = document.getElementById('blockedWarningText');
const blockedUnblockBtn = document.getElementById('blockedUnblockBtn');
const blockedResetBtn = document.getElementById('blockedResetBtn');

async function openBlockedModal() {
    // Fetch blocked issues
    try {
        const res = await fetch('/api/dialog/blocked-issues');
        const data = await res.json();
        blockedIssuesData = data.blocked_issues || [];
    } catch (err) {
        console.error('Failed to fetch blocked issues:', err);
        blockedIssuesData = [];
    }

    renderBlockedList();
    blockedModal.classList.add('visible');
}

function closeBlockedModal(e) {
    if (!e || e.target === blockedModal) {
        blockedModal.classList.remove('visible');
    }
}

// Phase Info Modal
const phaseModal = document.getElementById('phaseModal');
let currentPhaseData = null;
let currentPhaseIssue = null;

async function openPhaseModal(issueNumber, flowStepKey) {
    currentPhaseIssue = issueNumber;
    try {
        const res = await fetch(`/api/dialog/phase/${issueNumber}?phase=${encodeURIComponent(flowStepKey)}`);
        const data = await res.json();

        if (data.error) {
            console.error('Failed to fetch phases:', data.error);
            return;
        }

        const phase = data.phase;

        if (!phase) {
            // No phases yet, show a simple message
            document.getElementById('phaseModalTitle').textContent = flowStepKey.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
            document.getElementById('phaseStatusIcon').textContent = '○';
            document.getElementById('phaseStatusIcon').className = 'phase-status-icon';
            document.getElementById('phaseStatusLabel').textContent = 'Not started';
            document.getElementById('phaseDuration').textContent = '-';
            document.getElementById('phaseAgent').textContent = '-';
            document.getElementById('phaseValidationRow').style.display = 'none';
            document.getElementById('phaseDetailsBtn').style.display = 'none';
            phaseModal.classList.add('visible');
            return;
        }

        currentPhaseData = phase;

        // Update modal content
        document.getElementById('phaseModalTitle').textContent = phase.display_name;

        const iconEl = document.getElementById('phaseStatusIcon');
        const labelEl = document.getElementById('phaseStatusLabel');

        iconEl.textContent = phase.status_icon;
        iconEl.className = 'phase-status-icon ' + getStatusClass(phase.status);
        labelEl.textContent = formatStatus(phase.status);

        // Duration
        const duration = calculateDuration(phase.started_at, phase.ended_at);
        document.getElementById('phaseDuration').textContent = duration || '-';

        // Agent
        document.getElementById('phaseAgent').textContent = phase.agent_label || '-';

        // Validation
        const validationRow = document.getElementById('phaseValidationRow');
        if (phase.validation_passed !== null && phase.validation_passed !== undefined) {
            validationRow.style.display = 'flex';
            document.getElementById('phaseValidation').textContent =
                phase.validation_passed ? 'Passed' : 'Failed';
            document.getElementById('phaseValidation').style.color =
                phase.validation_passed ? 'var(--ok)' : 'var(--danger)';
        } else {
            validationRow.style.display = 'none';
        }

        // Show Details button
        document.getElementById('phaseDetailsBtn').style.display = 'block';

        phaseModal.classList.add('visible');
    } catch (err) {
        console.error('Error fetching phase data:', err);
    }
}

function closePhaseModal(e) {
    if (!e || e.target === phaseModal) {
        phaseModal.classList.remove('visible');
        currentPhaseData = null;
    }
}

const timelineModal = document.getElementById('timelineModal');
const issueDetailDrawer = document.getElementById('issueDetailDrawer');
let issueDetailData = null;
let lastIssueDetailTrigger = null;
let journeyFilter = 'latest-run'; // 'latest-run' or 'all'
let timelineView = 'user'; // 'user', 'ops', or 'debug'

async function openTimelineModal(issueNumber) {
    if (!timelineModal) return;
    timelineModal.classList.add('visible');
    document.getElementById('timelineModalTitle').textContent = `Timeline #${issueNumber}`;
    const content = document.getElementById('timelineModalContent');
    content.innerHTML = '<div class="timeline-loading">Loading timeline...</div>';

    try {
        const res = await fetch(`/api/timeline/${issueNumber}`);
        if (!res.ok) {
            content.innerHTML = '<div class="timeline-empty">No timeline data found.</div>';
            return;
        }
        const data = await res.json();
        renderTimeline(content, data.events || [], data.phase_toc || [], data.cycles || []);
    } catch (err) {
        console.error('Failed to load timeline:', err);
        content.innerHTML = '<div class="timeline-empty">Failed to load timeline.</div>';
    }
}

function closeTimelineModal(e) {
    if (!e || e.target === timelineModal) {
        timelineModal.classList.remove('visible');
    }
}

