# Bug-fix scope

Make the **smallest change that fixes the bug**. Resist scope creep — refactors, drive-by cleanups, and "while I'm here" improvements belong in their own PR.

Before changing anything, reproduce the bug if you can: write a failing test that captures the misbehaviour, then make it pass. The failing-test-first approach proves your fix is the right fix, not a coincidence.
