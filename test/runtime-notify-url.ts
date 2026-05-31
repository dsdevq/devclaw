/**
 * Regression test for notify_url callbacks.
 *
 * Boots devclaw-mcp in http mode, spins up a mock callback HTTP server on a
 * separate port, submits an implement_feature task with notify_url pointing
 * at the mock, and asserts:
 *   - the mock receives exactly one POST during the test window
 *   - the POST body has the expected shape (task_id, status, kind, …)
 *   - status is one of {done, failed} — both are terminal; the test does NOT
 *     pin to a specific outcome because that depends on whether the dev
 *     machine has the Python OpenHands venv set up. The mechanism under test
 *     is the notify path, not the OpenHands execution itself.
 *
 * Run with: `npm run test:notify-url`
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { spawn, ChildProcess } from "node:child_process";
import { createServer, Server } from "node:http";
import { AddressInfo } from "node:net";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

type CapturedPost = {
  url: string;
  headers: Record<string, string | string[] | undefined>;
  body: unknown;
};

async function waitForHealth(port: number, deadlineMs = 15_000): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < deadlineMs) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/health`);
      if (r.ok) return;
    } catch {
      // not listening yet
    }
    await new Promise((res) => setTimeout(res, 200));
  }
  throw new Error(`devclaw-mcp /health never came up on port ${port}`);
}

async function startMockCallback(): Promise<{
  server: Server;
  port: number;
  posts: CapturedPost[];
}> {
  const posts: CapturedPost[] = [];
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      let parsed: unknown = body;
      try {
        parsed = JSON.parse(body);
      } catch {
        // keep as string
      }
      posts.push({ url: req.url ?? "/", headers: req.headers, body: parsed });
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    });
  });
  await new Promise<void>((resolveListen) =>
    server.listen(0, "127.0.0.1", () => resolveListen()),
  );
  const port = (server.address() as AddressInfo).port;
  return { server, port, posts };
}

async function main(): Promise<void> {
  const workspaceDir = mkdtempSync(`${tmpdir()}/devclaw-v2-notify-`);
  const devclawPort = 8767;
  console.log(`workspace: ${workspaceDir}, devclaw port: ${devclawPort}`);

  const callback = await startMockCallback();
  const notifyUrl = `http://127.0.0.1:${callback.port}/devclaw-callback`;
  console.log(`mock callback listening on ${notifyUrl}`);

  const serverEntry = resolve(__dirname, "..", "src", "mcp-server.ts");
  let child: ChildProcess | null = null;

  try {
    child = spawn("npx", ["tsx", serverEntry], {
      env: {
        ...process.env,
        DEVCLAW_TRANSPORT: "http",
        DEVCLAW_PORT: String(devclawPort),
        DEVCLAW_HOST: "127.0.0.1",
        DEVCLAW_DB: `${workspaceDir}/devclaw.db`,
      } as Record<string, string>,
      stdio: ["ignore", "inherit", "inherit"],
    });

    await waitForHealth(devclawPort);

    const transport = new StreamableHTTPClientTransport(
      new URL(`http://127.0.0.1:${devclawPort}/mcp`),
    );
    const client = new Client(
      { name: "devclaw-notify-url-test", version: "0.0.1" },
      { capabilities: {} },
    );
    await client.connect(transport);

    // Submit a trivial implement_feature with the callback URL. The goal is
    // intentionally light — we don't care whether OpenHands succeeds or
    // fails; we care that the notify path fires exactly once with the right
    // payload shape regardless.
    const submitResult = (await client.callTool({
      name: "implement_feature",
      arguments: {
        workspace_dir: workspaceDir,
        goal:
          "Create a file called notify-ok.txt at the workspace root with " +
          "the single line: 'notify-url test ok'",
        notify_url: notifyUrl,
      },
    })) as { content: Array<{ text?: string }> };
    const submitText = (submitResult.content[0]?.text ?? "{}") as string;
    const { task_id: taskId } = JSON.parse(submitText) as { task_id: string };
    console.log(`submitted task_id=${taskId}`);
    if (!taskId) throw new Error("no task_id returned from submit");

    // Wait for the callback. We allow up to 240s — same window as
    // runtime-http.ts. In practice the failure path (no Python venv on a
    // fresh machine) fires within ~10s; the success path takes 30–60s.
    const deadline = Date.now() + 240_000;
    while (Date.now() < deadline) {
      if (callback.posts.length > 0) break;
      await new Promise((r) => setTimeout(r, 500));
    }

    if (callback.posts.length === 0) {
      throw new Error(
        `mock callback never received a POST within 240s (task_id=${taskId})`,
      );
    }
    if (callback.posts.length > 1) {
      throw new Error(
        `expected exactly 1 callback POST, got ${callback.posts.length}`,
      );
    }

    const got = callback.posts[0];
    console.log("callback received:", JSON.stringify(got, null, 2));

    if (got.url !== "/devclaw-callback") {
      throw new Error(`unexpected callback path: ${got.url}`);
    }
    const ct = got.headers["content-type"];
    if (!ct || !String(ct).includes("application/json")) {
      throw new Error(`expected JSON content-type, got ${ct}`);
    }

    const payload = got.body as Record<string, unknown>;
    if (payload.task_id !== taskId) {
      throw new Error(
        `task_id mismatch: callback=${String(payload.task_id)} expected=${taskId}`,
      );
    }
    if (payload.status !== "done" && payload.status !== "failed") {
      throw new Error(
        `expected terminal status (done|failed), got ${String(payload.status)}`,
      );
    }
    if (payload.kind !== "implement_feature") {
      throw new Error(`unexpected kind: ${String(payload.kind)}`);
    }
    for (const field of [
      "workspace_dir",
      "goal",
      "result_json",
      "error",
      "terminated_at",
    ]) {
      if (!(field in payload)) {
        throw new Error(`callback payload missing field: ${field}`);
      }
    }
    if (typeof payload.terminated_at !== "number") {
      throw new Error(
        `terminated_at should be a number, got ${typeof payload.terminated_at}`,
      );
    }

    await client.close();
    console.log("PASS — callback fired exactly once with the expected shape");
  } finally {
    if (child) {
      child.kill("SIGTERM");
      await new Promise((r) => setTimeout(r, 500));
    }
    callback.server.close();
    rmSync(workspaceDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error("FAIL:", err);
  process.exit(1);
});
