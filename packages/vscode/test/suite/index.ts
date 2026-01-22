import * as path from "path";
import { fileURLToPath } from "url";
import Mocha from "mocha";
import { glob } from "glob";

export function run(): Promise<void> {
  const mocha = new Mocha({
    ui: "tdd",
    timeout: 60000,
  });

  const currentFile = fileURLToPath(import.meta.url);
  const testsRoot = path.dirname(currentFile);

  return new Promise((resolve, reject) => {
    glob("**/*.test.js", { cwd: testsRoot })
      .then((files) => {
        files.forEach((file) => mocha.addFile(path.resolve(testsRoot, file)));
        try {
          mocha.run((failures) => {
            if (failures > 0) {
              reject(new Error(`${failures} tests failed.`));
            } else {
              resolve();
            }
          });
        } catch (error) {
          reject(error);
        }
      })
      .catch(reject);
  });
}
