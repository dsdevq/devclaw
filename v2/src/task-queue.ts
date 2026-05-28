/**
 * In-process async task executor. The MCP handler calls submit() or
 * submitProgram() and returns immediately; OpenHands subprocesses run
 * in the background and the state store flips task rows when they settle.
 *
 * Single-writer-to-state by design — only this queue mutates task rows.
 *
 * Programs (DAGs):
 *   submitProgram() returns a program_id synchronously and:
 *     1. spawns the planner asynchronously,
 *     2. on success, creates tasks with depends_on remapped to UUIDs and
 *        schedules tasks whose deps are already satisfied,
 *     3. on planner failure marks the program failed and fires the
 *        program-level notify.
 *   When a child task settles, the queue:
 *     - schedules any sibling whose deps are now all 'done',
 *     - decrements the program's in-flight counter,
 *     - if no in-flight remain and the program has reached a terminal
 *       state (all done, or any failed with no further progress
 *       possible), marks the program done/failed and fires its notify.
 *
 * Failure policy: a single sibling failure makes the program "sticky
 * failed" — pending siblings will not start, in-flight siblings still
 * run to completion. Matches the architecture-v2 §4 description (DAG
 * walks forward; failures don't roll back, but don't propagate either).
 *
 * Notifications:
 *   - Standalone tasks (no program_id) fire their own notify_url on
 *     terminal state, with bounded retries (1s/2s/4s backoff).
 *   - Program-child tasks DO NOT fire per-task callbacks — only the
 *     program-level notify fires once the program terminates. This
 *     keeps the callback contract simple for callers: one program in,
 *     one notify out.
 */

import { randomUUID } from "node:crypto";

import { planGoal, PlannerError, type PlannedTask } from "./planner.js";
import {
  runSandcastle,
  type OpenHandsResult,
  type RunnerEvent,
  type SandcastleRunRequest,
} from "./sandcastle-runner.js";
import {
  StateStore,
  Task,
  TaskKind,
  type Program,
} from "./state-store.js";

export type SubmitInput = {
  kind: TaskKind;
  workspaceDir: string;
  goal: string;
  notifyUrl?: string | null;
};

export type SubmitResult = {
  taskId: string;
};

export type SubmitProgramInput = {
  workspaceDir: string;
  goal: string;
  notifyUrl?: string | null;
};

export type SubmitProgramResult = {
  programId: string;
};

const NOTIFY_BACKOFF_MS = [1000, 2000, 4000];
const MAX_CONCURRENT_PER_PROGRAM = Number(
  process.env["DEVCLAW_MAX_CONCURRENT_PER_PROGRAM"] ?? "2",
);

export class TaskQueue {
  /** In-flight counter per program, used to enforce concurrency cap. */
  private readonly runningByProgram = new Map<string, number>();

  constructor(
    private readonly store: StateStore,
    /** Injectable for tests — defaults to the real planner. */
    private readonly planner: (
      goal: string,
      workspaceDir: string,
    ) => Promise<PlannedTask[]> = (g, w) => planGoal(g, w),
    /**
     * Injectable for tests — defaults to the sandcastle docker runner. The
     * DAG-stub smoke test swaps this for a fake that resolves quickly so
     * we can verify queue + state logic without burning Pro tokens or
     * requiring docker.
     */
    private readonly runner: (
      req: SandcastleRunRequest,
    ) => Promise<OpenHandsResult> = runSandcastle,
  ) {}

  // ---- standalone task path (unchanged) -------------------------------

  submit(input: SubmitInput): SubmitResult {
    const taskId = randomUUID();
    this.store.createTask({
      id: taskId,
      kind: input.kind,
      workspaceDir: input.workspaceDir,
      goal: input.goal,
      notifyUrl: input.notifyUrl ?? null,
    });
    void this.executeStandalone(taskId, input);
    return { taskId };
  }

  private async executeStandalone(
    taskId: string,
    input: SubmitInput,
  ): Promise<void> {
    this.store.markRunning(taskId);
    await this.runAndSettle(taskId, {
      kind: input.kind,
      workspaceDir: input.workspaceDir,
      goal: input.goal,
    });

    const finalRow = this.store.getTask(taskId);
    if (finalRow?.notifyUrl) {
      await this.notifyTask(finalRow);
    }
  }

  // ---- program path ---------------------------------------------------

