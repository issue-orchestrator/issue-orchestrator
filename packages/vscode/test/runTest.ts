import * as path from "path";
import * as fs from "fs";
import * as os from "os";
import { fileURLToPath } from "url";
import { runTests } from "@vscode/test-electron";

function resolveVsCodeExecutable(): string | undefined {
  const explicit = process.env.VSCODE_EXECUTABLE_PATH;
  if (explicit) {
    return explicit;
  }

  if (process.env.VSCODE_TEST_USE_INSIDERS !== "1") {
    return undefined;
  }

  const candidates: string[] = [];
  switch (process.platform) {
    case "darwin":
      candidates.push(
        "/Applications/Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code"
      );
      break;
    case "win32":
      candidates.push(
        "C:\\Program Files\\Microsoft VS Code Insiders\\Code - Insiders.exe",
        "C:\\Program Files (x86)\\Microsoft VS Code Insiders\\Code - Insiders.exe",
        path.join(
          process.env.LOCALAPPDATA ?? "",
          "Programs",
          "Microsoft VS Code Insiders",
          "Code - Insiders.exe"
        )
      );
      break;
    default:
      candidates.push(
        "/usr/bin/code-insiders",
        "/usr/local/bin/code-insiders"
      );
      break;
  }

  for (const candidate of candidates) {
    if (!candidate) {
      continue;
    }
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  return undefined;
}

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
  const vscodeExecutablePath = resolveVsCodeExecutable();

  await runTests({
    extensionDevelopmentPath,
    extensionTestsPath,
    ...(vscodeExecutablePath ? { vscodeExecutablePath } : {}),
    launchArgs: [
      path.resolve(currentDir, "../../../"),
      "--disable-extensions",
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
