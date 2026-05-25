#!/usr/bin/env node
/**
 * DevClaw v2 — MCP server (stdio transport, slice 1).
 *
 * Exposes one tool — `implement_feature` — that hands a natural-language
 * goal to OpenHands (via the Python runner subprocess) and returns when
 * OpenHands finishes. Synchronous in this slice; async + state store +
 * notify_url come in slice 2.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { runOpenHands, OpenHandsRunnerError } from "./openhands-runner.js";

const SERVER_NAME = "devclaw";
const SERVER_VERSION = "0.0.1";

const server = new Server(
  { name: SERVER_NAME, version: SERVER_VERSION },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "implement_feature",
      description:
        "Hand a natural-language coding goal to OpenHands. OpenHands runs " +
        "autonomously in the given workspace_dir, writing files, running " +
        "commands, and making git operations as needed. Synchronous — " +
        "returns when OpenHands finishes. For local goals that don't need " +
        "git/PR work, point workspace_dir at a scratch dir.",
      inputSchema: {
        type: "object",
        properties: {
          workspace_dir: {
            type: "string",
            description:
              "Absolute path to the workspace dir OpenHands will work in. " +
              "Created if it doesn't exist. Treat as the agent's cwd.",
          },
          goal: {
            type: "string",
            description:
              "Natural-language description of what OpenHands should do.",
          },
        },
        required: ["workspace_dir", "goal"],
        additionalProperties: false,
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name !== "implement_feature") {
    throw new Error(`Unknown tool: ${req.params.name}`);
  }

  const args = req.params.arguments ?? {};
  const workspaceDir = String(args["workspace_dir"] ?? "");
  const goal = String(args["goal"] ?? "");
  if (!workspaceDir || !goal) {
    throw new Error("implement_feature requires workspace_dir and goal");
  }

  try {
    const result = await runOpenHands({ workspaceDir, goal });
    return {
      content: [
        {
          type: "text" as const,
          text: JSON.stringify(result, null, 2),
        },
      ],
      isError: result.status === "error",
    };
  } catch (err) {
    const e = err as OpenHandsRunnerError;
    return {
      content: [
        {
          type: "text" as const,
          text: JSON.stringify(
            { status: "error", error: e.message, trace: e.trace },
            null,
            2,
          ),
        },
      ],
      isError: true,
    };
  }
});

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // Server runs until stdin closes; logging goes to stderr so we don't pollute
  // the JSON-RPC framing on stdout.
  process.stderr.write(`${SERVER_NAME} v${SERVER_VERSION} ready (stdio)\n`);
}

main().catch((err) => {
  process.stderr.write(`fatal: ${(err as Error).message}\n`);
  process.exit(1);
});
