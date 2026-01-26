import * as assert from "assert";
import { createRequire } from "module";

const require = createRequire(import.meta.url);
const vscode = require("vscode") as typeof import("vscode");

suite("Issue Orchestrator E2E", () => {
  if (process.env.IO_VSCODE_E2E !== "1") {
    test("skipped unless IO_VSCODE_E2E=1", function () {
      this.skip();
    });
    return;
  }

  test("snapshot fetches from running orchestrator", async () => {
    const configPath = process.env.IO_E2E_CONFIG_PATH;
    const repoRoot = process.env.IO_E2E_REPO_ROOT;
    const repoName = process.env.IO_E2E_REPO_NAME;
    assert.ok(configPath, "IO_E2E_CONFIG_PATH not set");
    assert.ok(repoRoot, "IO_E2E_REPO_ROOT not set");
    assert.ok(repoName, "IO_E2E_REPO_NAME not set");

    const config = vscode.workspace.getConfiguration("issueOrchestrator");
    await config.update("configPath", configPath, vscode.ConfigurationTarget.Workspace);
    await config.update("repoRoot", repoRoot, vscode.ConfigurationTarget.Workspace);
    await config.update("autoStart", false, vscode.ConfigurationTarget.Workspace);

    const extension = vscode.extensions.getExtension("issue-orchestrator.issue-orchestrator");
    assert.ok(extension, "Extension not found");
    await extension.activate();

    const snapshot = (await vscode.commands.executeCommand(
      "issueOrchestrator._e2eSnapshot"
    )) as { info?: { repo?: string | null } } | undefined;

    assert.ok(snapshot, "No snapshot returned");
    assert.strictEqual(snapshot?.info?.repo, repoName);
  });
});
