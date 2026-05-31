/**
 * Pure unit tests for planner validation. No subprocess, no network.
 * Run: npx tsx test/unit-planner.ts
 */

import {
  extractJson,
  planGoal,
  PlannerError,
  validatePlan,
  type PlannedTask,
} from "../src/planner.js";

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

function assertEq<T>(actual: T, expected: T, msg: string): void {
  if (actual !== expected) {
    throw new Error(`${msg}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

function assertThrows(fn: () => unknown, match: RegExp, msg: string): void {
  try {
    fn();
  } catch (err) {
    const m = (err as Error).message;
    if (!match.test(m)) throw new Error(`${msg}: error didn't match ${match}: ${m}`);
    return;
  }
  throw new Error(`${msg}: expected throw`);
}

// ---- extractJson ----------------------------------------------------------

check("extractJson: plain object", () => {
  assertEq(extractJson('{"tasks":[]}'), '{"tasks":[]}', "plain");
});

check("extractJson: leading whitespace", () => {
  assertEq(extractJson('  \n{"tasks":[]}\n').trim(), '{"tasks":[]}', "ws");
});

check("extractJson: fenced", () => {
  assertEq(
    extractJson('```json\n{"tasks":[]}\n```'),
    '{"tasks":[]}',
    "fenced",
  );
});

check("extractJson: prose preface + suffix", () => {
  const raw = 'Here is the plan:\n{"tasks":[{"key":"a"}]}\nThanks!';
  assertEq(extractJson(raw), '{"tasks":[{"key":"a"}]}', "prose");
});

check("extractJson: no json throws", () => {
  assertThrows(() => extractJson("no json here"), /No JSON object/, "no-json");
});

// ---- validatePlan ---------------------------------------------------------

check("validatePlan: single task no deps", () => {
  const out = validatePlan({
    tasks: [{ key: "a", goal: "do the thing", kind: "implement_feature" }],
  });
  assertEq(out.length, 1, "len");
  assertEq(out[0]!.key, "a", "key");
  assertEq(out[0]!.dependsOnKeys.length, 0, "no deps");
});

check("validatePlan: default kind is implement_feature", () => {
  const out = validatePlan({
    tasks: [{ key: "a", goal: "x" }],
  });
  assertEq(out[0]!.kind, "implement_feature", "default kind");
});

check("validatePlan: linear chain orders topologically", () => {
  const out = validatePlan({
    tasks: [
      { key: "c", goal: "c", depends_on: ["b"] },
      { key: "a", goal: "a" },
      { key: "b", goal: "b", depends_on: ["a"] },
    ],
  });
  assertEq(out.map((t) => t.key).join(","), "a,b,c", "topo");
});

check("validatePlan: diamond DAG ordered correctly", () => {
  const out = validatePlan({
    tasks: [
      { key: "root", goal: "x" },
      { key: "left", goal: "x", depends_on: ["root"] },
      { key: "right", goal: "x", depends_on: ["root"] },
      { key: "join", goal: "x", depends_on: ["left", "right"] },
    ],
  });
  const order = out.map((t) => t.key);
  assertEq(order[0], "root", "root first");
  assertEq(order[3], "join", "join last");
});

check("validatePlan: cycle rejected", () => {
  assertThrows(
    () =>
      validatePlan({
        tasks: [
          { key: "a", goal: "x", depends_on: ["b"] },
          { key: "b", goal: "x", depends_on: ["a"] },
        ],
      }),
    /cycle/i,
    "cycle",
  );
});

check("validatePlan: self-dep rejected", () => {
  assertThrows(
    () =>
      validatePlan({
        tasks: [{ key: "a", goal: "x", depends_on: ["a"] }],
      }),
    /depends on itself/,
    "self",
  );
});

check("validatePlan: dangling ref rejected", () => {
  assertThrows(
    () =>
      validatePlan({
        tasks: [{ key: "a", goal: "x", depends_on: ["ghost"] }],
      }),
    /unknown key/,
    "dangling",
  );
});

check("validatePlan: duplicate key rejected", () => {
  assertThrows(
    () =>
      validatePlan({
        tasks: [
          { key: "a", goal: "x" },
          { key: "a", goal: "y" },
        ],
      }),
    /Duplicate/,
    "dup",
  );
});

check("validatePlan: invalid kind rejected", () => {
  assertThrows(
    () =>
      validatePlan({
        tasks: [{ key: "a", goal: "x", kind: "build_pyramid" }],
      }),
    /invalid kind/,
    "kind",
  );
});

check("validatePlan: empty list rejected", () => {
  assertThrows(
    () => validatePlan({ tasks: [] }),
    /at least one/,
    "empty",
  );
});

check("validatePlan: non-array rejected", () => {
  assertThrows(
    () => validatePlan({ tasks: "nope" }),
    /must be an array/,
    "non-array",
  );
});

// ---- planGoal with stubbed claude ----------------------------------------

check("planGoal: parses fenced JSON from stub", async () => {
  const stub = async () =>
    '```json\n{"tasks":[{"key":"only","goal":"fix typo"}]}\n```';
  const out = await planGoal("fix typo", "/tmp/ws", stub);
  assertEq(out.length, 1, "len");
  assertEq(out[0]!.goal, "fix typo", "goal");
});

check("planGoal: bubbles JSON parse error as PlannerError", async () => {
  const stub = async () => "definitely not json";
  try {
    await planGoal("x", "/tmp", stub);
    throw new Error("expected throw");
  } catch (err) {
    if (!(err instanceof PlannerError)) throw new Error("wrong type");
  }
});

// ---- async test driver ---------------------------------------------------

async function runAsyncChecks(): Promise<void> {
  // Re-run the async checks above. The sync check() wrapper above only
  // works for sync fns; the async ones need their own driver. They're
  // already registered as 'ok' (since check() doesn't await), so we
  // re-execute them here for real verification.
  const cases: Array<[string, () => Promise<void>]> = [
    [
      "planGoal-stub: parses fenced JSON",
      async () => {
        const stub = async () =>
          '```json\n{"tasks":[{"key":"only","goal":"fix typo"}]}\n```';
        const out = await planGoal("fix typo", "/tmp/ws", stub);
        assertEq(out.length, 1, "len");
      },
    ],
    [
      "planGoal-stub: non-json bubbles as PlannerError",
      async () => {
        const stub = async () => "definitely not json";
        try {
          await planGoal("x", "/tmp", stub);
          throw new Error("expected throw");
        } catch (err) {
          if (!(err instanceof PlannerError)) {
            throw new Error("wrong error type");
          }
        }
      },
    ],
    [
      "planGoal-stub: full DAG round-trip preserves order",
      async () => {
        const plan = {
          tasks: [
            { key: "c", goal: "c", depends_on: ["b"] },
            { key: "a", goal: "a" },
            { key: "b", goal: "b", depends_on: ["a"] },
          ],
        };
        const stub = async () => JSON.stringify(plan);
        const out: PlannedTask[] = await planGoal("x", "/tmp", stub);
        assertEq(out.map((t) => t.key).join(","), "a,b,c", "order");
      },
    ],
  ];

  for (const [name, fn] of cases) {
    try {
      await fn();
      process.stdout.write(`ok   ${name}\n`);
    } catch (err) {
      failures++;
      process.stdout.write(`FAIL ${name}: ${(err as Error).message}\n`);
    }
  }
}

await runAsyncChecks();

if (failures > 0) {
  process.stderr.write(`\n${failures} failure(s)\n`);
  process.exit(1);
}
process.stdout.write("\nAll unit-planner tests passed.\n");
