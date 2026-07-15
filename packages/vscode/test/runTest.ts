import * as path from "path";
import * as fs from "fs";
import * as os from "os";
import { fileURLToPath } from "url";
import { runTests } from "@vscode/test-electron";

// Pin the VS Code build under test. Left unset, @vscode/test-electron resolves
// "stable", so every VS Code release invalidated the cache and forced a fresh
// ~273MB download -- which then blew the default 15s idle timeout and failed
// `make test-vscode` outright. Pinning keeps the harness reproducible and lets
// the shared cache actually hit; bump deliberately. Must stay at or above the
// `engines.vscode` floor in package.json.
const DEFAULT_VSCODE_VERSION = "1.128.1";

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
