import * as assert from "assert";
import { createRequire } from "module";

const require = createRequire(import.meta.url);
const vscode = require("vscode") as typeof import("vscode");

suite("Issue Orchestrator Extension", () => {
  test("extension activates", async () => {
    const extension = vscode.extensions.getExtension("issue-orchestrator.issue-orchestrator");
    assert.ok(extension, "Extension not found");
    await extension.activate();
    assert.ok(extension.isActive);
  });
});
