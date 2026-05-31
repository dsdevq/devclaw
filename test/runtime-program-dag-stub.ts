/**
 * Local dev harness — in-process DAG smoke test with stubbed planner and
 * OpenHands runner. No subprocess, no network, no Pro tokens, no OpenHands.
 *
 * What this covers (the slice-4 DAG path that costs real money to test
 * against the live VPS):
 *   - Single-task program: planner returns 1 task → executes → program done.
 *   - Diamond DAG: planner returns root → (left, right) → join. Asserts
 *     left + right run after root, join runs after both, program done.
 *   - Failure stickiness: planner returns A → B; A fails. B is pending and
 *     not yet running → must NEVER start. Program ends in failed.
 *   - In-flight finishes on failure: A and B run in parallel (no deps);
 *     A fails mid-flight while B is running. B finishes; program flips
 *     failed once in-flight count hits zero.
 *   - Concurrency cap: planner returns 5 independent tasks. With
 *     DEVCLAW_MAX_CONCURRENT_PER_PROGRAM=2 the queue must never have more
 *     than 2 runners in-flight simultaneously.
 *
 * Run: npm run test:dag
 * Fast: completes in ~1s. Safe to run in inner-loop iteration.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

import type {
  OpenHandsResult,
  SandcastleRunRequest,
} from "../src/sandcastle-runner.js";
import type { PlannedTask } from "../src/planner.js";
import { StateStore } from "../src/state-store.js";
import { TaskQueue } from "../src/task-queue.js";

// Force the concurrency cap to a known value regardless of caller env.
process.env["DEVCLAW_MAX_CONCURRENT_PER_PROGRAM"] = "2";

let failures = 0;
const log = (s: string) => process.stdout.write(s + "\n");

function fail(name: string, err: unknown): void {
  failures++;
  log(`FAIL ${name}: ${(err as Error).message ?? err}`);
}

function ok(name: string): void {
  log(`ok   ${name}`);
}

async function waitFor(
  cond: () => boolean,
  timeoutMs = 2000,
  pollMs = 10,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (cond()) return;
    await new Promise((r) => setTimeout(r, pollMs));
  }
  throw new Error(`timed out after ${timeoutMs}ms waiting for condition`);
}

function newStore(): { store: StateStore; cleanup: () => void } {
  const dir = mkdtempSync(resolve(tmpdir(), "devclaw-dag-stub-"));
  const store = new StateStore(resolve(dir, "test.db"));
  return {
    store,
    cleanup: () => {
      store.close();
      rmSync(dir, { recursive: true, force: true });
    },
  };
}

// ---- a controllable runner stub ------------------------------------------

type RunnerEvent = {
  goal: string;
  startedAt: number;
};

/**
 * Builds a runner stub with controllable per-goal behavior + an in-flight
 * counter. Returns the runner function plus inspection helpers.
 */
function makeRunner(opts: {
  // Per-goal directive. If a goal isn't in the map, defaults to ok after 30ms.
  behaviors?: Record<
    string,
    { kind: "ok" | "error"; delayMs?: number; message?: string }
  >;
}) {
  const events: RunnerEvent[] = [];
  let inFlight = 0;
  let peakInFlight = 0;
  const runner = async (req: SandcastleRunRequest): Promise<OpenHandsResult> => {
    inFlight++;
    if (inFlight > peakInFlight) peakInFlight = inFlight;
    events.push({ goal: req.goal, startedAt: Date.now() });
    // Emit one synthetic ActionEvent per run so the events-table assertion
    // has something to verify. Real runner.py emits many; one is enough to
    // prove plumbing.
    req.onEvent?.({
      id: `stub-${req.goal}`,
      type: "ActionEvent",
      source: "agent",
      ts: Date.now(),
      payload: { goal: req.goal },
    });
    const beh = opts.behaviors?.[req.goal] ?? { kind: "ok", delayMs: 30 };
    await new Promise((r) => setTimeout(r, beh.delayMs ?? 30));
    inFlight--;
    if (beh.kind === "error") {
      return { status: "error", error: beh.message ?? "stub error" };
    }
    return {
      status: "ok",
      workspaceDir: req.workspaceDir,
      message: beh.message ?? "stub ok",
    };
  };
  return {
    runner,
    events,
    peak: () => peakInFlight,
  };
}

