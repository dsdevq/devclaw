/**
 * Per-task docker sandbox runner.
 *
 * Spawns `docker run --rm` against the devclaw-sandbox image for each task.
 * The container's ENTRYPOINT runs the python OpenHands runner, which streams
 * one prefixed JSON line per event (`event: {...}`) plus a single terminating
 * line (`result: {...}`). This module:
 *
 *   - Translates an `OpenHandsRequest` into a docker invocation.
 *   - Bind-mounts the host workspace into /workspace and ~/.claude read-only
 *     into /home/agent/.claude (Pro OAuth posture: claude CLI inside sandbox
 *     can read tokens but not write back).
 *   - Streams stdout line-by-line; routes `event:` lines through `onEvent`
 *     and parses the final `result:` line as the OpenHandsResult.
 *   - Refuses to forward ANTHROPIC_API_KEY into the container (same belt
 *     +suspenders the python runner enforces).
 *
 * Container lifecycle: --rm + the per-task --name make destroy-on-exit
 * automatic. No persistent on-host state.
 *
 * This file replaces openhands-runner's "spawn python directly on host" path
 * for production. Tests still inject a stub runner so they don't need docker.
 */

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { resolve } from "node:path";

/**
 * Inputs the runner needs to launch one OpenHands run. Same kinds the MCP
 * tool surface exposes; the runner.py wrapper picks the right system-prompt
 * template per kind.
 */
export type OpenHandsRequest = {
  kind: "implement_feature" | "fix_bug" | "review_repository";
  workspaceDir: string;
  goal: string;
};

/**
 * Terminal verdict from one run. Mirrors the `result: {...}` line shape that
 * runner.py emits — fields are populated on success, plus `agent_output`
 * for debugging when present.
 */
export type OpenHandsResult =
  | {
      status: "ok";
      workspaceDir: string;
      message: string;
      agent_output?: string;
    }
  | { status: "error"; error: string; trace?: string };

/**
 * Event handed to the caller's `onEvent` for every `event:` line the runner
 * emits. Mirrors the python `_emit_event` payload shape verbatim.
 */
export type RunnerEvent = {
  id: string | null;
  type: string;
  source: string;
  ts: number | string;
  payload: unknown;
};

export type SandcastleRunRequest = OpenHandsRequest & {
  onEvent?: (event: RunnerEvent) => void;
};

const SANDBOX_IMAGE =
  process.env["DEVCLAW_SANDBOX_IMAGE"] ?? "devclaw-sandbox:latest";
const DOCKER_BIN = process.env["DEVCLAW_DOCKER_BIN"] ?? "docker";
// Container-side mount targets. Match Dockerfile expectations.
const CONTAINER_WORKSPACE = "/workspace";
const CONTAINER_CLAUDE_DIR = "/home/agent/.claude";

class SandcastleRunnerError extends Error {
  constructor(message: string, public readonly trace?: string) {
    super(message);
    this.name = "SandcastleRunnerError";
  }
}

/**
 * Run one task inside a fresh sandbox container. Resolves with the same
 * shape as `runOpenHands` so it's a drop-in for TaskQueue.
 */
