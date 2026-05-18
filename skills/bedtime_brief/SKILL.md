---
name: bedtime_brief
description: "Compose a calming end-of-day reflection for the operator and deliver it to Telegram. Mirrors morning_brief but fires at night. Invoked by the openclaw cron at 22:00 Europe/Dublin daily. Reads ~/.life/PLAN.md, today's queue.jsonl entries, and relevant domain files to summarise what happened, what's open for tomorrow, and anything blocked or escalated. NEVER use this skill interactively in chat — it is a scheduled brief generator. Output is short, calming, and bedtime-framed."
schedule: "0 22 * * *"
timezone: Europe/Dublin
delivery: telegram:owner
---

# bedtime_brief

You are composing the operator's **end-of-day reflection** — a short, calming brief sent to his Telegram at 22:00 Dublin time. This is the bedtime counterpart to `morning_brief` (07:30 Dublin). The two share structure; the framing differs.

This skill is invoked by an openclaw cron entry on the VPS. It is NOT for ad-hoc chat use. After composing the brief and handing it off to the delivery layer, control returns to the scheduler.

## Recommended openclaw cron entry

```yaml
schedule: "0 22 * * *"
timezone: Europe/Dublin
skill: bedtime_brief
delivery: telegram:owner
```

The cron file itself lives on the VPS (not in this repo). This block is the source-of-truth for what the schedule should be — if the VPS cron drifts, reconcile back to this.

## Hard behavioral rules

- **Read-only over operator state.** This skill never mutates `~/.life/`. It only reads PLAN.md, queue.jsonl, and domain files. Any insight that needs to be persisted goes back through `task_intake` or `propose_change` on the operator's own terms tomorrow.
- **One delivery per invocation.** If the cron fires twice (clock skew, retry), the second run still composes fresh — there is no dedupe layer here. Keep the brief short enough that a duplicate is annoying but not harmful.
- **Tone is calming.** Short sentences. No urgency language ("URGENT", "BLOCKED!!", exclamation points). Bedtime framing — the operator is winding down, not starting the day.
- **No new asks.** The morning brief opens loops; the bedtime brief closes them. Do NOT propose new tasks, do NOT ask the operator to decide anything tonight. If something needs a decision, surface it as "tomorrow" not "now".
- **Length cap.** Target ≤ 12 lines of Telegram message body. Anything longer reads as nagging.

## Inputs you receive

- The cron firing context — current time, timezone, target chat id.
- Read access to `~/.life/PLAN.md`.
- Read access to `~/.life/queue.jsonl` (operator's ambient log).
- Read access to `~/.life/domains/*.md` (food, mood, fitness, work, etc.).
- Read access to `~/.life/tasks/<id>/` for currently-open task specs.
- Read access to `~/.life/projects/<slug>/runs/` for any in-flight Runs.

## Sequence

### 1. Scan today's queue.jsonl entries

```bash
TODAY=$(TZ=Europe/Dublin date +%Y-%m-%d)
jq -c --arg d "$TODAY" 'select(.ts | startswith($d))' ~/.life/queue.jsonl
```

Group by domain tag (food, mood, fitness, work, etc.). The goal is a one-line summary per domain, not a literal replay. If a domain had no entries today, omit it from the brief — silence is fine.

### 2. Identify open Runs and tasks for tomorrow

- List `~/.life/tasks/*/spec.yaml` where `status: ready` or `status: dispatched` and `completed_at` is null.
- List `~/.life/projects/*/runs/*.md` where the Run is in-flight (no `closed_at` field).

These become the "tomorrow" section. Keep it to titles only — the operator does not need spec contents at bedtime.

### 3. Identify anything blocked or escalated

Scan today's queue.jsonl and the run.log.jsonl of any task dispatched today. Surface anything tagged `blocked`, `escalation`, `decision_pending`, or where a task spec emitted `status: blocked` with a blocker reason.

If nothing is blocked, say so explicitly with one calming line ("nothing snagged today"). Empty sections are worse than a one-line "all clear" — the operator should be able to scan and trust the brief is complete.

### 4. Compose the brief

Template:

```
🌙 bedtime · <YYYY-MM-DD>

Today
- <one line per domain that had activity>

Tomorrow
- <open Run or task titles, max 5>

Blocked
- <blocker lines, or "nothing snagged today">

Sleep well.
```

Trim aggressively. If "Today" has nothing notable, replace the bullets with a single line ("a quiet one"). If "Tomorrow" is empty, write "nothing on the board — your call in the morning."

### 5. Deliver via Telegram

Hand the composed text to the openclaw delivery layer (`telegram:owner`). Do NOT shell out to a Telegram bot directly — the delivery layer handles retries, rate limits, and chat-id resolution.

If delivery fails, log to `~/.life/system/bedtime_brief.log` and exit non-zero. The cron wrapper will surface the failure on the next morning's brief.

## What this skill is not

- Not a journal — the operator's own queue.jsonl is the journal. This skill summarises, it does not record.
- Not a task creator — see `task_intake`. Bedtime is the wrong moment to open loops.
- Not a status dashboard — it is a calming sign-off, not a metrics report. Resist the urge to add counts, streaks, or progress bars.
- Not a morning skill — see `morning_brief` for the 07:30 Dublin counterpart that opens the day with PLAN.md priorities and forward-looking framing.
