/**
 * In-process async task executor. The MCP handler calls submit() and returns
 * immediately with a task_id; the OpenHands subprocess runs in the background,
 * and status flips in the state store when it terminates.
 *
 * Single-writer-to-state by design — only this queue mutates task rows.
 *
 * If a task was submitted with a notify_url, after the row reaches a terminal
 * state (done | failed) we POST the row to that URL. Bounded retries (1s/2s/4s
 * backoff). All errors get logged and swallowed — a callback failure must not
 * crash the queue.
 */

import { randomUUID } from "node:crypto";

import { runOpenHands } from "./openhands-runner.js";
import { StateStore, Task, TaskKind } from "./state-store.js";

export type SubmitInput = {
  kind: TaskKind;
  workspaceDir: string;
  goal: string;
  notifyUrl?: string | null;
};

export type SubmitResult = {
  taskId: string;
};

const NOTIFY_BACKOFF_MS = [1000, 2000, 4000];

export class TaskQueue {
  constructor(private readonly store: StateStore) {}

  submit(input: SubmitInput): SubmitResult {
    const taskId = randomUUID();
    this.store.createTask({
      id: taskId,
      kind: input.kind,
      workspaceDir: input.workspaceDir,
      goal: input.goal,
      notifyUrl: input.notifyUrl ?? null,
    });

    // Kick off the runner without awaiting — the MCP handler returns
    // immediately. Errors thrown by the runner promise are caught and
    // recorded in state; we never let them surface as unhandled rejections.
    void this.execute(taskId, input);

    return { taskId };
  }

  private async execute(taskId: string, input: SubmitInput): Promise<void> {
    this.store.markRunning(taskId);
    try {
      const result = await runOpenHands({
        kind: input.kind,
        workspaceDir: input.workspaceDir,
        goal: input.goal,
      });
      if (result.status === "ok") {
        this.store.markDone(taskId, JSON.stringify(result));
      } else {
        this.store.markFailed(taskId, result.error);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.store.markFailed(taskId, msg);
    }

    // Fire the notify callback after the row is in its final state. We read
    // the row back from the store so the payload reflects exactly what
    // get_status would return. Errors here never propagate — at worst the
    // caller polls instead.
    const finalRow = this.store.getTask(taskId);
    if (finalRow?.notifyUrl) {
      await this.notify(finalRow);
    }
  }

  private async notify(task: Task): Promise<void> {
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
    const body = JSON.stringify(payload);

    for (let attempt = 0; attempt < NOTIFY_BACKOFF_MS.length; attempt++) {
      try {
        const res = await fetch(task.notifyUrl, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body,
        });
        if (res.ok) {
          process.stderr.write(
            `notify ok task=${task.id} url=${task.notifyUrl} status=${res.status} attempt=${attempt + 1}\n`,
          );
          return;
        }
        process.stderr.write(
          `notify non-2xx task=${task.id} url=${task.notifyUrl} status=${res.status} attempt=${attempt + 1}\n`,
        );
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        process.stderr.write(
          `notify error task=${task.id} url=${task.notifyUrl} err="${msg}" attempt=${attempt + 1}\n`,
        );
      }
      if (attempt < NOTIFY_BACKOFF_MS.length - 1) {
        await new Promise((r) => setTimeout(r, NOTIFY_BACKOFF_MS[attempt]));
      }
    }
    process.stderr.write(
      `notify WARN giving up task=${task.id} url=${task.notifyUrl} after ${NOTIFY_BACKOFF_MS.length} attempts\n`,
    );
  }
}
