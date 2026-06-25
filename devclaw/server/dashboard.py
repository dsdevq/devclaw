"""Dashboard presentation — pure HTML renderers, no server wiring.

Split out of ``server.py`` so the HTTP routes there stay thin (fetch data → call
a renderer) and the markup is unit-testable on its own (feed a renderer fake data,
assert the page). Every function here is pure: it takes plain data plus the auth
query-string + version and returns an HTML string. No FastMCP, no stores, no I/O.
"""

from __future__ import annotations

import datetime
from html import escape as _html_escape
from typing import Optional


def esc(s: str) -> str:
    return _html_escape(str(s), quote=True)


def iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()


def phase_class(phase: str) -> str:
    return {
        "done": "ok", "blocked": "bad", "cancelled": "muted", "error": "bad",
        "in_flight": "run", "verifying": "run",
    }.get(phase, "warn")


def health_class(health: str) -> str:
    return {
        "working": "run", "done": "ok", "blocked": "bad",
        "archived": "muted", "idle": "warn",
    }.get(health, "muted")


def preview_cell(p: dict) -> str:
    url = p.get("previewUrl")
    return f'<a href="{esc(url)}">open ↗</a>' if url else "—"


# Shared CSS for the goals/projects/goal-detail pages (the programs list + program
# detail pages keep their own minimal inline styles, preserved verbatim below).
CSS = """
 body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:2rem;color:#eee;background:#0d1117}
 a{color:#7ab8ff;text-decoration:none} a:hover{text-decoration:underline}
 h1{font-size:1.3rem} h2{font-size:1rem;color:#8b949e;margin:1.4rem 0 .4rem;text-transform:uppercase;letter-spacing:.04em}
 table{border-collapse:collapse;width:100%;margin-top:1rem}
 th,td{padding:.45rem .6rem;border-bottom:1px solid #30363d;text-align:left;vertical-align:top}
 th{background:#161b22}
 .pill{display:inline-block;padding:.1rem .5rem;border-radius:1rem;font-size:12px;background:#21262d}
 .ok{color:#3fb950} .warn{color:#d29922} .bad{color:#f85149} .run{color:#79c0ff}
 pre{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:.8rem;white-space:pre-wrap;word-break:break-word;font:12px/1.5 ui-monospace,monospace;max-height:32vh;overflow:auto}
 .nav{color:#8b949e} .muted{color:#8b949e}
 .now{background:#161b22;border:1px solid #30363d;border-left:3px solid #79c0ff;border-radius:6px;padding:.7rem 1rem;margin-top:.6rem}
"""


