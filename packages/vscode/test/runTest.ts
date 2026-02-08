import * as path from "path";
import * as fs from "fs";
import * as os from "os";
import { fileURLToPath } from "url";
import { runTests } from "@vscode/test-electron";

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
