/**
 * Planner — turns a single high-level goal into a DAG of OpenHands tasks.
 *
 * Cognition runs in Claude (we shell out to `claude --print`); the TS layer
 * only validates the JSON the model produces. Same split as the OpenHands
 * runner: mechanism here, decisions in Claude.
 *
 * The planner returns a topologically-ordered list of nodes. The caller
 * (TaskQueue.submitProgram) assigns UUIDs and remaps the model-supplied
 * keys to real task ids before persisting.
 *
 * Claude CLI shape:
 *   claude --print --output-format=text "<prompt>"
 * Auth comes from the bind-mounted ~/.claude session — no API key, ever.
 *
 * Single goals (the "small bounded" case from architecture-v2 §3.2) still
 * go through here: the planner returns a 1-element list with no deps. One
 * code path; less special-casing in the queue.
 */

import { spawn } from "node:child_process";

import type { TaskKind } from "./state-store.js";

const PLANNER_TIMEOUT_MS = Number(process.env["DEVCLAW_PLANNER_TIMEOUT_MS"] ?? "90000");
const CLAUDE_BIN = process.env["DEVCLAW_CLAUDE_BIN"] ?? "claude";
const MAX_TASKS_PER_PLAN = 20;

const VALID_KINDS: TaskKind[] = ["implement_feature", "fix_bug", "review_repository"];

export type PlannedTask = {
  /** Stable model-assigned id used to express deps within this plan only. */
  key: string;
  goal: string;
  kind: TaskKind;
  /** Keys (not UUIDs) of other tasks in this plan that must finish first. */
  dependsOnKeys: string[];
};

export class PlannerError extends Error {
  constructor(message: string, public readonly raw?: string) {
    super(message);
    this.name = "PlannerError";
  }
}

const SYSTEM_PROMPT = `You are DevClaw's planner. Decompose a single coding goal
into a directed acyclic graph (DAG) of smaller tasks that can each be executed
by an autonomous coding agent in one run.

Rules:
- Each task is bounded: an agent should finish it in one session.
- Prefer fewer, larger tasks over many tiny ones. Aim for 1–6 tasks. Use more
  only when the goal is genuinely large.
- If the goal is small (e.g. "fix a typo", "add a config flag"), return ONE task.
- Use "depends_on" for tasks that genuinely cannot start until another finishes
  (e.g. "frontend uses the API contract from task 1"). Don't invent fake deps.
- Independent tasks should have empty depends_on so they can run in parallel.
- Task "kind" must be one of: implement_feature, fix_bug, review_repository.
  Default to implement_feature unless the goal explicitly says fix a bug or
  review code without changing it.

Respond with STRICT JSON ONLY — no prose, no markdown fences. Schema:

{
  "tasks": [
    {
      "key": "<short stable id, e.g. 't1', 'scaffold'>",
      "goal": "<concrete instruction for the agent>",
      "kind": "implement_feature" | "fix_bug" | "review_repository",
      "depends_on": ["<key of another task in this plan>", ...]
    }
  ]
}`;

export function buildPlannerPrompt(goal: string, workspaceDir: string): string {
  return `${SYSTEM_PROMPT}

Workspace: ${workspaceDir}
Goal: ${goal}

Return the JSON now.`;
}

/**
 * Pull the first JSON object out of a model response. Tolerates leading
 * prose or markdown fences even though the prompt forbids them.
 */
export function extractJson(text: string): string {
  const trimmed = text.trim();
  if (trimmed.startsWith("{")) return trimmed;
  const fence = trimmed.match(/```(?:json)?\s*(\{[\s\S]*?\})\s*```/);
  if (fence && fence[1]) return fence[1];
  const first = trimmed.indexOf("{");
  const last = trimmed.lastIndexOf("}");
  if (first >= 0 && last > first) return trimmed.slice(first, last + 1);
  throw new PlannerError("No JSON object found in planner response", text);
}

/**
 * Validate the parsed plan and return tasks in topological order.
 * Throws PlannerError on cycles, dangling refs, missing fields, etc.
 */