  submitProgram(input: SubmitProgramInput): SubmitProgramResult {
    const programId = randomUUID();
    this.store.createProgram({
      id: programId,
      goal: input.goal,
      workspaceDir: input.workspaceDir,
      notifyUrl: input.notifyUrl ?? null,
    });
    void this.planAndStart(programId, input);
    return { programId };
  }

  private async planAndStart(
    programId: string,
    input: SubmitProgramInput,
  ): Promise<void> {
    let planned: PlannedTask[];
    try {
      planned = await this.planner(input.goal, input.workspaceDir);
    } catch (err) {
      const msg =
        err instanceof PlannerError
          ? `planner: ${err.message}`
          : err instanceof Error
            ? err.message
            : String(err);
      this.store.markProgramFailed(programId, msg);
      const program = this.store.getProgram(programId);
      if (program) await this.notifyProgram(program, []);
      return;
    }

    // Map planner-supplied keys → real UUIDs, then persist tasks with
    // depends_on remapped to UUIDs. The whole insert runs synchronously
    // before any task is scheduled so the dep graph is fully consistent
    // by the time the first task starts.
    const keyToUuid = new Map<string, string>();
    for (const p of planned) keyToUuid.set(p.key, randomUUID());

    planned.forEach((p, idx) => {
      const id = keyToUuid.get(p.key) as string;
      const depsUuids = p.dependsOnKeys.map((k) => {
        const uuid = keyToUuid.get(k);
        if (!uuid) {
          // Should never happen — validatePlan rejects dangling refs.
          throw new Error(`planner produced dangling ref '${k}'`);
        }
        return uuid;
      });
      this.store.createTask({
        id,
        kind: p.kind,
        workspaceDir: input.workspaceDir,
        goal: p.goal,
        // Per-task notify intentionally omitted — only program-level fires.
        notifyUrl: null,
        programId,
        dependsOn: depsUuids,
        orderIdx: idx,
      });
    });

    this.store.markProgramRunning(programId);
    this.scheduleReady(programId);
  }

  /**
   * Find pending tasks whose deps are all done and launch up to the
   * concurrency cap. Safe to call multiple times — claimPending is
   * atomic, so a second concurrent call is a no-op for already-claimed
   * tasks. If the program has already failed (sticky), launch nothing.
   */
  private scheduleReady(programId: string): void {
    const program = this.store.getProgram(programId);
    if (!program || program.status === "failed" || program.status === "done") {
      return;
    }

    const tasks = this.store.listProgramTasks(programId);
    const byId = new Map(tasks.map((t) => [t.id, t]));

    // If any task already failed, mark program failed (sticky) and bail.
    if (tasks.some((t) => t.status === "failed")) {
      // Don't start new work, but in-flight tasks will continue. The
      // program transition to 'failed' happens in the post-task hook
      // once the in-flight count hits zero. Here we only suppress new
      // launches.
      return;
    }

    let inFlight = this.runningByProgram.get(programId) ?? 0;
    for (const t of tasks) {
      if (inFlight >= MAX_CONCURRENT_PER_PROGRAM) break;
      if (t.status !== "pending") continue;
      const depsReady = t.dependsOn.every(
        (d) => byId.get(d)?.status === "done",
      );
      if (!depsReady) continue;
      if (!this.store.claimPending(t.id)) continue; // lost the race
      inFlight++;
      this.runningByProgram.set(programId, inFlight);
      void this.executeProgramTask(programId, t.id, t.kind, t.workspaceDir, t.goal);
    }
  }

  private async executeProgramTask(
    programId: string,
    taskId: string,
    kind: TaskKind,
    workspaceDir: string,
    goal: string,
  ): Promise<void> {
    await this.runAndSettle(taskId, { kind, workspaceDir, goal });

    // Decrement in-flight, then re-evaluate scheduling + termination.
    const n = (this.runningByProgram.get(programId) ?? 1) - 1;
    if (n <= 0) this.runningByProgram.delete(programId);
    else this.runningByProgram.set(programId, n);

    this.advanceProgram(programId);
  }

