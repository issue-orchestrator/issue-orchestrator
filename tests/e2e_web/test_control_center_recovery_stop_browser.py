"""Native dialog behavior for guarded retained-work engine stops."""

from pathlib import Path

from playwright.sync_api import Page, expect


ROOT = Path(__file__).resolve().parents[2]
STOP_SCRIPT = (
    ROOT
    / "src"
    / "issue_orchestrator"
    / "static"
    / "js"
    / "control_center_recovery_stop.js"
)
RECOVERY_SCRIPT = (
    ROOT / "src" / "issue_orchestrator" / "static" / "js" / "control_center_recovery.js"
)
CONTROL_CENTER_CSS = (
    ROOT / "src" / "issue_orchestrator" / "static" / "css" / "control_center.css"
)


def test_guarded_stop_dialog_keeps_failure_visible_and_restores_focus(
    page: Page,
) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.set_content(
        """
        <main id="reposContent"></main>
        <button id="outside">Outside</button>
        <dialog class="modal recovery-stop-dialog" id="recoveryStopDialog"
                aria-labelledby="recoveryStopDialogTitle"
                aria-describedby="recoveryStopSummary">
          <form id="recoveryStopForm" method="dialog">
            <div class="modal-header">
              <h2 id="recoveryStopDialogTitle">Stop Repository Engine</h2>
              <button type="button" id="closeRecoveryStopDialog">Close</button>
            </div>
            <div class="modal-body">
              <p id="recoveryStopSummary"></p>
              <label for="recoveryStopReason">Reason for stopping</label>
              <textarea id="recoveryStopReason" required></textarea>
              <p id="recoveryStopStatus" role="status" aria-live="polite" hidden></p>
            </div>
            <div class="modal-footer">
              <button type="button" id="cancelRecoveryStop">Cancel</button>
              <button type="submit" id="confirmRecoveryStop">Stop engine</button>
            </div>
          </form>
        </dialog>
        """
    )
    page.add_style_tag(path=str(CONTROL_CENTER_CSS))
    page.add_script_tag(path=str(STOP_SCRIPT))
    page.add_script_tag(path=str(RECOVERY_SCRIPT))
    page.evaluate(
        """
        async () => {
          const escapeHtml = value => String(value)
            .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
          const engine = {
            repo_root: '/repo', instance_id: 'worker-a', host: 'host-a', label: 'worker-a',
            process: {host: 'host-a', pid: 1234, started_at: '12345', instance_id: 'worker-a'},
          };
          const row = {
            kind: 'owned',
            work: {authority: {record_id: 'record-a', issue_number: 42}},
            owner: {engine, owner_fence: 7, stop_availability: 'available'},
            stop_action: {
              record_id: 'record-a', expected_engine: engine, expected_owner_fence: 7,
              graceful_timeout_seconds: 120, force_on_timeout: true,
            },
          };
          const repoKey = `repo-${'a'.repeat(64)}`;
          window.recoveryReads = 0;
          const recoveryRepo = {repo_key: repoKey};
          const recoveryPayload = fence => ({
            repo_key: repoKey,
            status: 'available',
            engine_groups: [{
              engine,
              presentation: 'observed',
              presentation_message: 'Exact engine is running',
              records: [{
                kind: 'owned',
                work: {
                  authority: {
                    record_id: 'record-a', issue_number: 42, branch_name: 'issue-42',
                  },
                  state: 'queued', reason: '', escrow_retained: true,
                },
                owner: {
                  engine, owner_fence: fence, stop_availability: 'exact_target_unavailable',
                },
                stop_action: null,
              }],
            }],
            unowned_records: [],
            message: 'Preserved validated work is available',
          });
          const recoveryView = createControlCenterRecoveryView({
            escapeHtml,
            fetch: async () => {
              window.recoveryReads += 1;
              return {ok: true, json: async () => recoveryPayload(
                window.recoveryReads === 1 ? 7 : 8,
              )};
            },
          });
          await recoveryView.load([recoveryRepo]);
          window.resolveStop = null;
          const view = createControlCenterRecoveryStopView({
                document,
                escapeHtml,
                fetch: () => new Promise(resolve => {
                  window.resolveStop = () => resolve({
                    ok: false,
                    json: async () => ({
                      status: 'owner_changed',
                      observed_owner: null,
                      message: 'The validated-work owner changed; refresh before stopping it',
                    }),
                  });
                }),
            refresh: async key => {
              recoveryView.invalidate(key);
              await recoveryView.load([recoveryRepo]);
              window.refreshedOwnerFence =
                recoveryRepo.validated_work.engine_groups[0].records[0].owner.owner_fence;
            },
          });
          const container = document.querySelector('#reposContent');
          container.innerHTML = view.renderAction(row, repoKey);
          view.bind(container);
        }
        """
    )

    opener = page.get_by_role(
        "button", name="Stop engine worker-a owning retained work for issue 42"
    )
    opener.click()
    dialog = page.locator("#recoveryStopDialog")
    expect(dialog).to_be_visible()
    reason = page.locator("#recoveryStopReason")
    expect(reason).to_be_focused()
    expect(page.locator("#recoveryStopSummary")).to_contain_text("stops every job")
    expect(page.locator("#recoveryStopSummary")).to_contain_text("120 seconds")
    expect(page.locator("#recoveryStopSummary")).to_contain_text("force-stops")

    for _ in range(6):
        page.keyboard.press("Tab")
        assert page.evaluate(
            "document.querySelector('#recoveryStopDialog').contains(document.activeElement)"
        )

    reason.fill("Engine is wedged")
    page.get_by_role("button", name="Stop engine", exact=True).click()
    expect(dialog).to_have_attribute("aria-busy", "true")
    expect(reason).to_be_focused()
    page.keyboard.press("Tab")
    expect(reason).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()

    page.evaluate("window.resolveStop()")
    status = page.locator("#recoveryStopStatus")
    expect(status).to_be_visible()
    expect(status).to_contain_text("owner changed")
    expect(dialog).to_be_visible()
    assert page.evaluate("window.recoveryReads") == 2
    assert page.evaluate("window.refreshedOwnerFence") == 8

    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()


