"""Browser guardrail for preserved-work disclosure state across card refreshes."""

from pathlib import Path

from playwright.sync_api import Page, expect


ROOT = Path(__file__).resolve().parents[2]
RECOVERY_SCRIPT = (
    ROOT / "src" / "issue_orchestrator" / "static" / "js" / "control_center_recovery.js"
)


def test_recovery_disclosure_and_focus_survive_repo_card_refresh(page: Page) -> None:
    page.set_content('<main id="reposContent"></main>')
    page.add_script_tag(path=str(RECOVERY_SCRIPT))
    page.evaluate(
        """
        () => {
            const escapeHtml = value => String(value)
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;');
            const repoKey = `repo-${'a'.repeat(64)}`;
            const payload = {
                repo_key: repoKey,
                status: 'available',
                message: 'One retained record',
                engine_groups: [],
                unowned_records: [{
                    kind: 'unowned',
                    work: {
                        authority: {
                            record_id: 'record-42',
                            evidence_id: 'evidence-42',
                            observation_revision: 3,
                            validated_head_sha: 'b'.repeat(40),
                            branch_name: 'issue-42',
                            repo_slug: 'owner/repo',
                            issue_number: 42,
                            pr_number: null,
                            expected_remote_head_sha: null,
                            remote_baseline_status: 'unobserved',
                        },
                        state: 'queued',
                        failure: null,
                        reason: '',
                        escrow_retained: true,
                    },
                }],
            };
            const container = document.querySelector('#reposContent');
            const view = createControlCenterRecoveryView({
                escapeHtml,
                fetch: async () => {},
            });
            container.innerHTML = view.render({ validated_work: payload });
            window.recoveryTest = { container, payload, view };
        }
        """
    )

    summary = page.locator("details.repo-recovery summary")
    summary.click()
    summary.focus()
    expect(summary).to_be_focused()

    page.evaluate(
        """
        () => {
            const { container, payload, view } = window.recoveryTest;
            const captured = view.capture(container);
            container.innerHTML = view.render({ validated_work: payload });
            view.restore(container, captured);
        }
        """
    )

    refreshed = page.locator("details.repo-recovery summary")
    expect(refreshed).to_be_focused()
    expect(page.locator("details.repo-recovery")).to_have_js_property("open", True)