function makePlanner(plan: PlannedTask[]) {
  return async () => plan;
}

// ---- tests ----------------------------------------------------------------

async function testSingleTask(): Promise<void> {
  const name = "single-task program → done";
  const { store, cleanup } = newStore();
  try {
    const planner = makePlanner([
      { key: "only", goal: "only-task", kind: "implement_feature", dependsOnKeys: [] },
    ]);
    const { runner } = makeRunner({});
    const queue = new TaskQueue(store, planner, runner);

    const { programId } = queue.submitProgram({
      workspaceDir: "/tmp/ws",
      goal: "single",
    });

    await waitFor(() => store.getProgram(programId)?.status === "done");

    const tasks = store.listProgramTasks(programId);
    if (tasks.length !== 1) throw new Error(`expected 1 task, got ${tasks.length}`);
    if (tasks[0]!.status !== "done") throw new Error(`task not done: ${tasks[0]!.status}`);
    ok(name);
  } catch (err) {
    fail(name, err);
  } finally {
    cleanup();
  }
}

async function testDiamondDag(): Promise<void> {
  const name = "diamond DAG executes in topo order";
  const { store, cleanup } = newStore();
  try {
    const planner = makePlanner([
      { key: "root", goal: "root", kind: "implement_feature", dependsOnKeys: [] },
      { key: "left", goal: "left", kind: "implement_feature", dependsOnKeys: ["root"] },
      { key: "right", goal: "right", kind: "implement_feature", dependsOnKeys: ["root"] },
      { key: "join", goal: "join", kind: "implement_feature", dependsOnKeys: ["left", "right"] },
    ]);
    const { runner, events } = makeRunner({});
    const queue = new TaskQueue(store, planner, runner);

    const { programId } = queue.submitProgram({
      workspaceDir: "/tmp/ws",
      goal: "diamond",
    });

    await waitFor(
      () => store.getProgram(programId)?.status === "done",
      4000,
    );

    const order = events.map((e) => e.goal);
    // root must precede left & right; left + right must precede join.
    const idx = (g: string) => order.indexOf(g);
    if (idx("root") < 0 || idx("left") < 0 || idx("right") < 0 || idx("join") < 0) {
      throw new Error(`missing goals in events: ${JSON.stringify(order)}`);
    }
    if (!(idx("root") < idx("left") && idx("root") < idx("right"))) {
      throw new Error(`root must precede left+right: ${JSON.stringify(order)}`);
    }
    if (!(idx("left") < idx("join") && idx("right") < idx("join"))) {
      throw new Error(`left+right must precede join: ${JSON.stringify(order)}`);
    }
    ok(name);
  } catch (err) {
    fail(name, err);
  } finally {
    cleanup();
  }
}

async function testFailureSticky(): Promise<void> {
  const name = "sequential failure: B never starts after A fails";
  const { store, cleanup } = newStore();
  try {
    const planner = makePlanner([
      { key: "a", goal: "a", kind: "implement_feature", dependsOnKeys: [] },
      { key: "b", goal: "b", kind: "implement_feature", dependsOnKeys: ["a"] },
    ]);
    const { runner, events } = makeRunner({
      behaviors: { a: { kind: "error", delayMs: 20, message: "a-fail" } },
    });
    const queue = new TaskQueue(store, planner, runner);

    const { programId } = queue.submitProgram({
      workspaceDir: "/tmp/ws",
      goal: "sticky",
    });

    await waitFor(() => store.getProgram(programId)?.status === "failed");
    const tasks = store.listProgramTasks(programId);
    const bTask = tasks.find((t) => t.goal === "b");
    if (!bTask) throw new Error("missing b task");
    if (bTask.status !== "pending") {
      throw new Error(`b should never have started; status=${bTask.status}`);
    }
    if (events.some((e) => e.goal === "b")) {
      throw new Error("b must not have been invoked by runner");
    }
    ok(name);
  } catch (err) {
    fail(name, err);
  } finally {
    cleanup();
  }
}

