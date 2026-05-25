#!/usr/bin/env node
/**
 * DevClaw v2 — MCP server (slice 2: async + state + http).
 *
 * Tools:
 *   - implement_feature(workspace_dir, goal) → { task_id }   (async, returns immediately)
 *   - get_status(task_id)                    → Task row from state store
 *   - list_tasks({status?, limit?})          → Task rows
 *
 * Transport:
 *   - DEVCLAW_TRANSPORT=stdio  (default) — for local dev + the existing smoke/runtime tests
 *   - DEVCLAW_TRANSPORT=http             — streamable-http on $DEVCLAW_PORT (default 8000)
 *
 * State:
 *   - SQLite at $DEVCLAW_DB (default ./devclaw.db)
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { resolve } from "node:path";

import { StateStore } from "./state-store.js";
import { TaskQueue } from "./task-queue.js";

const SERVER_NAME = "devclaw";
const SERVER_VERSION = "0.0.3";

const DB_PATH = resolve(process.cwd(), process.env["DEVCLAW_DB"] ?? "devclaw.db");
const TRANSPORT = process.env["DEVCLAW_TRANSPORT"] ?? "stdio";
const HTTP_PORT = Number(process.env["DEVCLAW_PORT"] ?? "8000");

const store = new StateStore(DB_PATH);
const queue = new TaskQueue(store);

function buildServer(): Server {
  const server = new Server(
    { name: SERVER_NAME, version: SERVER_VERSION },
    { capabilities: { tools: {} } },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: [
      {
        name: "implement_feature",
        description:
          "Submit a natural-language coding goal to be executed by OpenHands " +
          "in the given workspace_dir. Returns a task_id immediately; the " +
          "task runs asynchronously. Poll get_status(task_id) for completion. " +
          "Use this for new features or open-ended changes; prefer fix_bug " +
          "when the user describes an existing defect, and review_repository " +
          "when only a read-only review is wanted.",
        inputSchema: {
          type: "object",
          properties: {
            workspace_dir: {
              type: "string",
              description:
                "Absolute path to the workspace OpenHands will work in. " +
                "Created if missing. Treat as the agent's cwd.",
            },
            goal: {
              type: "string",
              description: "Natural-language description of what to do.",
            },
          },
          required: ["workspace_dir", "goal"],
          additionalProperties: false,
        },
      },
      {
        name: "fix_bug",
        description:
          "Submit a bug-fix task. Like implement_feature, but with a " +
          "specialized prompt that biases OpenHands toward: reading existing " +
          "code first, making the smallest change that fixes the bug, NOT " +
          "refactoring unrelated code, and running the project's tests to " +
          "confirm the fix. Returns task_id immediately.",
        inputSchema: {
          type: "object",
          properties: {
            workspace_dir: { type: "string", description: "Workspace path." },
            description: {
              type: "string",
              description:
                "Bug description — what's broken, where if known, what " +
                "should happen instead.",
            },
          },
          required: ["workspace_dir", "description"],
          additionalProperties: false,
        },
      },
      {
        name: "review_repository",
        description:
          "Submit a READ-ONLY code review task. OpenHands inspects the " +
          "workspace and writes a review report; it is prompt-instructed " +
          "NOT to modify, create, or delete any files. Returns task_id " +
          "immediately; final report appears in the task's result_json " +
          "agent_output field once status=done.",
        inputSchema: {
          type: "object",
          properties: {
            workspace_dir: { type: "string", description: "Workspace path." },
            focus: {
              type: "string",
              description:
                "Optional focus area for the review (e.g. 'security', " +
                "'error handling', 'test coverage'). Leave empty for a " +
                "general review.",
              default: "",
            },
          },
          required: ["workspace_dir"],
          additionalProperties: false,
        },
      },
      {
        name: "get_status",
        description:
          "Return the current status + (when terminated) the result or error " +
          "of a task submitted via implement_feature. Status values: " +
          "pending | running | done | failed.",
        inputSchema: {
          type: "object",
          properties: {
            task_id: { type: "string", description: "Task id from implement_feature." },
          },
          required: ["task_id"],
          additionalProperties: false,
        },
      },
      {
        name: "list_tasks",
        description:
          "List recent tasks, most-recent first. Optionally filter by status or kind.",
        inputSchema: {
          type: "object",
          properties: {
            status: {
              type: "string",
              enum: ["pending", "running", "done", "failed"],
            },
            kind: {
              type: "string",
              enum: ["implement_feature", "fix_bug", "review_repository"],
            },
            limit: { type: "number", default: 20, minimum: 1, maximum: 1000 },
          },
          additionalProperties: false,
        },
      },
    ],
  }));

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const args = (req.params.arguments ?? {}) as Record<string, unknown>;

    switch (req.params.name) {
      case "implement_feature": {
        const workspaceDir = String(args["workspace_dir"] ?? "");
        const goal = String(args["goal"] ?? "");
        if (!workspaceDir || !goal) {
          throw new Error("implement_feature requires workspace_dir and goal");
        }
        const { taskId } = queue.submit({
          kind: "implement_feature",
          workspaceDir,
          goal,
        });
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify({ task_id: taskId, status: "pending" }, null, 2),
            },
          ],
          isError: false,
        };
      }

      case "fix_bug": {
        const workspaceDir = String(args["workspace_dir"] ?? "");
        const description = String(args["description"] ?? "");
        if (!workspaceDir || !description) {
          throw new Error("fix_bug requires workspace_dir and description");
        }
        const { taskId } = queue.submit({
          kind: "fix_bug",
          workspaceDir,
          goal: description,
        });
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify({ task_id: taskId, status: "pending" }, null, 2),
            },
          ],
          isError: false,
        };
      }

      case "review_repository": {
        const workspaceDir = String(args["workspace_dir"] ?? "");
        const focus = String(args["focus"] ?? "general code review");
        if (!workspaceDir) {
          throw new Error("review_repository requires workspace_dir");
        }
        const { taskId } = queue.submit({
          kind: "review_repository",
          workspaceDir,
          goal: focus,
        });
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify({ task_id: taskId, status: "pending" }, null, 2),
            },
          ],
          isError: false,
        };
      }

      case "get_status": {
        const taskId = String(args["task_id"] ?? "");
        const task = store.getTask(taskId);
        if (!task) {
          return {
            content: [
              {
                type: "text" as const,
                text: JSON.stringify({ error: `unknown task_id: ${taskId}` }),
              },
            ],
            isError: true,
          };
        }
        return {
          content: [
            { type: "text" as const, text: JSON.stringify(task, null, 2) },
          ],
          isError: false,
        };
      }

      case "list_tasks": {
        const status = args["status"] as
          | "pending"
          | "running"
          | "done"
          | "failed"
          | undefined;
        const kind = args["kind"] as
          | "implement_feature"
          | "fix_bug"
          | "review_repository"
          | undefined;
        const limit =
          typeof args["limit"] === "number" ? (args["limit"] as number) : 20;
        const tasks = store.listTasks({ status, kind, limit });
        return {
          content: [
            { type: "text" as const, text: JSON.stringify(tasks, null, 2) },
          ],
          isError: false,
        };
      }

      default:
        throw new Error(`Unknown tool: ${req.params.name}`);
    }
  });

  return server;
}

async function runStdio(): Promise<void> {
  const server = buildServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
  process.stderr.write(`${SERVER_NAME} v${SERVER_VERSION} ready (stdio, db=${DB_PATH})\n`);
}

async function runHttp(): Promise<void> {
  // Streamable HTTP transport. The MCP SDK handles the JSON-RPC + SSE
  // framing; we provide the underlying Node http server.
  //
  // One server instance + one transport instance per process is the simple
  // path. The transport handles concurrent requests via sessionId routing.
  const server = buildServer();

  // sessionIdGenerator: undefined disables stateful sessions — every request
  // is independent. Suits our short-lived MCP tool calls; revisit if Kit
  // needs streaming responses.
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: () => randomUUID(),
  });

  await server.connect(transport);

  const httpServer = createServer(async (httpReq, httpRes) => {
    if (httpReq.url === "/health") {
      httpRes.writeHead(200, { "content-type": "application/json" });
      httpRes.end(JSON.stringify({ ok: true, name: SERVER_NAME, version: SERVER_VERSION }));
      return;
    }

    if (!httpReq.url?.startsWith("/mcp")) {
      httpRes.writeHead(404);
      httpRes.end();
      return;
    }

    // The transport handles MCP protocol on /mcp.
    try {
      await transport.handleRequest(httpReq, httpRes);
    } catch (err) {
      process.stderr.write(`http handler error: ${(err as Error).message}\n`);
      if (!httpRes.headersSent) {
        httpRes.writeHead(500);
        httpRes.end();
      }
    }
  });

  await new Promise<void>((resolveListen) => {
    httpServer.listen(HTTP_PORT, "127.0.0.1", () => {
      process.stderr.write(
        `${SERVER_NAME} v${SERVER_VERSION} ready (http://127.0.0.1:${HTTP_PORT}/mcp, db=${DB_PATH})\n`,
      );
      resolveListen();
    });
  });

  // Graceful shutdown
  const shutdown = (sig: string) => {
    process.stderr.write(`received ${sig}, shutting down\n`);
    httpServer.close(() => {
      store.close();
      process.exit(0);
    });
  };
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}

async function main(): Promise<void> {
  if (TRANSPORT === "http") {
    await runHttp();
  } else if (TRANSPORT === "stdio") {
    await runStdio();
  } else {
    throw new Error(
      `Unknown DEVCLAW_TRANSPORT=${TRANSPORT}; expected "stdio" or "http"`,
    );
  }
}

main().catch((err) => {
  process.stderr.write(`fatal: ${(err as Error).message}\n`);
  process.exit(1);
});
