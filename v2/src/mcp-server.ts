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
import { resolve } from "node:path";

import { StateStore } from "./state-store.js";
import { TaskQueue } from "./task-queue.js";

const SERVER_NAME = "devclaw";
const SERVER_VERSION = "0.0.4";

const DB_PATH = resolve(process.cwd(), process.env["DEVCLAW_DB"] ?? "devclaw.db");
const TRANSPORT = process.env["DEVCLAW_TRANSPORT"] ?? "stdio";
const HTTP_PORT = Number(process.env["DEVCLAW_PORT"] ?? "8000");
// Bind address. Defaults to 0.0.0.0 so sibling containers in the same compose
// network (e.g. openclaw-gateway) can reach the MCP endpoint. Set
// DEVCLAW_HOST=127.0.0.1 to restrict to the host loopback.
const HTTP_HOST = process.env["DEVCLAW_HOST"] ?? "0.0.0.0";

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
          "task runs asynchronously. Poll get_status(task_id) for completion, " +
          "or pass notify_url to be pushed the result instead of polling. " +
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
            notify_url: {
              type: "string",
              description:
                "Optional. URL to POST the task row (same shape as " +
                "get_status returns) to once the task reaches a terminal " +
                "state (done | failed). Bounded retries (1s/2s/4s) on " +
                "non-2xx or network error. Use this to avoid polling.",
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
          "confirm the fix. Returns task_id immediately. Accepts the same " +
          "optional notify_url as implement_feature.",
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
            notify_url: {
              type: "string",
              description:
                "Optional. URL to POST the final task row to. See " +
                "implement_feature for full semantics.",
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
          "agent_output field once status=done. Accepts the same optional " +
          "notify_url as implement_feature.",
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
            notify_url: {
              type: "string",
              description:
                "Optional. URL to POST the final task row to. See " +
                "implement_feature for full semantics.",
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
        name: "start_program",
        description:
          "Submit a high-level coding goal that DevClaw should decompose " +
          "into a DAG of smaller OpenHands tasks. The planner (a Claude " +
          "subprocess) writes the plan, then tasks execute in dep order " +
          "with bounded parallelism. Returns a program_id immediately; " +
          "poll get_program(program_id) to inspect progress or pass " +
          "notify_url to be pushed the final result when the whole " +
          "program terminates. Use this for goals too large for one " +
          "implement_feature call (e.g. 'scaffold a new service with CI " +
          "and a smoke test'). For small bounded goals, implement_feature " +
          "/ fix_bug / review_repository are still the right tools.",
        inputSchema: {
          type: "object",
          properties: {
            workspace_dir: {
              type: "string",
              description:
                "Absolute path to the workspace OpenHands will work in. " +
                "Shared by every task in this program.",
            },
            goal: {
              type: "string",
              description:
                "Natural-language description of the overall goal. The " +
                "planner will decide whether to decompose it.",
            },
            notify_url: {
              type: "string",
              description:
                "Optional. URL to POST the program row (with embedded " +
                "task rows) to once the program reaches a terminal state " +
                "(done | failed). Bounded retries (1s/2s/4s). No " +
                "per-task callbacks fire — only this one.",
            },
          },
          required: ["workspace_dir", "goal"],
          additionalProperties: false,
        },
      },
      {
        name: "get_program",
        description:
          "Return a program row and all its tasks in dependency order. " +
          "Use to poll the state of a program submitted via start_program.",
        inputSchema: {
          type: "object",
          properties: {
            program_id: {
              type: "string",
              description: "Program id returned by start_program.",
            },
          },
          required: ["program_id"],
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
        const notifyUrl =
          typeof args["notify_url"] === "string" && args["notify_url"]
            ? (args["notify_url"] as string)
            : null;
        if (!workspaceDir || !goal) {
          throw new Error("implement_feature requires workspace_dir and goal");
        }
        const { taskId } = queue.submit({
          kind: "implement_feature",
          workspaceDir,
          goal,
          notifyUrl,
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
        const notifyUrl =
          typeof args["notify_url"] === "string" && args["notify_url"]
            ? (args["notify_url"] as string)
            : null;
        if (!workspaceDir || !description) {
          throw new Error("fix_bug requires workspace_dir and description");
        }
        const { taskId } = queue.submit({
          kind: "fix_bug",
          workspaceDir,
          goal: description,
          notifyUrl,
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
        const notifyUrl =
          typeof args["notify_url"] === "string" && args["notify_url"]
            ? (args["notify_url"] as string)
            : null;
        if (!workspaceDir) {
          throw new Error("review_repository requires workspace_dir");
        }
        const { taskId } = queue.submit({
          kind: "review_repository",
          workspaceDir,
          goal: focus,
          notifyUrl,
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

      case "start_program": {
        const workspaceDir = String(args["workspace_dir"] ?? "");
        const goal = String(args["goal"] ?? "");
        const notifyUrl =
          typeof args["notify_url"] === "string" && args["notify_url"]
            ? (args["notify_url"] as string)
            : null;
        if (!workspaceDir || !goal) {
          throw new Error("start_program requires workspace_dir and goal");
        }
        const { programId } = queue.submitProgram({
          workspaceDir,
          goal,
          notifyUrl,
        });
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify(
                { program_id: programId, status: "planning" },
                null,
                2,
              ),
            },
          ],
          isError: false,
        };
      }

      case "get_program": {
        const programId = String(args["program_id"] ?? "");
        const program = store.getProgram(programId);
        if (!program) {
          return {
            content: [
              {
                type: "text" as const,
                text: JSON.stringify({ error: `unknown program_id: ${programId}` }),
              },
            ],
            isError: true,
          };
        }
        const tasks = store.listProgramTasks(programId);
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify({ program, tasks }, null, 2),
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
  // Streamable HTTP transport, stateless, **new Server + transport per
  // request**. Two SDK invariants force this exact shape:
  //
  //  1. Stateful mode (sessionIdGenerator set) with a shared transport
  //     rejects the second client's initialize with HTTP 400
  //     "Server already initialized" (webStandardStreamableHttp.js:425),
  //     and any non-initialize call without the original Mcp-Session-Id
  //     with "Mcp-Session-Id header is required" (line 599). That was
  //     the original bug — openclaw-gateway hit it on every reconnect.
  //
  //  2. Stateless mode (sessionIdGenerator undefined) is the obvious
  //     alternative, but the SDK explicitly forbids reusing a stateless
  //     transport across requests:
  //
  //       // webStandardStreamableHttp.js:136-141
  //       if (!this.sessionIdGenerator && this._hasHandledRequest) {
  //         throw new Error('Stateless transport cannot be reused …');
  //       }
  //
  //     The throw surfaces through hono's getRequestListener as a silent
  //     HTTP 500 on the 2nd POST of any client (e.g. the
  //     notifications/initialized that the SDK client sends right after
  //     initialize). So a singleton-stateless server is also broken.
  //
  // Therefore: new Server + new transport per request, stateless mode.
  // Matches the SDK's own example (simpleStatelessStreamableHttp.js).
  // Per-request cost is negligible — `store` and `queue` are module-level
  // so SQLite + in-flight task state stay shared; only the protocol
  // framing objects are recreated.

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

    let server: Server | undefined;
    let transport: StreamableHTTPServerTransport | undefined;
    try {
      server = buildServer();
      transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined,
      });
      await server.connect(transport);

      // Tear down per-request server/transport when the response closes.
      // Avoids leaking abort/protocol listeners across requests.
      httpRes.on("close", () => {
        transport?.close().catch(() => {});
        server?.close().catch(() => {});
      });

      await transport.handleRequest(httpReq, httpRes);
    } catch (err) {
      process.stderr.write(`http handler error: ${(err as Error).message}\n`);
      if (!httpRes.headersSent) {
        httpRes.writeHead(500);
        httpRes.end();
      }
      transport?.close().catch(() => {});
      server?.close().catch(() => {});
    }
  });

  await new Promise<void>((resolveListen) => {
    httpServer.listen(HTTP_PORT, HTTP_HOST, () => {
      process.stderr.write(
        `${SERVER_NAME} v${SERVER_VERSION} ready (http://${HTTP_HOST}:${HTTP_PORT}/mcp, db=${DB_PATH})\n`,
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
