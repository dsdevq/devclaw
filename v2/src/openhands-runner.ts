/**
 * Spawns the Python OpenHands runner as a subprocess. Returns the parsed
 * runner response. Keeps OpenHands invocation isolated from the MCP server's
 * main process so a crash in the agent loop can't take down DevClaw itself.
 */

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// src/openhands-runner.ts (dev) or dist/openhands-runner.js (built) both
// sit one level above v2/python-runner/.
const PYTHON_RUNNER_DIR = resolve(__dirname, "..", "python-runner");

export type OpenHandsRequest = {
  kind: "implement_feature" | "fix_bug" | "review_repository";
  workspaceDir: string;
  goal: string;
};

export type OpenHandsResult =
  | { status: "ok"; workspaceDir: string; message: string }
  | { status: "error"; error: string; trace?: string };

export class OpenHandsRunnerError extends Error {
  constructor(message: string, public readonly trace?: string) {
    super(message);
    this.name = "OpenHandsRunnerError";
  }
}

export async function runOpenHands(
  req: OpenHandsRequest,
): Promise<OpenHandsResult> {
  const venvPython = resolve(PYTHON_RUNNER_DIR, ".venv", "bin", "python3");
  const runnerScript = resolve(PYTHON_RUNNER_DIR, "runner.py");

  const payload = JSON.stringify({
    kind: req.kind,
    workspace_dir: req.workspaceDir,
    goal: req.goal,
  });

  return new Promise<OpenHandsResult>((resolveResult, rejectResult) => {
    // Refuse to inherit API keys — same belt + suspenders as the Python runner.
    const env: NodeJS.ProcessEnv = { ...process.env };
    delete env.ANTHROPIC_API_KEY;
    delete env.ANTHROPIC_AUTH_TOKEN;

    const child = spawn(venvPython, [runnerScript, payload], {
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString();
    });

    child.on("error", (err) => {
      rejectResult(
        new OpenHandsRunnerError(
          `Failed to spawn Python runner at ${venvPython}: ${err.message}. ` +
            `Did you run 'npm run python:install'?`,
        ),
      );
    });

    child.on("close", (code) => {
      const trimmed = stdout.trim();
      if (!trimmed) {
        rejectResult(
          new OpenHandsRunnerError(
            `Python runner exited ${code} with no JSON output. stderr:\n${stderr}`,
          ),
        );
        return;
      }
      try {
        const parsed = JSON.parse(trimmed) as OpenHandsResult;
        resolveResult(parsed);
      } catch (parseErr) {
        rejectResult(
          new OpenHandsRunnerError(
            `Python runner returned non-JSON output. Exit ${code}. stdout:\n${stdout}\nstderr:\n${stderr}`,
            (parseErr as Error).message,
          ),
        );
      }
    });
  });
}