async function testFailureInFlightDrains(): Promise<void> {
  const name = "parallel failure: in-flight sibling drains before program failed";
  const { store, cleanup } = newStore();
  try {
    // a and b are independent (no deps). a fails quickly; b takes longer.
    // The program must NOT flip to failed until b finishes — but b WILL run.
    const planner = makePlanner([
      { key: "a", goal: "a", kind: "implement_feature", dependsOnKeys: [] },
      { key: "b", goal: "b", kind: "implement_feature", dependsOnKeys: [] },
    ]);
    const { runner, events } = makeRunner({
      behaviors: {
        a: { kind: "error", delayMs: 20, message: "a-fail" },
        b: { kind: "ok", delayMs: 80 },
      },
    });
    const queue = new TaskQueue(store, planner, runner);

    const { programId } = queue.submitProgram({
      workspaceDir: "/tmp/ws",
      goal: "drain",
    });

    await waitFor(() => store.getProgram(programId)?.status === "failed", 2000);
    const tasks = store.listProgramTasks(programId);
    const bTask = tasks.find((t) => t.goal === "b");
    if (!bTask) throw new Error("missing b task");
    if (bTask.status !== "done") {
      throw new Error(`b should have finished; status=${bTask.status}`);
    }
    const bRan = events.some((e) => e.goal === "b");
    if (!bRan) throw new Error("b must have been invoked (was in-flight)");
    ok(name);
  } catch (err) {
    fail(name, err);
  } finally {
    cleanup();
  }
}

async function testConcurrencyCap(): Promise<void> {
  const name = "concurrency cap honoured (max 2 in flight)";
  const { store, cleanup } = newStore();
  try {
    // 5 independent tasks. Each takes 60ms. With cap=2 it should take
    // at least 3 windows (~180ms) and peak in-flight should be 2.
    const planner = makePlanner(
      ["t1", "t2", "t3", "t4", "t5"].map((k) => ({
        key: k,
        goal: k,
        kind: "implement_feature" as const,
        dependsOnKeys: [],
      })),
    );
    const { runner, peak } = makeRunner({
      behaviors: Object.fromEntries(
        ["t1", "t2", "t3", "t4", "t5"].map((k) => [
          k,
          { kind: "ok" as const, delayMs: 60 },
        ]),
      ),
    });
    const queue = new TaskQueue(store, planner, runner);

    const { programId } = queue.submitProgram({
      workspaceDir: "/tmp/ws",
      goal: "fanout",
    });

    await waitFor(() => store.getProgram(programId)?.status === "done", 4000);
    if (peak() > 2) {
      throw new Error(`peak in-flight ${peak()} exceeded cap 2`);
    }
    if (peak() < 2) {
      // Not strictly a failure (could be timing) but worth flagging.
      log(`note: peak in-flight only ${peak()}; concurrency may not be exercised`);
    }
    ok(name);
  } catch (err) {
    fail(name, err);
  } finally {
    cleanup();
  }
}

