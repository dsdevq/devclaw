/**
 * Runtime validation for the HTTP transport.
 *
 * Boots devclaw-mcp in DEVCLAW_TRANSPORT=http mode as a subprocess, hits
 * /health to confirm the listener is up, connects an MCP client over
 * streamable-http, runs a quick implement_feature → get_status flow.
 *
 * This is the transport OpenClaw will use to call DevClaw v2 in production
 * via its `mcp.servers` config — if this test passes, the integration
 * surface is real.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { mkdtempSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { spawn, ChildProcess } from "node:child_process";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

async function waitForHealth(port: number, deadlineMs = 15_000): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < deadlineMs) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/health`);
      if (r.ok) {
        const body = await r.json();
        console.log(`/health says ${JSON.stringify(body)}`);
        return;
      }
    } catch {
      // not listening yet
    }
    await new Promise((res) => setTimeout(res, 250));
  }
  throw new Error(`devclaw-mcp /health never came up on port ${port}`);
}

function unwrap(result: { content: unknown }): unknown {
  const content = result.content as Array<{ type: string; text?: string }>;
  return JSON.parse(content.map((c) => c.text ?? "").join("\n"));
}

async function main(): Promise<void> {
  const workspaceDir = mkdtempSync(`${tmpdir()}/devclaw-v2-http-`);
  const port = 8765; // dev-only, avoids the production 8000
  console.log(`workspace: ${workspaceDir}, port: ${port}`);

  // Minimal task: just create a file. Don't repeat the full bug-fix scenario;
  // that's covered by runtime-async.ts. Here we're testing the *transport*.
  writeFileSync(
    resolve(workspaceDir, "task.md"),
    "Goal: prove HTTP transport works.\n",
  );

  const serverEntry = resolve(__dirname, "..", "src", "mcp-server.ts");
  let child: ChildProcess | null = null;

  try {
    child = spawn(
      "npx",
      ["tsx", serverEntry],
      {
        env: {
          ...process.env,
          DEVCLAW_TRANSPORT: "http",
          DEVCLAW_PORT: String(port),
          DEVCLAW_DB: `${workspaceDir}/devclaw.db`,
        } as Record<string, string>,
        stdio: ["ignore", "inherit", "inherit"],
      },
    );

    await waitForHealth(port);

    const transport = new StreamableHTTPClientTransport(
      new URL(`http://127.0.0.1:${port}/mcp`),
    );
    const client = new Client(
      { name: "devclaw-http-runtime-test", version: "0.0.1" },
      { capabilities: {} },
    );

    console.log("connecting over streamable-http…");
    await client.connect(transport);

    const tools = await client.listTools();
    console.log("tools:", tools.tools.map((t) => t.name).join(", "));
    const expected = ["implement_feature", "get_status", "list_tasks"];
    for (const name of expected) {
      if (!tools.tools.some((t) => t.name === name)) {
        throw new Error(`expected tool '${name}' not exposed over HTTP`);
      }
    }

    console.log("calling implement_feature…");
    const t0 = Date.now();
    const submitResult = await client.callTool({
      name: "implement_feature",
      arguments: {
        workspace_dir: workspaceDir,
        goal:
          "Create a file named http-ok.txt in the current workspace with " +
          "the single line: 'http transport works'",
      },
    });
    const dt = Date.now() - t0;
    const { task_id: taskId, status } = unwrap(submitResult) as {
      task_id: string;
      status: string;
    };
    console.log(`submit: ${dt}ms, task_id=${taskId}, status=${status}`);
    if (dt > 3000) throw new Error(`submit too slow (${dt}ms) — not async`);

    // Poll
    const deadline = Date.now() + 240_000;
    let terminal: { status: string; error: string | null } | null = null;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 2000));
      const statusResult = await client.callTool({
        name: "get_status",
        arguments: { task_id: taskId },
      });
      const t = unwrap(statusResult) as {
        status: string;
        error: string | null;
      };
      if (t.status === "done" || t.status === "failed") {
        terminal = t;
        break;
      }
    }
    if (!terminal) throw new Error("never reached terminal status");
    console.log(`terminal status: ${terminal.status}, error: ${terminal.error}`);
    if (terminal.status !== "done") {
      throw new Error(`task ended in ${terminal.status} — http path broken`);
    }

    const out = resolve(workspaceDir, "http-ok.txt");
    if (!existsSync(out)) throw new Error("http-ok.txt was not created");
    console.log("http-ok.txt exists ✓");

    await client.close();
    console.log("PASS — HTTP transport + async + state all work end-to-end");
  } finally {
    if (child) {
      child.kill("SIGTERM");
      // Give it a moment to flush + close the db cleanly.
      await new Promise((r) => setTimeout(r, 500));
    }
    rmSync(workspaceDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error("FAIL:", err);
  process.exit(1);
});
