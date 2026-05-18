"""Cognition-layer runner nodes — each shells out to Claude Code CLI for one task kind.

All runners share the same subprocess shape (see `_subprocess.py`). They differ in:
  - the prompt they build (`_build_prompt`)
  - the expected output artifact (PR, findings.md, draft.md, proposal.md, ...)
  - the Result shape they fill
"""

from orchestrator.runners.code_task import code_task_node, code_task_node_stub
from orchestrator.runners.propose_change import (
    propose_change_node,
    propose_change_node_stub,
)
from orchestrator.runners.research_task import (
    research_task_node,
    research_task_node_stub,
)

__all__ = [
    "code_task_node",
    "code_task_node_stub",
    "research_task_node",
    "research_task_node_stub",
    "propose_change_node",
    "propose_change_node_stub",
]
