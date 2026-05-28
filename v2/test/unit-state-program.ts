/**
 * Pure unit test for the new program + DAG columns on the state store.
 * No subprocess, no network. Uses a tmp SQLite file.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

import { StateStore } from "../src/state-store.js";

const dir = mkdtempSync(resolve(tmpdir(), "devclaw-state-"));
const store = new StateStore(resolve(dir, "test.db"));

let failures = 0;
function check(name: string, fn: () => void): void {
  try {
    fn();
    process.stdout.write(`ok   ${name}\n`);
  } catch (err) {
    failures++;
    process.stdout.write(`FAIL ${name}: ${(err as Error).message}\n`);
  }
}

function assertEq<T>(a: T, b: T, msg: string): void {
  if (a !== b) {
    throw new Error(`${msg}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
  }
}

check("createProgram + getProgram round-trips", () => {
  store.createProgram({
    id: "P1",
    goal: "build a thing",
    workspaceDir: "/tmp/ws",
    notifyUrl: "http://localhost/cb",
  });
  const p = store.getProgram("P1");
  if (!p) throw new Error("missing program");
  assertEq(p.status, "planning", "initial status");
  assertEq(p.goal, "build a thing", "goal");
  assertEq(p.notifyUrl, "http://localhost/cb", "notify");
});

check("markProgramRunning + Done transitions", () => {
  store.markProgramRunning("P1");
  assertEq(store.getProgram("P1")?.status, "running", "running");
  store.markProgramDone("P1");
  assertEq(store.getProgram("P1")?.status, "done", "done");
});

check("markProgramFailed terminal — sticky", () => {
  store.createProgram({
    id: "P2",
    goal: "g",
    workspaceDir: "/tmp/ws2",
  });
  store.markProgramRunning("P2");
  store.markProgramFailed("P2", "boom");
  assertEq(store.getProgram("P2")?.status, "failed", "failed");
  assertEq(store.getProgram("P2")?.error, "boom", "err msg");
  // Subsequent transitions are blocked by the WHERE clause.
  store.markProgramDone("P2");
  assertEq(store.getProgram("P2")?.status, "failed", "still failed");
});

check("createTask with program_id + dependsOn round-trips", () => {
  store.createProgram({ id: "P3", goal: "x", workspaceDir: "/tmp/ws3" });
  store.createTask({
    id: "T1",
    kind: "implement_feature",
    workspaceDir: "/tmp/ws3",
    goal: "step 1",
    programId: "P3",
    dependsOn: [],
    orderIdx: 0,
  });
  store.createTask({
    id: "T2",
    kind: "implement_feature",
    workspaceDir: "/tmp/ws3",
    goal: "step 2",
    programId: "P3",
    dependsOn: ["T1"],
    orderIdx: 1,
  });
  const tasks = store.listProgramTasks("P3");
  assertEq(tasks.length, 2, "two tasks");
  assertEq(tasks[0]!.id, "T1", "order_idx orders T1 first");
  assertEq(tasks[1]!.dependsOn[0], "T1", "deps preserved");
});

check("claimPending is atomic — second call returns false", () => {
  store.createTask({
    id: "T3",
    kind: "implement_feature",
    workspaceDir: "/tmp",
    goal: "x",
  });
  assertEq(store.claimPending("T3"), true, "first claim wins");
  assertEq(store.claimPending("T3"), false, "second claim no-ops");
  assertEq(store.getTask("T3")?.status, "running", "row now running");
});

check("listProgramTasks ignores standalone tasks", () => {
  store.createTask({
    id: "TS",
    kind: "implement_feature",
    workspaceDir: "/tmp",
    goal: "standalone",
  });
  const tasks = store.listProgramTasks("P3");
  assertEq(tasks.length, 2, "still two");
});

store.close();
rmSync(dir, { recursive: true, force: true });

if (failures > 0) {
  process.stderr.write(`\n${failures} failure(s)\n`);
  process.exit(1);
}
process.stdout.write("\nAll unit-state-program tests passed.\n");
