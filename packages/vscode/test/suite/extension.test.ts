import * as assert from "assert";
import { createRequire } from "module";
import { normalizeClientCapabilities, sessionActionMode } from "../../src/clientCapabilities.js";

const require = createRequire(import.meta.url);
const vscode = require("vscode") as typeof import("vscode");

suite("Issue Orchestrator Extension", () => {
  test("extension activates", async () => {
    const extension = vscode.extensions.getExtension("issue-orchestrator.issue-orchestrator");
    assert.ok(extension, "Extension not found");
    await extension.activate();
    assert.ok(extension.isActive);
  });

  test("normalizeClientCapabilities defaults missing fields", () => {
    const capabilities = normalizeClientCapabilities({ focus_session: true });
    assert.strictEqual(capabilities.focus_session, true);
    assert.strictEqual(capabilities.open_path, false);
    assert.strictEqual(capabilities.reveal_worktree, false);
    assert.strictEqual(capabilities.local_server_paths_only, true);
    assert.strictEqual(capabilities.host_platform, "unknown");
  });

  test("sessionActionMode falls back to console when focus unsupported", () => {
    assert.strictEqual(sessionActionMode({ focus_session: false }), "console");
    assert.strictEqual(sessionActionMode({ focus_session: true }), "focus");
  });
});
