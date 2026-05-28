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

// Programs hold a DAG of tasks decomposed from a single high-level goal.
// Status semantics:
//   planning   — planner is still decomposing (claude --print in flight)
//   running    — at least one task exists, none have moved to failed AND
//                not all are terminal yet
//   done       — every task in the program is status='done'
//   failed     — planner failed OR any task is status='failed' (failure
//                is sticky; we do NOT continue scheduling siblings after
//                a sibling fails — see TaskQueue for the policy)
export type ProgramStatus = "planning" | "running" | "done" | "failed";

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
  programId: string | null;
  dependsOn: string[];
  orderIdx: number | null;
};

export type Program = {
  id: string;
  goal: string;
  workspaceDir: string;
  notifyUrl: string | null;
  status: ProgramStatus;
  error: string | null;
  createdAt: number;
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
  program_id: string | null;
  depends_on: string | null;
  order_idx: number | null;
};

type ProgramRow = {
  id: string;
  goal: string;
  workspace_dir: string;
  notify_url: string | null;
  status: ProgramStatus;
  error: string | null;
  created_at: number;
  completed_at: number | null;
};

function rowToTask(row: TaskRow): Task {
  let dependsOn: string[] = [];
  if (row.depends_on) {
    try {
      const parsed = JSON.parse(row.depends_on);
      if (Array.isArray(parsed)) dependsOn = parsed.filter((x) => typeof x === "string");
    } catch {
      // tolerate corrupt depends_on cell — treat as no deps
    }
  }
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
    programId: row.program_id,
    dependsOn,
    orderIdx: row.order_idx,
  };
}

function rowToProgram(row: ProgramRow): Program {
  return {
    id: row.id,
    goal: row.goal,
    workspaceDir: row.workspace_dir,
    notifyUrl: row.notify_url,
    status: row.status,
    error: row.error,
    createdAt: row.created_at,
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
        completed_at    INTEGER,
        program_id      TEXT,
        depends_on      TEXT,
        order_idx       INTEGER
      );

      CREATE TABLE IF NOT EXISTS programs (
        id              TEXT PRIMARY KEY,
        goal            TEXT NOT NULL,
        workspace_dir   TEXT NOT NULL,
        notify_url      TEXT,
        status          TEXT NOT NULL,
        error           TEXT,
        created_at      INTEGER NOT NULL,
        completed_at    INTEGER
      );

      CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks(status);
      CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
      CREATE INDEX IF NOT EXISTS idx_tasks_kind       ON tasks(kind);
      CREATE INDEX IF NOT EXISTS idx_tasks_program    ON tasks(program_id);
      CREATE INDEX IF NOT EXISTS idx_programs_status  ON programs(status);
    `);

    // Forward-compat ALTERs for DBs created by older slices. Each is
    // idempotent — swallow duplicate-column errors. Keep one ALTER per
    // added column so the order of slices stays explicit.
    for (const sql of [
      `ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'implement_feature'`,
      `ALTER TABLE tasks ADD COLUMN notify_url TEXT`,
      `ALTER TABLE tasks ADD COLUMN program_id TEXT`,
      `ALTER TABLE tasks ADD COLUMN depends_on TEXT`,
      `ALTER TABLE tasks ADD COLUMN order_idx INTEGER`,
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
    programId?: string | null;
    dependsOn?: string[];
    orderIdx?: number | null;
  }): void {
    this.db
      .prepare(
        `INSERT INTO tasks
           (id, kind, status, workspace_dir, goal, notify_url, created_at,
            program_id, depends_on, order_idx)
         VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        input.id,
        input.kind,
        input.workspaceDir,
        input.goal,
        input.notifyUrl ?? null,
        Date.now(),
        input.programId ?? null,
        input.dependsOn && input.dependsOn.length
          ? JSON.stringify(input.dependsOn)
          : null,
        input.orderIdx ?? null,
      );
  }

  createProgram(input: {
    id: string;
    goal: string;
    workspaceDir: string;
    notifyUrl?: string | null;
  }): void {
    this.db
      .prepare(
        `INSERT INTO programs (id, goal, workspace_dir, notify_url, status, created_at)
         VALUES (?, ?, ?, ?, 'planning', ?)`,
      )
      .run(
        input.id,
        input.goal,
        input.workspaceDir,
        input.notifyUrl ?? null,
        Date.now(),
      );
  }

  markProgramRunning(programId: string): void {
    this.db
      .prepare(
        `UPDATE programs SET status = 'running'
         WHERE id = ? AND status = 'planning'`,
      )
      .run(programId);
  }

  markProgramDone(programId: string): void {
    this.db
      .prepare(
        `UPDATE programs SET status = 'done', completed_at = ?
         WHERE id = ? AND status IN ('planning', 'running')`,
      )
      .run(Date.now(), programId);
  }

  markProgramFailed(programId: string, error: string): void {
    this.db
      .prepare(
        `UPDATE programs SET status = 'failed', error = ?, completed_at = ?
         WHERE id = ? AND status IN ('planning', 'running')`,
      )
      .run(error, Date.now(), programId);
  }

  getProgram(programId: string): Program | null {
    const row = this.db
      .prepare(`SELECT * FROM programs WHERE id = ?`)
      .get(programId) as ProgramRow | undefined;
    return row ? rowToProgram(row) : null;
  }

  listProgramTasks(programId: string): Task[] {
    const rows = this.db
      .prepare(
        `SELECT * FROM tasks WHERE program_id = ?
         ORDER BY order_idx IS NULL, order_idx ASC, created_at ASC`,
      )
      .all(programId) as TaskRow[];
    return rows.map(rowToTask);
  }

  markRunning(taskId: string): void {
    this.db
      .prepare(
        `UPDATE tasks SET status = 'running', started_at = ?
         WHERE id = ? AND status = 'pending'`,
      )
      .run(Date.now(), taskId);
  }

  /**
   * Atomically transition pending → running. Returns true if THIS call won
   * the race (and the caller must therefore execute the task), false if
   * the task was already running/done/failed or doesn't exist. Used by
   * the DAG scheduler where multiple siblings finishing can both try to
   * unblock the same downstream task.
   */
  claimPending(taskId: string): boolean {
    const result = this.db
      .prepare(
        `UPDATE tasks SET status = 'running', started_at = ?
         WHERE id = ? AND status = 'pending'`,
      )
      .run(Date.now(), taskId);
    return result.changes === 1;
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
