"""Periodic audits that run inside the orchestrator daemon.

Each audit is pure-Python mechanism (zero LLM tokens per tick). If an audit
detects drift, it can optionally spawn a code_task spec to fix it — but v1
just writes a report to ~/.life/audits/ and logs the summary.
"""
