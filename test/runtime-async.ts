/**
 * Runtime validation for slice 2 (async + state store + polling).
 *
 * Pattern:
 *   1. Set up a throwaway buggy project + failing test suite.
 *   2. Call implement_feature — assert it returns task_id IMMEDIATELY
 *      (synchronous-blocking would defeat the slice 2 architecture).
 *   3. Poll get_status(task_id) until status is "done" or "failed".
 *      Verify intermediate states (pending → running → done) make sense.
 *   4. Run the project's own test suite ourselves to judge the fix.
 *   5. list_tasks should include our task.
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

// Same throwaway project as runtime-fix-bug.ts. Reusing the same shape so
// the difference under test is the async flow, not the task itself.
const BUGGY = `def divide(a, b):
    # BUG: should be a / b
    return a * b
`;
const TESTS = `import unittest
from calculator import divide


class TestCalculator(unittest.TestCase):
    def test_divide_basic(self):
        self.assertEqual(divide(10, 2), 5)
        self.assertEqual(divide(9, 3), 3)
    def test_divide_zero_dividend(self):
        self.assertEqual(divide(0, 5), 0)


if __name__ == "__main__":
    unittest.main()
`;

function setupProject(workspaceDir: string): void {
  writeFileSync(resolve(workspaceDir, "calculator.py"), BUGGY);
  writeFileSync(resolve(workspaceDir, "test_calculator.py"), TESTS);
}

function runTests(workspaceDir: string): { passed: boolean; output: string } {
  const r = spawnSync(
    "python3",
    ["-m", "unittest", "test_calculator.py", "-v"],
    { cwd: workspaceDir, encoding: "utf8" },
  );
  return {
    passed: r.status === 0,
    output: `${r.stdout ?? ""}\n${r.stderr ?? ""}`,
  };
}

type TaskRow = {
  id: string;
  status: "pending" | "running" | "done" | "failed";
  workspaceDir: string;
  goal: string;
  resultJson: string | null;
  error: string | null;
  createdAt: number;
  startedAt: number | null;
  completedAt: number | null;
};

function unwrap(result: { content: unknown }): unknown {
  const content = result.content as Array<{ type: string; text?: string }>;
  const text = content.map((c) => c.text ?? "").join("\n");
  return JSON.parse(text);
}

async function main(): Promise<void> {
  const workspaceDir = mkdtempSync(`${tmpdir()}/devclaw-v2-async-`);
  console.log(`workspace: ${workspaceDir}`);
  setupProject(workspaceDir);

  // Use a temp DB so we don't pollute any persistent state.
  const dbPath = `${workspaceDir}/devclaw.db`;

  // sanity: bug exists
  const before = runTests(workspaceDir);
  if (before.passed) throw new Error("test scaffold is broken — buggy code passes its own tests");
  console.log("precheck: buggy tests FAIL (expected)");

  const serverEntry = resolve(__dirname, "..", "src", "mcp-server.ts");
  const transport = new StdioClientTransport({
    command: "npx",
    args: ["tsx", serverEntry],
    env: {
      ...process.env,
      DEVCLAW_DB: dbPath,
      DEVCLAW_TRANSPORT: "stdio",
    } as Record<string, string>,
  });
  const client = new Client(
    { name: "devclaw-async-runtime-test", version: "0.0.1" },
    { capabilities: {} },
  );

  console.log("connecting devclaw v2…");
  await client.connect(transport);

  // --- 1. submit ---
  const submitT0 = Date.now();
  const submitResult = await client.callTool({
    name: "implement_feature",
    arguments: {
      workspace_dir: workspaceDir,
      goal: [
        "There is a bug in calculator.py — divide(a, b) does multiplication",
        "instead of division. Fix it so divide(a, b) returns a / b. Run",
        "`python3 -m unittest test_calculator.py -v` from this directory to",
        "confirm the tests pass. Do not modify the tests.",
      ].join(" "),
    },
  });
  const submitDt = Date.now() - submitT0;
  const submitPayload = unwrap(submitResult) as { task_id: string; status: string };
  console.log(
    `submit returned in ${submitDt}ms — task_id=${submitPayload.task_id}, status=${submitPayload.status}`,
  );
  if (submitDt > 2000) {
    throw new Error(
      `submit took ${submitDt}ms — slice 2 should return immediately (<2s); something is blocking`,
    );
  }
  if (submitPayload.status !== "pending") {
    throw new Error(
      `expected initial status pending, got ${submitPayload.status}`,
    );
  }

  // --- 2. poll until terminal ---
  const taskId = submitPayload.task_id;
  let lastStatus = submitPayload.status;
  let task: TaskRow | null = null;
  const sawStatuses = new Set<string>([lastStatus]);
  const deadline = Date.now() + 240_000; // 4 min hard cap

  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2000));
    const statusResult = await client.callTool({
      name: "get_status",
      arguments: { task_id: taskId },
    });
    task = unwrap(statusResult) as TaskRow;
    if (task.status !== lastStatus) {
      console.log(`  status: ${lastStatus} → ${task.status}`);
      sawStatuses.add(task.status);
      lastStatus = task.status;
    }
    if (task.status === "done" || task.status === "failed") break;
  }

  if (!task) throw new Error("never got a task row back from get_status");

  console.log("transitions observed:", [...sawStatuses].join(" → "));
  if (!sawStatuses.has("running")) {
    // Polling at 2s intervals may miss a fast running phase — note but
    // don't fail, the terminal state is what matters.
    console.log("(note: didn't catch the 'running' transition — too fast at 2s polling)");
  }

  if (task.status !== "done") {
    console.error("task row:", task);
    throw new Error(`task ended in status=${task.status}, expected done`);
  }

  // --- 3. independent verification: run tests ---
  const after = runTests(workspaceDir);
  console.log("--- unittest output ---");
  console.log(after.output);
  if (!after.passed) throw new Error("tests still FAIL — bug not actually fixed");

  // --- 4. confirm code shape ---
  const fixed = readFileSync(resolve(workspaceDir, "calculator.py"), "utf8");
  console.log("--- calculator.py after fix ---");
  console.log(fixed);
  if (!/return\s+a\s*\/\s*b/.test(fixed)) {
    throw new Error("calculator.py does not return a/b — fix not the right shape");
  }
  if (/return\s+a\s*\*\s*b/.test(fixed)) {
    throw new Error("calculator.py still multiplies — fix incomplete");
  }

  // --- 5. list_tasks should include us ---
  const listResult = await client.callTool({
    name: "list_tasks",
    arguments: { limit: 10 },
  });
  const tasks = unwrap(listResult) as TaskRow[];
  const ours = tasks.find((t) => t.id === taskId);
  if (!ours) throw new Error("list_tasks didn't include our task");
  console.log(
    `list_tasks: ${tasks.length} task(s), ours status=${ours.status}, completed_at=${ours.completedAt}`,
  );

  await client.close();
  console.log(
    "PASS — async submit, polling, state transitions, and independent fix verification all work",
  );

  rmSync(workspaceDir, { recursive: true, force: true });
}

main().catch((err) => {
  console.error("FAIL:", err);
  process.exit(1);
});