def render_programs(programs: list, *, version: str, token_qs: str) -> str:
    rows = "".join(
        (
            "<tr>"
            f'<td><a href="/dashboard/{p.id}{token_qs}">{p.id[:8]}</a></td>'
            f"<td>{esc(p.status)}</td>"
            f"<td>{esc(iso(p.created_at))}</td>"
            f"<td>{esc(p.goal[:117] + '...' if len(p.goal) > 120 else p.goal)}</td>"
            "</tr>"
        )
        for p in programs
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>devclaw — programs</title>
<style>
 body{{font:14px/1.4 -apple-system,system-ui,sans-serif;margin:2rem;color:#eee;background:#0d1117}}
 a{{color:#7ab8ff}}
 table{{border-collapse:collapse;width:100%;margin-top:1rem}}
 th,td{{padding:.4rem .6rem;border-bottom:1px solid #30363d;text-align:left}}
 th{{background:#161b22}}
</style></head><body>
<p style="color:#8b949e"><b>programs</b> · <a href="/goals{token_qs}" style="color:#7ab8ff">goals</a> · <a href="/projects{token_qs}" style="color:#7ab8ff">projects</a></p>
<h1>devclaw programs <small>v{esc(version)}</small></h1>
<p>{len(programs)} program(s). Click a row to open the live event stream.</p>
<table><thead><tr><th>id</th><th>status</th><th>created</th><th>goal</th></tr></thead>
<tbody>{rows}</tbody></table>
</body></html>"""


def render_program(program, *, token_qs: str) -> str:
    program_id = program.id
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>devclaw — {esc(program_id)}</title>
<style>
 body{{font:13px/1.4 -apple-system,system-ui,sans-serif;margin:2rem;color:#eee;background:#0d1117}}
 a{{color:#7ab8ff}}
 h1{{font-size:1.2rem}}
 #events{{margin-top:1rem;border:1px solid #30363d;border-radius:6px;padding:1rem;background:#161b22;max-height:80vh;overflow:auto;font-family:ui-monospace,monospace;font-size:12px}}
 .ev{{padding:.2rem 0;border-bottom:1px solid #21262d}}
 .type{{color:#79c0ff;font-weight:bold}} .source{{color:#8b949e}} .id{{color:#6e7681}}
</style></head><body>
<p><a href="/dashboard{token_qs}">&larr; all programs</a></p>
<h1>program {esc(program_id)} <small>({esc(program.status)})</small></h1>
<p>{esc(program.goal)}</p>
<div id="events"></div>
<script>
 const box = document.getElementById('events');
 const src = new EventSource('/programs/{program_id}/events{token_qs}');
 src.onmessage = (e) => {{
   try {{
     const ev = JSON.parse(e.data);
     const div = document.createElement('div');
     div.className = 'ev';
     div.innerHTML = '<span class=id>#' + ev.id + '</span> ' +
                     '<span class=type>' + ev.type + '</span> ' +
                     '<span class=source>(' + ev.source + ')</span>';
     box.appendChild(div);
     box.scrollTop = box.scrollHeight;
   }} catch (err) {{ /* swallow */ }}
 }};
 src.onerror = () => {{ /* browser auto-reconnects with Last-Event-Id */ }};
</script>
</body></html>"""


def render_goals(items: list, *, version: str, token_qs: str) -> str:
    rows = "".join(
        (
            "<tr>"
            f'<td><a href="/goals/{esc(g["id"])}{token_qs}">{esc(g["id"])}</a></td>'
            f'<td><span class="pill {phase_class(g.get("phase",""))}">{esc(g.get("phase",""))}</span></td>'
            f'<td>{esc(g.get("lifecycle") or "")}</td>'
            f'<td>{esc(str(g.get("direction") or "—"))}</td>'
            f'<td>{g.get("actions_dispatched",0)}</td>'
            f'<td>{esc(g.get("objective",""))}</td>'
            "</tr>"
        )
        for g in items
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>devclaw — goals</title><style>{CSS}</style></head><body>
<p class="nav"><a href="/dashboard{token_qs}">programs</a> · <b>goals</b> · <a href="/projects{token_qs}">projects</a></p>
<h1>devclaw goals <small class="muted">v{esc(version)}</small></h1>
<p class="muted">{len(items)} goal(s) · auto-refresh 10s · click a goal for the live view</p>
<table><thead><tr><th>goal</th><th>phase</th><th>lifecycle</th><th>direction</th><th>acts</th><th>objective</th></tr></thead>
<tbody>{rows or '<tr><td colspan=6 class=muted>no goals yet</td></tr>'}</tbody></table>
</body></html>"""


def render_projects(items: list, *, version: str, token_qs: str) -> str:
    rows = "".join(
        (
            "<tr>"
            f'<td>{esc(p["id"])}</td>'
            f'<td>{esc(p["name"])}</td>'
            f'<td><span class="pill {health_class(p["health"])}">{esc(p["health"])}</span></td>'
            f'<td>{esc(p["status"])}</td>'
            f'<td>{len(p["goals"])}</td>'
            f'<td>{preview_cell(p)}</td>'
            f'<td>{esc(p.get("repoUrl") or "—")}</td>'
            "</tr>"
        )
        for p in items
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta http-equiv="refresh" content="15">
<title>devclaw — projects</title><style>{CSS}</style></head><body>
<p class="nav"><a href="/dashboard{token_qs}">programs</a> · <a href="/goals{token_qs}">goals</a> · <b>projects</b></p>
<h1>devclaw projects <small class="muted">v{esc(version)}</small></h1>
<p class="muted">{len(items)} project(s) · auto-refresh 15s</p>
<table><thead><tr><th>id</th><th>name</th><th>health</th><th>status</th><th>goals</th><th>preview</th><th>repo</th></tr></thead>
<tbody>{rows or '<tr><td colspan=7 class=muted>no projects registered yet</td></tr>'}</tbody></table>
</body></html>"""


def render_goal(d: dict, goal_id: str, *, token_qs: str) -> str:
    inf = d.get("in_flight") or {}
    direction = d.get("direction") or {}
    now_bits = []
    if d.get("next"):
        now_bits.append(f"<b>next:</b> {esc(d['next'])}")
    if inf:
        prog_link = ""
        if inf.get("ref_kind") == "program":
            prog_link = f' · <a href="/dashboard/{esc(inf["id"])}{token_qs}">live event stream &rarr;</a>'
        now_bits.append(
            f'<b>in flight:</b> <span class="run">{esc(inf.get("tool",""))}</span> '
            f'<span class=muted>({esc((inf.get("id") or "")[:8])})</span>{prog_link}'
        )
    if d.get("blocked_on"):
        now_bits.append(f'<b class="bad">blocked on:</b> {esc(d["blocked_on"])}')
    now_html = "<br>".join(now_bits) or '<span class="muted">idle — nothing in flight</span>'

    dir_html = ""
    if direction.get("verdict"):
        dir_html = (
            f'<p><b>direction:</b> <span class="pill">{esc(direction.get("verdict",""))}</span> '
            f'{esc(direction.get("note",""))} <span class=muted>{esc(direction.get("at") or "")}</span></p>'
        )

    events = "".join(
        f'<div><span class="run">{esc(e.get("type",""))}</span> '
        f'<span class=muted>({esc(e.get("source",""))})</span></div>'
        for e in (d.get("live_events") or [])
    ) or '<span class="muted">no live events</span>'

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta http-equiv="refresh" content="8">
<title>devclaw goal — {esc(goal_id)}</title><style>{CSS}</style></head><body>
<p class="nav"><a href="/goals{token_qs}">&larr; all goals</a></p>
<h1>{esc(goal_id)} <span class="pill {phase_class(d.get("phase",""))}">{esc(d.get("phase",""))}</span>
 <small class="muted">{esc(d.get("lifecycle") or "")} · {d.get("actions_dispatched",0)} action(s) · auto-refresh 8s</small></h1>
<p>{esc(d.get("objective",""))}</p>
{dir_html}
<h2>working on now</h2>
<div class="now">{now_html}</div>
<h2>done when</h2>
<p class="muted">{esc(d.get("done_when","") or "—")}</p>
<h2>deliveries (what shipped)</h2>
<pre>{esc(d.get("deliveries","") or "nothing shipped yet")}</pre>
<h2>recent activity</h2>
<pre>{esc(d.get("recent_log","") or "—")}</pre>
<h2>live events (last {len(d.get("live_events") or [])})</h2>
<div>{events}</div>
</body></html>"""


def render_not_found(kind: str, ident: str) -> str:
    return f"<p>unknown {kind}: {esc(ident)}</p>"
