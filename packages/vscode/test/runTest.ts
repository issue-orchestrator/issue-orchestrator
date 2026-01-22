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
  const userDataDir = path.join(os.tmpdir(), "io-vscode-user-data");
  const extensionsDir = path.join(os.tmpdir(), "io-vscode-extensions");
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.mkdirSync(extensionsDir, { recursive: true });

  await runTests({
    extensionDevelopmentPath,
    extensionTestsPath,
    launchArgs: [
      path.resolve(currentDir, "../../../"),
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