export function validatePlan(parsed: unknown): PlannedTask[] {
  if (!parsed || typeof parsed !== "object") {
    throw new PlannerError("Plan must be a JSON object");
  }
  const obj = parsed as Record<string, unknown>;
  const raw = obj["tasks"];
  if (!Array.isArray(raw)) {
    throw new PlannerError("Plan.tasks must be an array");
  }
  if (raw.length === 0) {
    throw new PlannerError("Plan must contain at least one task");
  }
  if (raw.length > MAX_TASKS_PER_PLAN) {
    throw new PlannerError(
      `Plan has ${raw.length} tasks; max is ${MAX_TASKS_PER_PLAN}. Refine the goal.`,
    );
  }

  const seen = new Set<string>();
  const tasks: PlannedTask[] = [];
  for (const t of raw) {
    if (!t || typeof t !== "object") {
      throw new PlannerError("Each task must be an object");
    }
    const r = t as Record<string, unknown>;
    const key = typeof r["key"] === "string" ? (r["key"] as string).trim() : "";
    const goal = typeof r["goal"] === "string" ? (r["goal"] as string).trim() : "";
    const kindRaw = typeof r["kind"] === "string" ? (r["kind"] as string) : "implement_feature";
    const depsRaw = r["depends_on"];
    if (!key) throw new PlannerError("Task missing 'key'");
    if (!goal) throw new PlannerError(`Task '${key}' missing 'goal'`);
    if (seen.has(key)) throw new PlannerError(`Duplicate task key '${key}'`);
    if (!VALID_KINDS.includes(kindRaw as TaskKind)) {
      throw new PlannerError(
        `Task '${key}' has invalid kind '${kindRaw}'; expected one of ${VALID_KINDS.join(", ")}`,
      );
    }
    const dependsOnKeys: string[] = [];
    if (depsRaw != null) {
      if (!Array.isArray(depsRaw)) {
        throw new PlannerError(`Task '${key}' depends_on must be an array`);
      }
      for (const d of depsRaw) {
        if (typeof d !== "string" || !d.trim()) {
          throw new PlannerError(`Task '${key}' has non-string dep`);
        }
        if (d === key) {
          throw new PlannerError(`Task '${key}' depends on itself`);
        }
        dependsOnKeys.push(d.trim());
      }
    }
    seen.add(key);
    tasks.push({ key, goal, kind: kindRaw as TaskKind, dependsOnKeys });
  }

  // Validate all dep refs resolve.
  for (const t of tasks) {
    for (const d of t.dependsOnKeys) {
      if (!seen.has(d)) {
        throw new PlannerError(`Task '${t.key}' depends on unknown key '${d}'`);
      }
    }
  }

  // Kahn topological sort — also detects cycles.
  const byKey = new Map(tasks.map((t) => [t.key, t]));
  const indegree = new Map<string, number>();
  const dependents = new Map<string, string[]>();
  for (const t of tasks) {
    indegree.set(t.key, t.dependsOnKeys.length);
    for (const d of t.dependsOnKeys) {
      const list = dependents.get(d) ?? [];
      list.push(t.key);
      dependents.set(d, list);
    }
  }
  const ready: string[] = [];
  for (const [k, n] of indegree) if (n === 0) ready.push(k);
  ready.sort(); // deterministic order across runs

  const ordered: PlannedTask[] = [];
  while (ready.length) {
    const k = ready.shift() as string;
    ordered.push(byKey.get(k) as PlannedTask);
    const downstream = dependents.get(k) ?? [];
    for (const d of downstream) {
      const n = (indegree.get(d) ?? 0) - 1;
      indegree.set(d, n);
      if (n === 0) ready.push(d);
    }
    ready.sort();
  }
  if (ordered.length !== tasks.length) {
    throw new PlannerError("Plan contains a dependency cycle");
  }
  return ordered;
}

/**
 * Spawn `claude --print` with the planner prompt and return its stdout.
 * Exported so tests can stub it.
 */
export async function callClaude(prompt: string): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const env: NodeJS.ProcessEnv = { ...process.env };
    // Belt + suspenders: never let an API key on the process env override
    // the OAuth session in ~/.claude.
    delete env.ANTHROPIC_API_KEY;
    delete env.ANTHROPIC_AUTH_TOKEN;

    const child = spawn(
      CLAUDE_BIN,
      ["--print", "--output-format=text", prompt],
      { env, stdio: ["ignore", "pipe", "pipe"] },
    );

    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, PLANNER_TIMEOUT_MS);

    child.stdout.on("data", (b: Buffer) => {
      stdout += b.toString();
    });
    child.stderr.on("data", (b: Buffer) => {
      stderr += b.toString();
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(new PlannerError(`Failed to spawn ${CLAUDE_BIN}: ${err.message}`));
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (timedOut) {
        reject(
          new PlannerError(
            `claude --print timed out after ${PLANNER_TIMEOUT_MS}ms. stderr:\n${stderr}`,
          ),
        );
        return;
      }
      if (code !== 0) {
        reject(
          new PlannerError(
            `claude --print exited ${code}. stderr:\n${stderr}`,
            stdout,
          ),
        );
        return;
      }
      resolve(stdout);
    });
  });
}

/**
 * Full planner entry point: prompt Claude, validate, return ordered DAG.
 * `claudeCaller` is injected so tests can stub the subprocess.
 */
export async function planGoal(
  goal: string,
  workspaceDir: string,
  claudeCaller: (prompt: string) => Promise<string> = callClaude,
): Promise<PlannedTask[]> {
  const prompt = buildPlannerPrompt(goal, workspaceDir);
  const raw = await claudeCaller(prompt);
  const jsonText = extractJson(raw);
  let parsed: unknown;
  try {
    parsed = JSON.parse(jsonText);
  } catch (err) {
    throw new PlannerError(
      `Planner JSON parse failed: ${(err as Error).message}`,
      raw,
    );
  }
  return validatePlan(parsed);
}
