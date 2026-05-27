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

export type TaskKind =
  | "implement_feature"
  | "fix_bug"
  | "review_repository";

export type Task = {
  id: string;
  kind: TaskKind;
  status: TaskStatus;
  workspaceDir: string;
  goal: string;
  notifyUrl: string | null;
  resultJson: string | null;
  error: string | null;
  createdAt: number;
  startedAt: number | null;
  completedAt: number | null;
};

type TaskRow = {
  id: string;
  kind: TaskKind;
  status: TaskStatus;
  workspace_dir: string;
  goal: string;
  notify_url: string | null;
  result_json: string | null;
  error: string | null;
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
};

function rowToTask(row: TaskRow): Task {
  return {
    id: row.id,
    kind: row.kind,
    status: row.status,
    workspaceDir: row.workspace_dir,
    goal: row.goal,
    notifyUrl: row.notify_url,
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
        kind            TEXT NOT NULL DEFAULT 'implement_feature',
        status          TEXT NOT NULL,
        workspace_dir   TEXT NOT NULL,
        goal            TEXT NOT NULL,
        notify_url      TEXT,
        result_json     TEXT,
        error           TEXT,
        created_at      INTEGER NOT NULL,
        started_at      INTEGER,
        completed_at    INTEGER
      );

      CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks(status);
      CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
      CREATE INDEX IF NOT EXISTS idx_tasks_kind       ON tasks(kind);
    `);

    // Forward-compat ALTERs for DBs created by older slices. Each is
    // idempotent — swallow duplicate-column errors. Keep one ALTER per
    // added column so the order of slices stays explicit.
    for (const sql of [
      `ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'implement_feature'`,
      `ALTER TABLE tasks ADD COLUMN notify_url TEXT`,
    ]) {
      try {
        this.db.exec(sql);
      } catch {
        // column already exists
      }
    }
  }

  createTask(input: {
    id: string;
    kind: TaskKind;
    workspaceDir: string;
    goal: string;
    notifyUrl?: string | null;
  }): void {
    this.db
      .prepare(
        `INSERT INTO tasks (id, kind, status, workspace_dir, goal, notify_url, created_at)
         VALUES (?, ?, 'pending', ?, ?, ?, ?)`,
      )
      .run(
        input.id,
        input.kind,
        input.workspaceDir,
        input.goal,
        input.notifyUrl ?? null,
        Date.now(),
      );
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

  listTasks(opts?: {
    status?: TaskStatus;
    kind?: TaskKind;
    limit?: number;
  }): Task[] {
    const limit = opts?.limit ?? 100;
    const where: string[] = [];
    const args: (string | number)[] = [];
    if (opts?.status) {
      where.push("status = ?");
      args.push(opts.status);
    }
    if (opts?.kind) {
      where.push("kind = ?");
      args.push(opts.kind);
    }
    const whereSql = where.length ? `WHERE ${where.join(" AND ")}` : "";
    const rows = this.db
      .prepare(
        `SELECT * FROM tasks ${whereSql} ORDER BY created_at DESC LIMIT ?`,
      )
      .all(...args, limit) as TaskRow[];
    return rows.map(rowToTask);
  }

  close(): void {
    this.db.close();
  }
}
