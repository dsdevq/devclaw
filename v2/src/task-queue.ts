/**
 * In-process async task executor. The MCP handler calls submit() and returns
 * immediately with a task_id; the OpenHands subprocess runs in the background,
 * and status flips in the state store when it terminates.
 *
 * Single-writer-to-state by design — only this queue mutates task rows.
 */

import { randomUUID } from "node:crypto";

import { runOpenHands } from "./openhands-runner.js";
import { StateStore, TaskKind } from "./state-store.js";

export type SubmitInput = {
  kind: TaskKind;
  workspaceDir: string;
  goal: string;
};

export type SubmitResult = {
  taskId: string;
};

export class TaskQueue {
  constructor(private readonly store: StateStore) {}

  submit(input: SubmitInput): SubmitResult {
    const taskId = randomUUID();
    this.store.createTask({
      id: taskId,
      kind: input.kind,
      workspaceDir: input.workspaceDir,
      goal: input.goal,
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
  }
}
