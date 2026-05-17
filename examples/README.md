# Examples

This directory will hold sanitized examples of artifacts produced by devclaw:

- A sample `plan.md` (the project understanding produced by `project_init`)
- A sample `recon.md` (a real recon of a small public repo)
- A sample proposal RFC (`proposals/<date>-<slug>.md`)
- A sample `dag.yaml` (`runs/<run-slug>/dag.yaml`) — the DAG `define_run` generates
- A sample `spec.yaml` and `result.json` — what runners read and write

**Not populated in v0.1.** Coming as part of the v0.2 polish pass once devclaw has been dogfooded against a few different projects and we have real-world artifacts worth canonicalizing.

If you're trying to figure out the shape of these files right now, the canonical schemas live in:

- [`docs/architecture-tasks.md`](../docs/architecture-tasks.md) §5 — `spec.yaml`, `result.json`, `run.log.jsonl`
- [`docs/architecture-curator.md`](../docs/architecture-curator.md) §5 — `plan.md` frontmatter, `proposals/<slug>.md`, `dag.yaml`, `status.yaml`, `settings.yaml`
