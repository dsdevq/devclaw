"""Validate the pre-PR review gate's JUDGMENT on real + synthetic diffs.

Step 3 (discrimination measurement, rate-not-run): feed review_gate.review_diff
the THREE real "green" diffs from the Step-1 basket (good code — measures the
false-positive rate) and TWO synthetic bad diffs (dead/no-op code; happy-path-only
no-tests — measures true-positive power). Uses the REAL claude at the review tier.

Run inside the devclaw-mcp container:
    DEVCLAW_REVIEW_MODEL=sonnet python3 /var/lib/devclaw/qdriver/validate_review_gate.py
"""

from __future__ import annotations

import asyncio
import subprocess

from devclaw.quality import review_diff

REPO = "dsdevq/todo-fullstack-demo"

REAL = [
    (9, "implement_feature",
     "Add support for optional due dates on todos (model, schemas, endpoints, "
     "frontend) with an overdue filter."),
    (10, "implement_feature",
     "Add bulk operations to the todos API: mark ALL todos completed, and delete "
     "ALL completed todos."),
    (11, "implement_feature",
     "Frontend: inline-edit a todo title (double-click to edit, Enter saves via "
     "PUT, Escape cancels) and show each todo's created date."),
]

# Synthetic bad diffs — the failure classes the gate exists to catch.
DEAD_CODE_DIFF = """diff --git a/backend/main.py b/backend/main.py
--- a/backend/main.py
+++ b/backend/main.py
@@ -40,6 +40,16 @@
+@app.get("/health/ready")
+def readiness(db: Session = Depends(get_db)):
+    # check the todos table is accessible
+    try:
+        db.query(Todo).limit(0).count()  # enumerates nothing — never touches a row
+        accessible = True
+    except Exception:
+        accessible = False
+    return {"status": "ready" if accessible else "not-ready"}
"""

HAPPY_PATH_DIFF = """diff --git a/backend/main.py b/backend/main.py
--- a/backend/main.py
+++ b/backend/main.py
@@ -40,6 +40,12 @@
+@app.post("/todos/{todo_id}/duplicate", response_model=TodoResponse, status_code=201)
+def duplicate_todo(todo_id: int, db: Session = Depends(get_db)):
+    original = db.get(Todo, todo_id)
+    copy = Todo(title=original.title)   # AttributeError if todo_id doesn't exist
+    db.add(copy)
+    db.commit()
+    db.refresh(copy)
+    return copy
diff --git a/backend/tests/test_todos.py b/backend/tests/test_todos.py
--- a/backend/tests/test_todos.py
+++ b/backend/tests/test_todos.py
@@ -195,3 +195,8 @@
+def test_duplicate(client):
+    created = client.post("/todos", json={"title": "x"}).json()
+    resp = client.post(f"/todos/{created['id']}/duplicate")
+    assert resp.status_code == 201   # only the happy path; no missing-id case
"""


def _pr_diff(num: int) -> str:
    return subprocess.run(
        ["gh", "pr", "diff", str(num), "-R", REPO],
        capture_output=True, text=True,
    ).stdout


async def _review(label: str, kind: str, goal: str, diff: str) -> None:
    try:
        v = await review_diff(goal=goal, kind=kind, diff=diff)
    except Exception as err:
        print(f"\n### {label}: REVIEW ERROR: {err}")
        return
    print(f"\n### {label} -> verdict={v['verdict']}  blocking={len(v['blocking'])}")
    print(f"    summary: {v['summary']}")
    for i in v["issues"]:
        print(f"    - ({i['severity']}) [{i['location']}] {i['problem']}")


async def main() -> None:
    print("=== REAL diffs (good code — expect mostly approve; few/no false blockers) ===")
    for num, kind, goal in REAL:
        await _review(f"PR#{num}", kind, goal, _pr_diff(num))

    print("\n=== SYNTHETIC bad diffs (expect request_changes) ===")
    await _review("dead-code", "implement_feature",
                  "Add a GET /health/ready readiness endpoint that verifies the "
                  "todos table is accessible.", DEAD_CODE_DIFF)
    await _review("happy-path-no-edge", "implement_feature",
                  "Add POST /todos/{id}/duplicate that copies a todo; 404 if the "
                  "id doesn't exist.", HAPPY_PATH_DIFF)


if __name__ == "__main__":
    asyncio.run(main())
