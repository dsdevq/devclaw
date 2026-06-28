# Quality bar

Hold yourself to a production code-quality bar — write code you would approve in a code review, not just code that makes the tests pass.

Concretely:

- Match the surrounding code's architecture and put new logic where similar logic already lives (e.g. in the relevant service/module, not inlined into an unrelated spot).
- Follow the existing style, naming, and error-handling patterns.
- Write NO dead, placeholder, or no-op code — every line must do real work.
- Handle the real edge and error cases, not only the happy path.
- Make any tests you add genuinely exercise the behaviour (including failure/edge cases); never weaken or delete existing tests just to go green.

A passing test suite is necessary but NOT sufficient — before you finish, re-read your own diff critically, as a senior engineer would, and fix anything sloppy, misleading, or that wouldn't pass review.
