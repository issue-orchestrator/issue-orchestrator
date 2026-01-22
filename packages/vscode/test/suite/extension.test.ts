import * as assert from "assert";
import * as vscode from "vscode";

suite("Issue Orchestrator Extension", () => {
  if (process.env.CI) {
    test("skipped in CI", function () {
      this.skip();
    });
    return;
  }

  test("commands are registered", async () => {
    const commands = await vscode.commands.getCommands(true);
    assert.ok(commands.includes("issueOrchestrator.start"));
    assert.ok(commands.includes("issueOrchestrator.refresh"));
    assert.ok(commands.includes("issueOrchestrator.quickActions"));
  });

  test("refresh command executes", async () => {
    await vscode.commands.executeCommand("issueOrchestrator.refresh");
  });
});
