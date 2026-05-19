"""State-currency audit — flags drift between documented and actual state.

Stage 1 (this module): scan canonical docs for retired terms; check that
expected-live containers/files are present. Pure Python, zero LLM tokens.

Stage 2 (future): if drift detected, write a code_task spec so the orchestrator
sweep picks it up and opens a PR with corrections. Not implemented in v1.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).with_name("state_currency.yaml")


@dataclass
class RetiredHit:
    term: str
    file: str
    line_no: int
    line: str
    replacement: str | None
    retired_on: str
    reason: str


@dataclass
class MissingComponent:
    kind: str  # "docker" | "file"
    name: str


@dataclass
class AuditReport:
    generated_at: str
    retired_hits: list[RetiredHit] = field(default_factory=list)
    missing_components: list[MissingComponent] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.retired_hits or self.missing_components)


def _load_config(config_path: Path) -> dict:
    return yaml.safe_load(config_path.read_text())


def _iter_in_scope(life_root: Path, include_globs: list[str]) -> Iterable[Path]:
    seen: set[Path] = set()
    for pattern in include_globs:
        for path in life_root.glob(pattern):
            if path.is_file() and path not in seen:
                seen.add(path)
                yield path


def _compile_term(term: str) -> re.Pattern[str]:
    # Identifier-shaped terms get \b word boundaries; punctuated terms
    # (e.g. "swarm-langgraph.service") match as substrings.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", term):
        return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    return re.compile(re.escape(term), re.IGNORECASE)


def _scan_retired(
    life_root: Path,
    scope: dict,
    retired_terms: list[dict],
) -> list[RetiredHit]:
    compiled = [(t, _compile_term(t["term"])) for t in retired_terms]
    hits: list[RetiredHit] = []
    for path in _iter_in_scope(life_root, scope["include"]):
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        rel = str(path.relative_to(life_root))
        for i, line in enumerate(lines, start=1):
            for term_cfg, pattern in compiled:
                if pattern.search(line):
                    hits.append(
                        RetiredHit(
                            term=term_cfg["term"],
                            file=rel,
                            line_no=i,
                            line=line.rstrip(),
                            replacement=term_cfg.get("replacement"),
                            retired_on=str(term_cfg.get("retired_on", "")),
                            reason=term_cfg.get("reason", ""),
                        )
                    )
    return hits


def _check_components(life_root: Path, expected: dict) -> list[MissingComponent]:
    missing: list[MissingComponent] = []

    containers = expected.get("docker_containers") or []
    if containers and shutil.which("docker"):
        try:
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            running = set(result.stdout.split())
            for name in containers:
                if name not in running:
                    missing.append(MissingComponent(kind="docker", name=name))
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("docker ps failed: %s", exc)
    # If docker isn't on PATH we skip silently — audit running outside its env.

    for rel in expected.get("files", []):
        if not (life_root / rel).exists():
            missing.append(MissingComponent(kind="file", name=rel))

    return missing


def run_audit(life_root: Path, config_path: Path = CONFIG_PATH) -> AuditReport:
    config = _load_config(config_path)
    return AuditReport(
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        retired_hits=_scan_retired(life_root, config["scope"], config["retired_terms"]),
        missing_components=_check_components(life_root, config.get("expected_live", {})),
    )


def write_report(report: AuditReport, life_root: Path) -> Path:
    today = dt.date.today().isoformat()
    audits_dir = life_root / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)

    md_path = audits_dir / f"{today}-state-currency.md"
    json_path = audits_dir / f"{today}-state-currency.json"

    lines = [
        "# State-currency audit",
        f"_Generated: {report.generated_at}_",
        "",
    ]
    if not report.has_drift:
        lines.append("No drift detected.")
    else:
        if report.retired_hits:
            lines.append(f"## Retired terms still in canonical docs ({len(report.retired_hits)})")
            lines.append("")
            for h in report.retired_hits:
                tail = f" -> **{h.replacement}**" if h.replacement else ""
                lines.append(f"- `{h.file}:{h.line_no}` — `{h.term}`{tail}")
                lines.append(f"  > {h.line}")
                if h.reason:
                    lines.append(f"  _({h.reason}, retired {h.retired_on})_")
            lines.append("")
        if report.missing_components:
            lines.append(f"## Expected-live components missing ({len(report.missing_components)})")
            lines.append("")
            for m in report.missing_components:
                lines.append(f"- `{m.kind}`: `{m.name}`")
            lines.append("")
    md_path.write_text("\n".join(lines))

    json_path.write_text(
        json.dumps(
            {
                "generated_at": report.generated_at,
                "has_drift": report.has_drift,
                "retired_hits": [asdict(h) for h in report.retired_hits],
                "missing_components": [asdict(m) for m in report.missing_components],
            },
            indent=2,
        )
    )

    return md_path


def run_and_write(life_root: Path) -> tuple[AuditReport, Path]:
    """Convenience entry point used by the daemon."""
    report = run_audit(life_root)
    report_path = write_report(report, life_root)
    return report, report_path
