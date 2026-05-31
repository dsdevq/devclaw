/**
 * Runtime validation: give DevClaw v2 a real-shaped task — fix a bug in
 * existing code, prove it's fixed by running tests — and verify by
 * actually executing the test suite afterward.
 *
 * Goes beyond the "did a file get written" smoke test: we judge the
 * outcome by whether the project's own tests pass.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import {
  existsSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
  readFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// --- the throwaway project --------------------------------------------------

const BUGGY_CALCULATOR = `def add(a, b):
    # BUG: should be a + b
    return a - b


def multiply(a, b):
    return a * b
`;

// Uses stdlib unittest so the test environment doesn't need pytest installed.
const CALCULATOR_TESTS = `import unittest
from calculator import add, multiply


class TestCalculator(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(0, 0), 0)
        self.assertEqual(add(-1, 1), 0)

    def test_multiply(self):
        self.assertEqual(multiply(2, 3), 6)
        self.assertEqual(multiply(0, 5), 0)


if __name__ == "__main__":
    unittest.main()
`;

function setupProject(workspaceDir: string): void {
  writeFileSync(resolve(workspaceDir, "calculator.py"), BUGGY_CALCULATOR);
  writeFileSync(resolve(workspaceDir, "test_calculator.py"), CALCULATOR_TESTS);
  writeFileSync(
    resolve(workspaceDir, "README.md"),
    "# calculator\n\nRun tests with: `python -m unittest test_calculator.py -v`\n",
  );
}

function runTests(workspaceDir: string): {
  passed: boolean;
  stdout: string;
  stderr: string;
} {
  const r = spawnSync(
    "python3",
    ["-m", "unittest", "test_calculator.py", "-v"],
    {
      cwd: workspaceDir,
      encoding: "utf8",
    },
  );
  return {
    passed: r.status === 0,
    stdout: r.stdout ?? "",
    stderr: r.stderr ?? "",
  };
}

// --- the runtime test -------------------------------------------------------

async function main(): Promise<void> {
  const workspaceDir = mkdtempSync(
    `${tmpdir()}/devclaw-v2-runtime-fix-bug-`,
  );
  console.log(`workspace: ${workspaceDir}`);

  setupProject(workspaceDir);

  // Sanity: confirm the tests FAIL before DevClaw runs — if they pass, the
  // bug isn't really there and the test isn't proving anything.
  const before = runTests(workspaceDir);
  console.log(
    `precheck: tests ${before.passed ? "PASS (unexpected)" : "FAIL (expected)"}`,
  );
  if (before.passed) {
    throw new Error(
      "Buggy code didn't fail its own tests — runtime test is broken, not DevClaw",
    );
  }

  const serverEntry = resolve(__dirname, "..", "src", "mcp-server.ts");
  const transport = new StdioClientTransport({
    command: "npx",
    args: ["tsx", serverEntry],
  });
  const client = new Client(
    { name: "devclaw-runtime-test", version: "0.0.1" },
    { capabilities: {} },
  );

  console.log("connecting devclaw v2…");
  await client.connect(transport);

  const goal = [
    "There is a bug in calculator.py — the add() function does subtraction",
    "instead of addition. Fix it so add(a, b) returns a + b. After fixing,",
    "run `python3 -m unittest test_calculator.py -v` from this directory",
    "and confirm all tests pass. Do not modify the tests; only fix the bug.",
  ].join(" ");

  console.log("calling implement_feature with a real bug-fix goal…");
  const t0 = Date.now();
  const result = await client.callTool({
    name: "implement_feature",
    arguments: { workspace_dir: workspaceDir, goal },
  });
  const dt = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`tool returned in ${dt}s, isError=${result.isError}`);

  await client.close();

  if (result.isError) {
    console.error("DevClaw reported an error:");
    console.error(
      (result.content as Array<{ type: string; text?: string }>)
        .map((c) => c.text ?? "")
        .join("\n"),
    );
    throw new Error("DevClaw returned isError=true — runtime test failed");
  }

  // The real validation — run the tests OURSELVES, don't trust the agent.
  console.log("verifying: running tests against modified workspace…");
  const after = runTests(workspaceDir);
  console.log("--- pytest stdout ---");
  console.log(after.stdout);
  console.log("--- pytest stderr ---");
  console.log(after.stderr);

  if (!after.passed) {
    throw new Error(
      "Tests still FAIL after DevClaw ran — bug not actually fixed",
    );
  }

  // Bonus check: confirm the fix is the RIGHT fix (a + b), not a hack like
  // "if a==2 and b==3 return 5". Reading the source helps catch
  // pathological "passes the tests but isn't the fix" answers.
  const fixed = readFileSync(
    resolve(workspaceDir, "calculator.py"),
    "utf8",
  );
  console.log("--- calculator.py after fix ---");
  console.log(fixed);

  if (!/a\s*\+\s*b/.test(fixed)) {
    throw new Error(
      "Tests pass but calculator.py does not contain 'a + b' — DevClaw may have hacked the test instead of fixing the function",
    );
  }
  if (/return\s+a\s*-\s*b/.test(fixed)) {
    throw new Error(
      "calculator.py still contains 'return a - b' — fix incomplete",
    );
  }

  console.log("PASS — DevClaw v2 fixed a real bug, tests pass, fix is the right shape");

  // Clean up. Comment out if you want to inspect the workspace.
  if (existsSync(workspaceDir)) {
    rmSync(workspaceDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error("FAIL:", err);
  process.exit(1);
});