def test_unavailable_exact_target_keyboard_navigation_only_focuses_engine_controls(
    page: Page,
) -> None:
    page.set_content(
        """
        <main id="reposContent"></main>
        <button type="button" data-action="stop" id="independentStop">Stop engine</button>
        <dialog id="recoveryStopDialog"><form id="recoveryStopForm">
          <button id="closeRecoveryStopDialog"></button>
          <button id="cancelRecoveryStop"></button>
          <button id="confirmRecoveryStop"></button>
          <p id="recoveryStopSummary"></p><textarea id="recoveryStopReason"></textarea>
          <p id="recoveryStopStatus"></p>
        </form></dialog>
        """
    )
    page.add_script_tag(path=str(STOP_SCRIPT))
    page.evaluate(
        """
        () => {
          const escapeHtml = value => String(value)
            .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
          const engine = {
            repo_root: '/repo', instance_id: 'worker-a', host: 'host-a', label: 'worker-a',
            process: {host: 'host-a', pid: 1234, started_at: '12345', instance_id: 'worker-a'},
          };
          const row = {
            kind: 'owned',
            work: {authority: {record_id: 'record-a', issue_number: 42}},
            owner: {engine, owner_fence: 7, stop_availability: 'exact_target_unavailable'},
            stop_action: null,
          };
          window.stopDispatches = 0;
          const view = createControlCenterRecoveryStopView({
            document,
            escapeHtml,
            fetch: async () => { window.stopDispatches += 1; },
            navigateToEngineControls: () => document.querySelector('#independentStop').focus(),
          });
          const container = document.querySelector('#reposContent');
          container.innerHTML = view.renderAction(row, `repo-${'a'.repeat(64)}`);
          view.bind(container);
        }
        """
    )

    navigation = page.get_by_role("button", name="Go to independent engine controls")
    navigation.focus()
    page.keyboard.press("Enter")

    expect(page.locator("#independentStop")).to_be_focused()
    assert page.evaluate("window.stopDispatches") == 0
