/**
 * SQLite state store for DevClaw v2 tasks.
 *
 * Tracks every task DevClaw has been asked to run, its current status, and
 * the result (or error) once it terminates. better-sqlite3 is sync, fast,
 * and zero-dep — fine for a single-writer process. We never need
 * concurrent writes because DevClaw v2 is one process that owns its DB.
 */

import Database from "better-sqlite3";
import { dirname } from "node:path";
import { mkdirSync, existsSync } from "node:fs";

export type TaskStatus = "pending" | "running" | "done" | "failed";

export type Task = {
  id: string;
  status: TaskStatus;
  workspaceDir: string;
  goal: string;
  resultJson: string | null;
  error: string | null;
  createdAt: number;
  startedAt: number | null;
  completedAt: number | null;
};

type TaskRow = {
  id: string;
  status: TaskStatus;
  workspace_dir: string;
  goal: string;
  result_json: string | null;
  error: string | null;
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
};

function rowToTask(row: TaskRow): Task {
  return {
    id: row.id,
    status: row.status,
    workspaceDir: row.workspace_dir,
    goal: row.goal,
    resultJson: row.result_json,
    error: row.error,
    createdAt: row.created_at,
    startedAt: row.started_at,
    completedAt: row.completed_at,
  };
}

export class StateStore {
  private db: Database.Database;

  constructor(dbPath: string) {
    const dir = dirname(dbPath);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    this.db = new Database(dbPath);
    this.db.pragma("journal_mode = WAL"); // concurrent reads, single writer
    this.db.pragma("foreign_keys = ON");
    this.bootstrap();
  }

  private bootstrap(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS tasks (
        id              TEXT PRIMARY KEY,
        status          TEXT NOT NULL,
        workspace_dir   TEXT NOT NULL,
        goal            TEXT NOT NULL,
        result_json     TEXT,
        error           TEXT,
        created_at      INTEGER NOT NULL,
        started_at      INTEGER,
        completed_at    INTEGER
      );

      CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks(status);
      CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
    `);
  }

  createTask(input: { id: string; workspaceDir: string; goal: string }): void {
    this.db
      .prepare(
        `INSERT INTO tasks (id, status, workspace_dir, goal, created_at)
         VALUES (?, 'pending', ?, ?, ?)`,
      )
      .run(input.id, input.workspaceDir, input.goal, Date.now());
  }

  markRunning(taskId: string): void {
    this.db
      .prepare(
        `UPDATE tasks SET status = 'running', started_at = ?
         WHERE id = ? AND status = 'pending'`,
      )
      .run(Date.now(), taskId);
  }

  markDone(taskId: string, resultJson: string): void {
    this.db
      .prepare(
        `UPDATE tasks SET status = 'done', result_json = ?, completed_at = ?
         WHERE id = ? AND status IN ('pending', 'running')`,
      )
      .run(resultJson, Date.now(), taskId);
  }

  markFailed(taskId: string, error: string): void {
    this.db
      .prepare(
        `UPDATE tasks SET status = 'failed', error = ?, completed_at = ?
         WHERE id = ? AND status IN ('pending', 'running')`,
      )
      .run(error, Date.now(), taskId);
  }

  getTask(taskId: string): Task | null {
    const row = this.db
      .prepare(`SELECT * FROM tasks WHERE id = ?`)
      .get(taskId) as TaskRow | undefined;
    return row ? rowToTask(row) : null;
  }

  listTasks(opts?: { status?: TaskStatus; limit?: number }): Task[] {
    const status = opts?.status;
    const limit = opts?.limit ?? 100;
    const rows = (
      status
        ? this.db
            .prepare(
              `SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?`,
            )
            .all(status, limit)
        : this.db
            .prepare(`SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?`)
            .all(limit)
    ) as TaskRow[];
    return rows.map(rowToTask);
  }

  close(): void {
    this.db.close();
  }
}
