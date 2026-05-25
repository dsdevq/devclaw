/**
 * Slice 3 runtime validation: review_repository must be READ-ONLY.
 *
 * Sets up a small project, snapshots every file's hash, calls
 * review_repository, then asserts NO file was modified, created, or
 * deleted. This is the load-bearing invariant for review_repository —
 * without it, the tool is just another implement_feature.
 *
 * Also asserts the agent_output (review report) is non-empty so we know
 * the tool produced *something*, not just refused to act.
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
  readdirSync,
  statSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { createHash } from "node:crypto";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Tiny but real-ish project to give the reviewer something to look at.
// Includes one mild "issue" (no input validation) so a sensible review
// would mention it.
const CALCULATOR = `def divide(a, b):
    # Returns a / b. No validation — caller's responsibility.
    return a / b


def add(a, b):
    return a + b
`;
const README = `# tiny-calc

A tiny calculator module. See calculator.py.

## Usage

    >>> from calculator import add, divide
    >>> add(2, 3)
    5
    >>> divide(10, 2)
    5.0
`;

type Snapshot = Map<string, string>;

function snapshot(dir: string): Snapshot {
  const out: Snapshot = new Map();
  function walk(p: string) {
    for (const entry of readdirSync(p, { withFileTypes: true })) {
      // Skip the devclaw.db file we're using for state — it will change.
      if (entry.name === "devclaw.db" || entry.name.startsWith("devclaw.db-")) continue;
      const full = `${p}/${entry.name}`;
      if (entry.isDirectory()) {
        walk(full);
      } else if (entry.isFile()) {
        const h = createHash("sha256").update(readFileSync(full)).digest("hex");
        out.set(full, h);
      }
    }
  }
  walk(dir);
  return out;
}

function diffSnapshots(before: Snapshot, after: Snapshot): {
  modified: string[];
  added: string[];
  removed: string[];
} {
  const modified: string[] = [];
  const added: string[] = [];
  const removed: string[] = [];
  for (const [path, hash] of before) {
    const afterHash = after.get(path);
    if (afterHash === undefined) removed.push(path);
    else if (afterHash !== hash) modified.push(path);
  }
  for (const path of after.keys()) {
    if (!before.has(path)) added.push(path);
  }
  return { modified, added, removed };
}

function unwrap(result: { content: unknown }): unknown {
  const content = result.content as Array<{ type: string; text?: string }>;
  return JSON.parse(content.map((c) => c.text ?? "").join("\n"));
}

async function main(): Promise<void> {
  const workspaceDir = mkdtempSync(`${tmpdir()}/devclaw-v2-review-`);
  writeFileSync(resolve(workspaceDir, "calculator.py"), CALCULATOR);
  writeFileSync(resolve(workspaceDir, "README.md"), README);
  console.log(`workspace: ${workspaceDir}`);

  // Snapshot AFTER setup, BEFORE devclaw runs.
  const before = snapshot(workspaceDir);
  console.log(`snapshot: ${before.size} files tracked`);

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
    { name: "devclaw-review-test", version: "0.0.1" },
    { capabilities: {} },
  );
  await client.connect(transport);

  const tools = await client.listTools();
  if (!tools.tools.some((t) => t.name === "review_repository")) {
    throw new Error("review_repository tool not exposed");
  }

  console.log("calling review_repository (read-only)…");
  const submitResult = await client.callTool({
    name: "review_repository",
    arguments: {
      workspace_dir: workspaceDir,
      focus: "input validation and error handling",
    },
  });
  const { task_id: taskId } = unwrap(submitResult) as {
    task_id: string;
    status: string;
  };
  console.log(`task_id=${taskId}`);

  // Poll
  const deadline = Date.now() + 240_000;
  let terminal: {
    status: string;
    kind?: string;
    resultJson: string | null;
  } | null = null;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2000));
    const t = unwrap(
      await client.callTool({
        name: "get_status",
        arguments: { task_id: taskId },
      }),
    ) as { status: string; kind?: string; resultJson: string | null };
    if (t.status === "done" || t.status === "failed") {
      terminal = t;
      break;
    }
  }
  if (!terminal) throw new Error("no terminal status");
  if (terminal.status !== "done") {
    throw new Error(`task ended in ${terminal.status}`);
  }
  if (terminal.kind !== "review_repository") {
    throw new Error(`expected kind=review_repository, got ${terminal.kind}`);
  }

  // --- THE LOAD-BEARING ASSERTION ---
  const after = snapshot(workspaceDir);
  const diff = diffSnapshots(before, after);
  console.log(
    `diff: modified=${diff.modified.length}, added=${diff.added.length}, removed=${diff.removed.length}`,
  );

  if (diff.modified.length > 0) {
    console.error("MODIFIED files:", diff.modified);
    throw new Error(
      "review_repository modified files — read-only invariant violated",
    );
  }
  if (diff.added.length > 0) {
    console.error("ADDED files:", diff.added);
    throw new Error(
      "review_repository added files — read-only invariant violated",
    );
  }
  if (diff.removed.length > 0) {
    console.error("REMOVED files:", diff.removed);
    throw new Error(
      "review_repository removed files — read-only invariant violated",
    );
  }

  // Confirm the agent actually produced a non-empty report.
  if (terminal.resultJson) {
    const result = JSON.parse(terminal.resultJson) as {
      agent_output?: string;
    };
    const out = result.agent_output ?? "";
    if (out.length < 100) {
      throw new Error(
        `review report suspiciously short (${out.length} chars) — agent may have refused entirely`,
      );
    }
    console.log(`review report: ${out.length} chars`);
  }

  await client.close();
  console.log(
    "PASS — review_repository is read-only, produced a real report, no files touched",
  );

  if (existsSync(workspaceDir)) rmSync(workspaceDir, { recursive: true, force: true });
}

main().catch((err) => {
  console.error("FAIL:", err);
  process.exit(1);
});
