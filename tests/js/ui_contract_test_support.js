// Shared JS-vm test support for the browser contract layer (issue #6337).
//
// Not a test file (no ``.test.js`` suffix), so the auto-discovering
// runner in tests/unit/test_dashboard_ui_guardrails.py skips it.
//
// Deliberately exposes the REAL ``ui_contract_json.js`` + generated
// ``ui-contracts.validators.js`` rather than a stub: dashboard modules
// under test should be held to the same generated contract the browser
// enforces.  A stubbed "always valid" reader would let a payload that
// production rejects sail through the suite.

const uiContractJson = require('../../src/issue_orchestrator/static/js/ui_contract_json.js');

// Redirects contract diagnostics into the returned array for the
// duration of a test, instead of the default console/toast reporter.
// Call from any test that feeds a module a deliberately invalid payload:
// it both keeps the run quiet and lets the test assert on the rejection.
function captureContractViolations() {
    const violations = [];
    uiContractJson.setViolationReporter((violation) => violations.push(violation));
    return violations;
}

function resetContractViolationReporter() {
    uiContractJson.setViolationReporter(null);
}

module.exports = {
    captureContractViolations,
    resetContractViolationReporter,
    uiContractJson,
};