export async function runSandcastle(
  req: SandcastleRunRequest,
): Promise<OpenHandsResult> {
  const claudeDir =
    process.env["DEVCLAW_HOST_CLAUDE_DIR"] ?? resolve(homedir(), ".claude");
  if (!existsSync(claudeDir)) {
    return {
      status: "error",
      error:
        `host claude dir not found at ${claudeDir} — set DEVCLAW_HOST_CLAUDE_DIR ` +
        `to override or mount your Claude Code config into the devclaw container.`,
    };
  }

  // When devclaw-mcp itself runs in a container and spawns docker on the
  // host socket, the workspace path it sees internally (e.g.
  // /var/lib/devclaw/workspaces/<id>) is not the same as the host's view of
  // that bind-mounted directory (e.g. /srv/devclaw/workspaces/<id>). Docker
  // needs the host path. The path-prefix env pair tells us how to translate.
  // Unset → pass through (typical local dev where we run directly on host).
  const hostBindPath = translateWorkspacePath(req.workspaceDir);

  // Per-task container name for greppable logs + manual cleanup if --rm
  // somehow fails (which it shouldn't, but operators appreciate the hook).
  const containerName = `devclaw-${randomUUID().slice(0, 8)}`;

  const payload = JSON.stringify({
    kind: req.kind,
    workspace_dir: CONTAINER_WORKSPACE,
    goal: req.goal,
  });

  const dockerArgs: string[] = [
    "run",
    "--rm",
    "--name",
    containerName,
    "--network",
    "host", // claude OAuth refresh needs egress; tighten later via allowlist.
    "-v",
    `${hostBindPath}:${CONTAINER_WORKSPACE}`,
    "-v",
    `${claudeDir}:${CONTAINER_CLAUDE_DIR}:ro`,
    "-e",
    "OPENHANDS_SUPPRESS_BANNER=1",
    SANDBOX_IMAGE,
    payload,
  ];

  return new Promise<OpenHandsResult>((resolveResult) => {
    const child = spawn(DOCKER_BIN, dockerArgs, {
      stdio: ["ignore", "pipe", "pipe"],
      env: stripApiKeys(process.env),
    });

    let stdoutBuf = "";
    let stderrBuf = "";
    let result: OpenHandsResult | null = null;

    child.stdout.on("data", (chunk: Buffer) => {
      stdoutBuf += chunk.toString("utf8");
      // Drain complete lines. Anything past the last newline stays buffered.
      const lines = stdoutBuf.split("\n");
      stdoutBuf = lines.pop() ?? "";
      for (const raw of lines) {
        const line = raw.trim();
        if (!line) continue;
        if (line.startsWith("event: ")) {
          if (req.onEvent) {
            try {
              const ev = JSON.parse(line.slice("event: ".length)) as RunnerEvent;
              req.onEvent(ev);
            } catch (parseErr) {
              process.stderr.write(
                `sandcastle-runner: dropping malformed event line: ${(parseErr as Error).message}\n`,
              );
            }
          }
        } else if (line.startsWith("result: ")) {
          // First result line wins; ignore anything after.
          if (result === null) {
            try {
              result = JSON.parse(line.slice("result: ".length)) as OpenHandsResult;
            } catch (parseErr) {
              result = {
                status: "error",
                error: `runner emitted unparsable result: ${(parseErr as Error).message}`,
                trace: line,
              };
            }
          }
        }
        // Everything else is sandbox decorative output — drop.
      }
    });

    child.stderr.on("data", (chunk: Buffer) => {
      stderrBuf += chunk.toString("utf8");
    });

    child.on("error", (err) => {
      resolveResult({
        status: "error",
        error:
          `failed to spawn ${DOCKER_BIN}: ${err.message}. ` +
          `Is docker installed and the socket reachable from this process?`,
      });
    });

    child.on("close", (code) => {
      if (result) {
        resolveResult(result);
        return;
      }
      resolveResult({
        status: "error",
        error:
          `sandbox exited ${code} without a result line. ` +
          `stderr tail:\n${stderrBuf.slice(-1024)}`,
      });
    });
  });
}

function translateWorkspacePath(workspaceDir: string): string {
  const containerPrefix = process.env["DEVCLAW_CONTAINER_PATH_PREFIX"];
  const hostPrefix = process.env["DEVCLAW_HOST_PATH_PREFIX"];
  if (
    containerPrefix &&
    hostPrefix &&
    workspaceDir.startsWith(containerPrefix)
  ) {
    return hostPrefix + workspaceDir.slice(containerPrefix.length);
  }
  return workspaceDir;
}

function stripApiKeys(
  env: NodeJS.ProcessEnv,
): NodeJS.ProcessEnv {
  const clean: NodeJS.ProcessEnv = { ...env };
  delete clean["ANTHROPIC_API_KEY"];
  delete clean["ANTHROPIC_AUTH_TOKEN"];
  return clean;
}

export { SandcastleRunnerError };
