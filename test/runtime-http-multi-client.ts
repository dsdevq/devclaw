/**
 * Regression test for the streamable-http transport.
 *
 * Reproduces the bug that blocked openclaw-gateway's MCP bundle loader:
 * the v2 server was running in stateful mode (sessionIdGenerator set) with
 * a single shared transport. The first client's initialize was accepted;
 * every subsequent client got HTTP 400 "Server already initialized" /
 * "Mcp-Session-Id header is required".
 *
 * This test boots devclaw-mcp in http mode and connects TWO independent
 * MCP clients sequentially, plus one raw `fetch` to verify bare HTTP
 * initialize still works. Pre-fix this test fails on the second client.
 * Post-fix (stateless mode) all three should succeed.
 *
 * Run with: `npx tsx test/runtime-http-multi-client.ts`
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { spawn, ChildProcess } from "node:child_process";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

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

async function connectAndListTools(
  url: URL,
  clientName: string,
): Promise<string[]> {
  const transport = new StreamableHTTPClientTransport(url);
  const client = new Client(
    { name: clientName, version: "0.0.1" },
    { capabilities: {} },
  );
  await client.connect(transport);
  const tools = await client.listTools();
  await client.close();
  return tools.tools.map((t) => t.name);
}

async function rawInitialize(url: URL): Promise<void> {
  // Mirrors what a non-SDK client (or a curl probe) sends. Pre-fix this
  // returned 400 after the first SDK client had already taken the session.
  const body = {
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2025-03-26",
      capabilities: {},
      clientInfo: { name: "raw-fetch-probe", version: "0.0.1" },
    },
  };
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      // streamable-http requires both content types in Accept
      accept: "application/json, text/event-stream",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "<unreadable>");
    throw new Error(
      `raw initialize POST got ${res.status} ${res.statusText}: ${text}`,
    );
  }
}

async function main(): Promise<void> {
  const workspaceDir = mkdtempSync(`${tmpdir()}/devclaw-v2-multi-`);
  const port = 8766;
  const url = new URL(`http://127.0.0.1:${port}/mcp`);
  console.log(`workspace: ${workspaceDir}, port: ${port}`);

  const serverEntry = resolve(__dirname, "..", "src", "mcp-server.ts");
  let child: ChildProcess | null = null;

  try {
    child = spawn("npx", ["tsx", serverEntry], {
      env: {
        ...process.env,
        DEVCLAW_TRANSPORT: "http",
        DEVCLAW_PORT: String(port),
        // Bind loopback for the test to avoid surprising the host firewall.
        DEVCLAW_HOST: "127.0.0.1",
        DEVCLAW_DB: `${workspaceDir}/devclaw.db`,
      } as Record<string, string>,
      stdio: ["ignore", "inherit", "inherit"],
    });

    await waitForHealth(port);

    // Client 1: connect, list, close.
    console.log("client 1: connecting…");
    const tools1 = await connectAndListTools(url, "devclaw-multi-client-1");
    console.log(`client 1 tools: ${tools1.join(", ")}`);
    if (!tools1.includes("implement_feature")) {
      throw new Error("client 1: implement_feature missing");
    }

    // Client 2: this is the path that broke pre-fix. The shared singleton
    // transport in stateful mode would 400 on a second initialize.
    console.log("client 2: connecting (this is the regression check)…");
    const tools2 = await connectAndListTools(url, "devclaw-multi-client-2");
    console.log(`client 2 tools: ${tools2.join(", ")}`);
    if (!tools2.includes("implement_feature")) {
      throw new Error("client 2: implement_feature missing");
    }

    // Raw HTTP probe — covers non-SDK clients (and curl).
    console.log("raw fetch initialize…");
    await rawInitialize(url);
    console.log("raw initialize: ok");

    console.log("PASS — multiple clients can initialize independently");
  } finally {
    if (child) {
      child.kill("SIGTERM");
      await new Promise((r) => setTimeout(r, 500));
    }
    rmSync(workspaceDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error("FAIL:", err);
  process.exit(1);
});