  /**
   * Evaluate program-level state after a child terminated.
   *   - if all tasks 'done' → mark program done + notify
   *   - if any task 'failed' AND in-flight count is zero → mark program
   *     failed + notify (sticky)
   *   - otherwise schedule any newly-ready tasks
   */
  private advanceProgram(programId: string): void {
    const program = this.store.getProgram(programId);
    if (!program || program.status === "done" || program.status === "failed") {
      // Already terminal — notify already fired (or will, in the in-flight
      // task's own advanceProgram call).
      return;
    }

    const tasks = this.store.listProgramTasks(programId);
    const allDone = tasks.length > 0 && tasks.every((t) => t.status === "done");
    const anyFailed = tasks.some((t) => t.status === "failed");
    const inFlight = this.runningByProgram.get(programId) ?? 0;

    if (allDone) {
      this.store.markProgramDone(programId);
      void this.notifyProgram(
        this.store.getProgram(programId) as Program,
        tasks,
      );
      return;
    }
    if (anyFailed && inFlight === 0) {
      const firstErr =
        tasks.find((t) => t.status === "failed")?.error ?? "task failed";
      this.store.markProgramFailed(programId, firstErr);
      void this.notifyProgram(
        this.store.getProgram(programId) as Program,
        tasks,
      );
      return;
    }
    if (!anyFailed) {
      this.scheduleReady(programId);
    }
  }

  // ---- shared runner --------------------------------------------------

  private async runAndSettle(
    taskId: string,
    req: { kind: TaskKind; workspaceDir: string; goal: string },
  ): Promise<void> {
    // Resolve the program_id once so onEvent doesn't re-query the store on
    // every event. Standalone tasks just get null.
    const row = this.store.getTask(taskId);
    const programId = row?.programId ?? null;

    const onEvent = (event: RunnerEvent): void => {
      try {
        this.store.appendEvent({
          taskId,
          programId,
          type: event.type,
          source: event.source,
          payloadJson: JSON.stringify(event.payload ?? null),
          ts:
            typeof event.ts === "number"
              ? event.ts
              : Date.now(),
        });
      } catch (err) {
        // Event-table writes must never crash the run. Surface to stderr so
        // operators can spot a recurring schema/db problem.
        process.stderr.write(
          `task-queue: appendEvent failed task=${taskId}: ${(err as Error).message}\n`,
        );
      }
    };

    try {
      const result = await this.runner({ ...req, onEvent });
      if (result.status === "ok") {
        this.store.markDone(taskId, JSON.stringify(result));
      } else {
        this.store.markFailed(taskId, result.error);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.store.markFailed(taskId, msg);
    }
  }

  // ---- notify ---------------------------------------------------------

  private async notifyTask(task: Task): Promise<void> {
    if (!task.notifyUrl) return;
    const payload = {
      task_id: task.id,
      status: task.status,
      kind: task.kind,
      workspace_dir: task.workspaceDir,
      goal: task.goal,
      result_json: task.resultJson,
      error: task.error,
      terminated_at: task.completedAt,
    };
    await this.postWithRetries(task.notifyUrl, payload, `task=${task.id}`);
  }

  private async notifyProgram(program: Program, tasks: Task[]): Promise<void> {
    if (!program.notifyUrl) return;
    const payload = {
      program_id: program.id,
      status: program.status,
      goal: program.goal,
      workspace_dir: program.workspaceDir,
      error: program.error,
      terminated_at: program.completedAt,
      tasks: tasks.map((t) => ({
        task_id: t.id,
        kind: t.kind,
        status: t.status,
        goal: t.goal,
        depends_on: t.dependsOn,
        result_json: t.resultJson,
        error: t.error,
      })),
    };
    await this.postWithRetries(
      program.notifyUrl,
      payload,
      `program=${program.id}`,
    );
  }

  private async postWithRetries(
    url: string,
    payload: object,
    tag: string,
  ): Promise<void> {
    const body = JSON.stringify(payload);
    for (let attempt = 0; attempt < NOTIFY_BACKOFF_MS.length; attempt++) {
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body,
        });
        if (res.ok) {
          process.stderr.write(
            `notify ok ${tag} url=${url} status=${res.status} attempt=${attempt + 1}\n`,
          );
          return;
        }
        process.stderr.write(
          `notify non-2xx ${tag} url=${url} status=${res.status} attempt=${attempt + 1}\n`,
        );
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        process.stderr.write(
          `notify error ${tag} url=${url} err="${msg}" attempt=${attempt + 1}\n`,
        );
      }
      if (attempt < NOTIFY_BACKOFF_MS.length - 1) {
        await new Promise((r) => setTimeout(r, NOTIFY_BACKOFF_MS[attempt]));
      }
    }
    process.stderr.write(
      `notify WARN giving up ${tag} url=${url} after ${NOTIFY_BACKOFF_MS.length} attempts\n`,
    );
  }
}
