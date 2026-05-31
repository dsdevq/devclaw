"""Filesystem state for build-from-scratch projects.

A project lives through phases: ``eliciting`` (the grill is running) → ``ready``
(a spec is signed-off-able) → ``approved`` (planned + handed to the executor as a
program). Per the design, the human-readable contract lives on disk — not in the
repo — so an operator can read what was agreed and audit the interview.

Layout (under ``$DEVCLAW_STATE/projects/<id>/``):
  project.json   — canonical state (read-modify-write)
  idea.md        — the original ask
  spec.md        — the shared understanding, once ready (human-readable mirror)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_STATE_DIR = os.environ.get("DEVCLAW_STATE", "./.devclaw-state")


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Project:
    id: str
    idea: str
    workspace_dir: str
    status: str  # eliciting | ready | approved
    #: the outstanding question awaiting an answer (None once finalized)
    pending_question: Optional[str] = None
    pending_recommended: Optional[str] = None
    #: completed interview turns: [{question, recommended, answer}]
    transcript: list[dict] = field(default_factory=list)
    spec: Optional[str] = None
    program_id: Optional[str] = None
    created_at: int = field(default_factory=_now_ms)

    def to_dict(self) -> dict:
        return asdict(self)


class ProjectStore:
    def __init__(self, root_dir: str = DEFAULT_STATE_DIR) -> None:
        self._root = Path(root_dir).expanduser() / "projects"

    def _dir(self, project_id: str) -> Path:
        return self._root / project_id

    def create(self, *, idea: str, workspace_dir: str) -> Project:
        project = Project(
            id=str(uuid.uuid4()),
            idea=idea,
            workspace_dir=workspace_dir,
            status="eliciting",
        )
        self.save(project)
        return project

    def get(self, project_id: str) -> Optional[Project]:
        path = self._dir(project_id) / "project.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return Project(**data)

    def save(self, project: Project) -> None:
        d = self._dir(project.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "project.json").write_text(json.dumps(project.to_dict(), indent=2))
        # human-readable mirrors
        (d / "idea.md").write_text(project.idea + "\n")
        if project.spec:
            (d / "spec.md").write_text(project.spec + "\n")

    def record_answer(self, project: Project, answer: str) -> None:
        """Fold the outstanding question + the user's answer into the transcript."""
        if project.pending_question is not None:
            project.transcript.append(
                {
                    "question": project.pending_question,
                    "recommended": project.pending_recommended or "",
                    "answer": answer,
                }
            )
            project.pending_question = None
            project.pending_recommended = None
        self.save(project)
