# Diagnosis loop (hard bugs)

For bugs that don't fall to the first obvious fix, work this loop in order.
Skip a phase only when you can say why it's unnecessary for this bug.

## 1. Build a feedback loop — this is the skill

Everything else is mechanical. If you have a **tight** pass/fail signal that
goes red on *this* bug, you will find the cause; hypothesis-testing and
instrumentation just consume it. If you don't, no amount of staring at code
will save you. Spend disproportionate effort here.

Ways to construct one, in rough order: a failing test at whatever seam reaches
the bug; a curl/HTTP script against a running dev server; a CLI invocation
with a fixture input diffed against known-good output; a headless browser
script; replaying a captured payload/trace through the code path in isolation;
a throwaway harness that boots a minimal subset of the system; a property/fuzz
loop for "sometimes wrong" bugs; a bisection harness (`git bisect run`) when
the bug appeared between two known states.

Once you have *a* loop, **tighten it**: faster (cache setup, narrow scope),
sharper (assert the exact symptom, not "didn't crash"), more deterministic
(pin time, seed RNG, isolate filesystem). For flaky bugs, don't chase a clean
repro — raise the reproduction rate (loop the trigger 100×, add stress, narrow
timing) until it's debuggable.

**Phase 1 is done** when you can name one command you have already run that
is: red-capable (asserts the reported symptom, can go green once fixed),
deterministic, fast (seconds), and runnable by you unattended. If you catch
yourself reading code to build a theory before this command exists — stop;
jumping straight to a hypothesis is the exact failure this loop prevents.

If you genuinely cannot build a loop, do not proceed to guessing: report what
you tried and what artifact would unblock you (a captured payload, a log dump,
access to the environment that reproduces it), and fail the task legibly.

## 2. Reproduce and minimise

Run the loop; watch it go red with the **reported** symptom — not a different
failure that happens to be nearby (wrong bug = wrong fix). Then shrink the
repro to the smallest scenario that still goes red, cutting one element at a
time and re-running after each cut. Done when every remaining element is
load-bearing. The minimal repro shrinks the hypothesis space and becomes the
regression test.

## 3. Hypothesise — several, ranked, falsifiable

Generate 3–5 ranked hypotheses before testing any — single-hypothesis
generation anchors on the first plausible idea. Each must state its
prediction: "if X is the cause, changing Y makes the bug disappear." If you
can't state the prediction, it's a vibe — discard or sharpen. Record the
ranked list in your summary so a reviewer can follow the reasoning.

## 4. Instrument

Each probe maps to one prediction; change one variable at a time. Prefer a
debugger/REPL breakpoint over logs; targeted logs at hypothesis-distinguishing
boundaries over "log everything and grep". Tag every debug log with one unique
prefix (e.g. `[DEBUG-a4f2]`) so cleanup is a single grep. For performance
regressions, logs are usually wrong: establish a baseline measurement first,
then bisect — measure, then fix.

## 5. Fix + regression test

Write the regression test before the fix — but only at a **correct seam**, one
where the test exercises the real bug pattern as it occurred. A too-shallow
seam gives false confidence. If no correct seam exists, that is itself a
finding: say so in your summary instead of forcing a bad test. Then: watch the
test fail, apply the fix, watch it pass, re-run the Phase-1 loop against the
original un-minimised scenario.

## 6. Cleanup

Before declaring done: original repro is green; regression test passes (or its
absence is explained); every `[DEBUG-…]` line is removed (grep the prefix);
throwaway harnesses are deleted; and the hypothesis that proved correct is
stated in the commit message — the next debugger learns from it. If the
diagnosis exposed an architectural problem (no good test seam, tangled
callers), note it as follow-up in your summary — after the fix is in, not
instead of it.

---
*Adapted from [mattpocock/skills](https://github.com/mattpocock/skills) (MIT © 2026 Matt Pocock).*
