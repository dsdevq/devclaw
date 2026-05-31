/**
 * Smoke test for slice 1: spawn the MCP server, call `implement_feature` with
 * a tiny goal, verify a file gets created.
 *
 * Run with: `npx tsx test/client.ts`
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { existsSync, readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

async function main(): Promise<void> {
  const workspaceDir = mkdtempSync(`${tmpdir()}/devclaw-v2-smoke-`);
  console.log(`workspace: ${workspaceDir}`);

  const serverEntry = resolve(__dirname, "..", "src", "mcp-server.ts");
  // Use tsx to avoid requiring a build step before running the test.
  const transport = new StdioClientTransport({
    command: "npx",
    args: ["tsx", serverEntry],
  });

  const client = new Client(
    { name: "devclaw-smoke", version: "0.0.1" },
    { capabilities: {} },
  );

  console.log("connecting…");
  await client.connect(transport);

  console.log("listing tools…");
  const tools = await client.listTools();
  console.log(`tools: ${tools.tools.map((t) => t.name).join(", ")}`);
  if (!tools.tools.some((t) => t.name === "implement_feature")) {
    throw new Error("implement_feature tool not exposed");
  }

  const goal =
    "Create a file named hello.txt in the current working directory with " +
    "exactly this content (no extra newlines, no trailing whitespace beyond " +
    "the single line):\n\nHello from DevClaw v2 slice 1";

  console.log("calling implement_feature…");
  const t0 = Date.now();
  const result = await client.callTool({
    name: "implement_feature",
    arguments: { workspace_dir: workspaceDir, goal },
  });
  const dt = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`tool returned in ${dt}s, isError=${result.isError}`);
  console.log(
    "tool response:",
    (result.content as Array<{ type: string; text?: string }>)
      .map((c) => c.text ?? "")
      .join("\n"),
  );

  const hello = resolve(workspaceDir, "hello.txt");
  if (!existsSync(hello)) {
    throw new Error(
      `hello.txt was NOT created — chain broken. workspace=${workspaceDir}`,
    );
  }
  const content = readFileSync(hello, "utf8");
  console.log("hello.txt content:", JSON.stringify(content));

  await client.close();
  console.log("PASS — chain verified end-to-end");

  // Clean up — comment this line if you want to inspect the workspace.
  rmSync(workspaceDir, { recursive: true, force: true });
}

main().catch((err) => {
  console.error("FAIL:", err);
  process.exit(1);
});
