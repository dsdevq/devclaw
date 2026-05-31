"""Build-from-scratch orchestration: grill → spec → approve → execute.

Ties the elicitation grill, the filesystem ProjectStore, and the executor
together. Cognition (the grill) and the spec planner are injected, so the whole
flow is testable end-to-end with stubs — no claude, no docker. The MCP tools in
``server.py`` are thin wrappers over this.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from .elicitation import next_step
from .planner import PlannedTask, call_claude, plan_spec
from .project_store import Project, ProjectStore
from .task_queue import TaskQueue

GrillCaller = Callable[[str], Awaitable[str]]
SpecPlanner = Callable[[str, str], Awaitable[list[PlannedTask]]]


class ProjectService:
    def __init__(
        self,
        store: ProjectStore,
        queue: TaskQueue,
        *,
        grill_caller: GrillCaller = call_claude,
        spec_planner: SpecPlanner = plan_spec,
    ) -> None:
        self._store = store
        self._queue = queue
        self._grill = grill_caller
        self._plan_spec = spec_planner

    async def start(self, idea: str, workspace_dir: str) -> dict:
        """Begin a project: create it and run the first grill turn."""
        project = self._store.create(idea=idea, workspace_dir=workspace_dir)
        return await self._advance(project)

    async def answer(self, project_id: str, answer: str) -> dict:
        """Record the answer to the outstanding question and run the next turn."""
        project = self._store.get(project_id)
        if project is None:
            raise KeyError(project_id)
        if project.status != "eliciting":
            raise ValueError(f"project is '{project.status}', not awaiting an answer")
        self._store.record_answer(project, answer)
        return await self._advance(project)

    async def _advance(self, project: Project) -> dict:
        step = await next_step(project.idea, project.transcript, self._grill)
        if step["action"] == "ask":
            project.pending_question = step["question"]
            project.pending_recommended = step["recommended"]
            self._store.save(project)
            return {
                "project_id": project.id,
                "status": "eliciting",
                "question": step["question"],
                "recommended": step["recommended"],
            }
        # done — a shared understanding is reached
        project.spec = step["spec"]
        project.status = "ready"
        self._store.save(project)
        return {"project_id": project.id, "status": "ready", "spec": project.spec}

    async def approve(self, project_id: str) -> dict:
        """Approve the ready spec: plan it into a DAG and hand it to the executor."""
        project = self._store.get(project_id)
        if project is None:
            raise KeyError(project_id)
        if project.status == "approved":  # idempotent
            return {
                "project_id": project.id,
                "status": "approved",
                "program_id": project.program_id,
            }
        if project.status != "ready" or not project.spec:
            raise ValueError(f"project has no ready spec (status '{project.status}')")
        planned = await self._plan_spec(project.spec, project.workspace_dir)
        program_id = self._queue.start_planned_program(
            goal=project.idea, workspace_dir=project.workspace_dir, planned=planned
        )
        project.status = "approved"
        project.program_id = program_id
        self._store.save(project)
        return {"project_id": project.id, "status": "approved", "program_id": program_id}

    def get(self, project_id: str) -> Optional[Project]:
        return self._store.get(project_id)