async function testEventsLandInTable(): Promise<void> {
  const name = "runner events land in events table with correct program_id";
  const { store, cleanup } = newStore();
  try {
    const planner = makePlanner([
      { key: "a", goal: "a", kind: "implement_feature", dependsOnKeys: [] },
      { key: "b", goal: "b", kind: "implement_feature", dependsOnKeys: ["a"] },
    ]);
    const { runner } = makeRunner({});
    const queue = new TaskQueue(store, planner, runner);

    const { programId } = queue.submitProgram({
      workspaceDir: "/tmp/ws",
      goal: "events",
    });

    await waitFor(() => store.getProgram(programId)?.status === "done");

    const events = store.listEvents({ programId, limit: 100 });
    if (events.length !== 2) {
      throw new Error(`expected 2 events, got ${events.length}`);
    }
    if (!events.every((e) => e.programId === programId)) {
      throw new Error("event program_id mismatch");
    }
    if (!events.every((e) => e.type === "ActionEvent")) {
      throw new Error("event type mismatch");
    }
    // ids must be monotonic — that's what the SSE resume cursor relies on.
    if (events[1]!.id <= events[0]!.id) {
      throw new Error("event ids must be strictly increasing");
    }

    // since_id should exclude already-seen events.
    const tail = store.listEvents({ programId, sinceId: events[0]!.id });
    if (tail.length !== 1 || tail[0]!.id !== events[1]!.id) {
      throw new Error("since_id cursor did not filter correctly");
    }
    ok(name);
  } catch (err) {
    fail(name, err);
  } finally {
    cleanup();
  }
}

async function testStandaloneEventsNullProgramId(): Promise<void> {
  const name = "standalone task events have null program_id";
  const { store, cleanup } = newStore();
  try {
    const { runner } = makeRunner({});
    const queue = new TaskQueue(
      store,
      // Planner is unused on this path; throw to make sure it's not called.
      async () => {
        throw new Error("planner should not run for submit()");
      },
      runner,
    );

    const { taskId } = queue.submit({
      kind: "implement_feature",
      workspaceDir: "/tmp/ws",
      goal: "standalone",
    });

    await waitFor(() => store.getTask(taskId)?.status === "done");

    const events = store.listEvents({ taskId, limit: 10 });
    if (events.length !== 1) {
      throw new Error(`expected 1 event, got ${events.length}`);
    }
    if (events[0]!.programId !== null) {
      throw new Error(
        `standalone task event must have null program_id; got ${events[0]!.programId}`,
      );
    }
    ok(name);
  } catch (err) {
    fail(name, err);
  } finally {
    cleanup();
  }
}

async function testListProgramsOrdering(): Promise<void> {
  const name = "list_programs returns most-recent first";
  const { store, cleanup } = newStore();
  try {
    const { runner } = makeRunner({});
    const queue = new TaskQueue(
      store,
      makePlanner([
        { key: "x", goal: "x", kind: "implement_feature", dependsOnKeys: [] },
      ]),
      runner,
    );

    const first = queue.submitProgram({ workspaceDir: "/tmp/ws", goal: "first" });
    // Force created_at to differ so the ORDER BY is deterministic — Date.now()
    // ticks at ms granularity which can collide on fast machines.
    await new Promise((r) => setTimeout(r, 2));
    const second = queue.submitProgram({ workspaceDir: "/tmp/ws", goal: "second" });
    await new Promise((r) => setTimeout(r, 2));
    const third = queue.submitProgram({ workspaceDir: "/tmp/ws", goal: "third" });

    await waitFor(() => store.getProgram(third.programId)?.status === "done");

    const programs = store.listPrograms({ limit: 10 });
    if (programs.length !== 3) {
      throw new Error(`expected 3 programs, got ${programs.length}`);
    }
    const ids = programs.map((p) => p.id);
    const expected = [third.programId, second.programId, first.programId];
    if (JSON.stringify(ids) !== JSON.stringify(expected)) {
      throw new Error(
        `ordering wrong: got ${JSON.stringify(ids)} expected ${JSON.stringify(expected)}`,
      );
    }
    ok(name);
  } catch (err) {
    fail(name, err);
  } finally {
    cleanup();
  }
}

async function main(): Promise<void> {
  await testSingleTask();
  await testDiamondDag();
  await testFailureSticky();
  await testFailureInFlightDrains();
  await testConcurrencyCap();
  await testEventsLandInTable();
  await testStandaloneEventsNullProgramId();
  await testListProgramsOrdering();

  if (failures > 0) {
    process.stderr.write(`\n${failures} failure(s)\n`);
    process.exit(1);
  }
  log("\nAll DAG stub smoke tests passed.");
}

await main();
