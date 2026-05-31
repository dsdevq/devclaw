/**
 * Slice 3 runtime validation: the `fix_bug` MCP tool specifically.
 * Mirrors runtime-fix-bug.ts (which uses `implement_feature`) but calls
 * the specialized tool — proves the wrapper-prompt fix-bug flow works.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import {
  mkdtempSync,
  rmSync,
  writeFileSync,
  readFileSync,
  existsSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const BUGGY = `def subtract(a, b):
    # BUG: should be a - b
    return a + b
`;
const TESTS = `import unittest
from calculator import subtract


class TestSubtract(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(subtract(10, 3), 7)
        self.assertEqual(subtract(0, 0), 0)
        self.assertEqual(subtract(5, 5), 0)


if __name__ == "__main__":
    unittest.main()
`;

function runTests(dir: string): { passed: boolean; output: string } {
  const r = spawnSync(
    "python3",
    ["-m", "unittest", "test_calculator.py", "-v"],
    { cwd: dir, encoding: "utf8" },
  );
  return {
    passed: r.status === 0,
    output: `${r.stdout ?? ""}\n${r.stderr ?? ""}`,
  };
}

function unwrap(result: { content: unknown }): unknown {
  const content = result.content as Array<{ type: string; text?: string }>;
  return JSON.parse(content.map((c) => c.text ?? "").join("\n"));
}

async function main(): Promise<void> {
  const workspaceDir = mkdtempSync(`${tmpdir()}/devclaw-v2-fix-bug-tool-`);
  writeFileSync(resolve(workspaceDir, "calculator.py"), BUGGY);
  writeFileSync(resolve(workspaceDir, "test_calculator.py"), TESTS);
  console.log(`workspace: ${workspaceDir}`);

  const before = runTests(workspaceDir);
  if (before.passed) throw new Error("scaffold broken: buggy tests pass");
  console.log("precheck: buggy tests FAIL (expected)");

  const serverEntry = resolve(__dirname, "..", "src", "mcp-server.ts");
  const transport = new StdioClientTransport({
    command: "npx",
    args: ["tsx", serverEntry],
    env: {
      ...process.env,
      DEVCLAW_DB: `${workspaceDir}/devclaw.db`,
      DEVCLAW_TRANSPORT: "stdio",
    } as Record<string, string>,
  });
  const client = new Client(
    { name: "devclaw-fix-bug-tool-test", version: "0.0.1" },
    { capabilities: {} },
  );
  await client.connect(transport);

  const tools = await client.listTools();
  if (!tools.tools.some((t) => t.name === "fix_bug")) {
    throw new Error("fix_bug tool not exposed");
  }

  console.log("calling fix_bug tool…");
  const submitResult = await client.callTool({
    name: "fix_bug",
    arguments: {
      workspace_dir: workspaceDir,
      description:
        "calculator.py's subtract(a, b) returns a + b instead of a - b.",
    },
  });
  const { task_id: taskId } = unwrap(submitResult) as {
    task_id: string;
    status: string;
  };
  console.log(`task_id=${taskId}`);

  // Poll
  const deadline = Date.now() + 240_000;
  let terminal: { status: string; kind?: string; error: string | null } | null = null;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2000));
    const t = unwrap(
      await client.callTool({
        name: "get_status",
        arguments: { task_id: taskId },
      }),
    ) as { status: string; kind?: string; error: string | null };
    if (t.status === "done" || t.status === "failed") {
      terminal = t;
      break;
    }
  }
  if (!terminal) throw new Error("no terminal status");
  if (terminal.status !== "done") {
    throw new Error(`task ended in ${terminal.status} — fix-bug path broken`);
  }
  if (terminal.kind !== "fix_bug") {
    throw new Error(`expected kind=fix_bug, got ${terminal.kind}`);
  }

  // Independent verification
  const after = runTests(workspaceDir);
  console.log("--- unittest output ---\n" + after.output);
  if (!after.passed) throw new Error("tests still FAIL");

  const fixed = readFileSync(resolve(workspaceDir, "calculator.py"), "utf8");
  console.log("--- calculator.py after fix ---\n" + fixed);
  if (!/return\s+a\s*-\s*b/.test(fixed)) {
    throw new Error("fix not the right shape — missing 'a - b'");
  }
  if (/return\s+a\s*\+\s*b/.test(fixed)) {
    throw new Error("calculator.py still adds — fix incomplete");
  }

  // list_tasks should be able to filter by kind
  const listed = unwrap(
    await client.callTool({
      name: "list_tasks",
      arguments: { kind: "fix_bug", limit: 5 },
    }),
  ) as Array<{ id: string; kind: string }>;
  if (!listed.find((t) => t.id === taskId)) {
    throw new Error("list_tasks(kind=fix_bug) didn't include our task");
  }
  if (listed.some((t) => t.kind !== "fix_bug")) {
    throw new Error("list_tasks(kind=fix_bug) returned non-fix_bug tasks");
  }
  console.log(`list_tasks(kind=fix_bug): ${listed.length} task(s), filter works`);

  await client.close();
  console.log("PASS — fix_bug tool, kind filtering, fix shape all correct");

  if (existsSync(workspaceDir)) rmSync(workspaceDir, { recursive: true, force: true });
}

main().catch((err) => {
  console.error("FAIL:", err);
  process.exit(1);
});
