import * as path from "path";
import * as fs from "fs";
import * as os from "os";
import { fileURLToPath } from "url";
import { runTests } from "@vscode/test-electron";

// Pin the VS Code build under test to the declared engines.vscode floor, so the
// harness validates the lower-bound compatibility contract: this extension is
// ESM ("type": "module"), and the VS Code extension host only supports ESM from
// 1.100, so it cannot even load below that. Testing at the floor proves it
// loads at the minimum we advertise -- testing only at a newer build would hide
// a raised real floor. Keep this equal to engines.vscode in package.json
// (enforced by test_vscode_types_pinned_to_engines_floor); raise both together.
//
// (Pinning also keeps the harness reproducible: left unset, @vscode/test-electron
// resolves "stable" and every VS Code release forced a fresh ~273MB download
// that blew the default 15s idle timeout.)
const DEFAULT_VSCODE_VERSION = "1.100.0";

// Generous idle timeout so a cold cache (CI, new machine, version bump) can
// still complete the download rather than aborting mid-stream.
const DOWNLOAD_IDLE_TIMEOUT_MS = 120_000;

async function main(): Promise<void> {
  const currentFile = fileURLToPath(import.meta.url);
  const currentDir = path.dirname(currentFile);
  const extensionDevelopmentPath = path.resolve(currentDir, "..", "..");
  const extensionTestsPath = path.resolve(currentDir, "./suite/index.js");
  const userDataDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "io-vscode-user-data-")
  );
  const extensionsDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "io-vscode-extensions-")
  );
  const workspacePath = process.env.IO_VSCODE_TEST_WORKSPACE;
  const cachePath =
    process.env.IO_VSCODE_TEST_CACHE_PATH ??
    path.join(os.homedir(), ".cache", "issue-orchestrator", "vscode-test");
  fs.mkdirSync(cachePath, { recursive: true });

  await runTests({
    version: process.env.IO_VSCODE_TEST_VERSION ?? DEFAULT_VSCODE_VERSION,
    timeout: DOWNLOAD_IDLE_TIMEOUT_MS,
    extensionDevelopmentPath,
    extensionTestsPath,
    cachePath,
    launchArgs: [
      ...(workspacePath ? [workspacePath] : []),
      "--user-data-dir",
      userDataDir,
      "--extensions-dir",
      extensionsDir,
    ],
    extensionTestsEnv: {
      IO_VSCODE_TEST: "1",
    },
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
