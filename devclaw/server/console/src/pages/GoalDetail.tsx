import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { answerGoal, cancelGoal, fetchGoal, resumeGoal, setGoalStrictness, steerGoal, tokenQueryString, type GoalDetail as GD } from "../api";
import { EventFeed } from "../components/EventFeed";
import { GoalRunWindow } from "../components/GoalRunWindow";
import { PRList } from "../components/PRList";
import { MilestoneTasks } from "../components/MilestoneTasks";
import { IconAlert, IconSteer, IconStop } from "../icons";
import { phaseColor, VERDICT_LABEL, verdictColor } from "../status";
import { Badge, ErrorNote, Loading, Modal, StatusDot, Tabs } from "../ui";

type Tab = "timeline" | "tasks" | "prs" | "activity" | "schedule";

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`;
  return String(n);
}

function shortTime(ms: number | null): string {
  if (ms === null) return "—";
  const d = new Date(ms);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n: number) => String(n).padStart(2, "0");
  return sameDay ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : `${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
}

export function GoalDetail() {
  const { id } = useParams<{ id: string }>();
  const qs = tokenQueryString();
  const [data, setData] = useState<GD | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("timeline");
  const [busy, setBusy] = useState<"cancel" | "steer" | "resume" | "answer" | "strictness" | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [steerOpen, setSteerOpen] = useState(false);
  const [steerMsg, setSteerMsg] = useState("");
  const [cancelOpen, setCancelOpen] = useState(false);
  const [answerOpen, setAnswerOpen] = useState(false);
  const [answers, setAnswers] = useState<Record<string, string>>({});

  const terminal = ["done", "cancelled", "achieved", "error"].includes(data?.phase ?? "");

  const reload = () => id && fetchGoal(id).then(setData).catch((e) => setErr(String(e)));

  useEffect(() => {
    if (!id) return;
    let alive = true;
    setData(null);
    setErr(null);
    fetchGoal(id).then((d) => alive && setData(d)).catch((e) => alive && setErr(String(e)));
    return () => {
      alive = false;
    };
  }, [id]);

  const doSteer = async () => {
    if (!id || !steerMsg.trim()) return;
    setBusy("steer");
    setSteerOpen(false);
    try {
      await steerGoal(id, steerMsg.trim());
      setFlash("Steer sent");
      setSteerMsg("");
      reload();
    } catch (e) {
      setFlash(String(e));
    } finally {
      setBusy(null);
    }
  };

  const doCancel = async () => {
    if (!id) return;
    setBusy("cancel");
    setCancelOpen(false);
    try {
      const r = await cancelGoal(id);
      setFlash(r.cancelled ? "Goal cancelled" : `No-op: ${r.reason ?? r.phase}`);
      reload();
    } catch (e) {
      setFlash(String(e));
    } finally {
      setBusy(null);
    }
  };

  const doResume = async () => {
    if (!id) return;
    setBusy("resume");
    try {
      const r = await resumeGoal(id);
      setFlash(r.resumed ? "Resumed — re-attempting on the next tick" : r.message ?? "Not resumable");
      reload();
    } catch (e) {
      setFlash(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(null);
    }
  };

  const doAnswer = async () => {
    if (!id) return;
    setBusy("answer");
    setAnswerOpen(false);
    try {
      await answerGoal(id, answers);
      setFlash("Answers sent");
      setAnswers({});
      reload();
    } catch (e) {
      setFlash(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(null);
    }
  };

  const doToggleStrictness = async () => {
    if (!id || !data) return;
    const next = data.strictness === "strict" ? "trust" : "strict";
    setBusy("strictness");
    try {
      await setGoalStrictness(id, next);
      setFlash(
        next === "strict"
          ? "Gate: strict — dial-able gate failures now block"
          : "Gate: trust — dial-able gate failures ship with a caveat in the PR",
      );
      reload();
    } catch (e) {
      setFlash(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(null);
    }
  };

  const hasUnknowns = (data?.unknowns?.length ?? 0) > 0;
  const answersComplete =
    hasUnknowns && (data?.unknowns ?? []).every((u) => (answers[u.id] ?? "").trim());

  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "timeline", label: "Timeline" },
    { id: "tasks", label: "Tasks", count: data?.tasks?.length },
    { id: "prs", label: "Pull requests" },
    { id: "activity", label: "Activity" },
    { id: "schedule", label: "Schedule" },
  ];

  return (
    <div className="page" style={{ maxWidth: 980 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
        <Link to={`/goals${qs}`} className="secondary">Goals</Link>
        <span className="muted">›</span>
        {data && <span className="mono muted">{id}</span>}
      </div>

      {err && <ErrorNote>{err}</ErrorNote>}
      {!data && !err && <Loading />}

      {data && (
        <>
          <div style={{ margin: "16px 0 18px" }}>
            <p style={{ fontSize: 15.5, lineHeight: 1.55, margin: "0 0 16px", maxWidth: 820 }}>
              {data.objective || "—"}
            </p>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Badge k="Phase" dot={phaseColor(data.phase)}>{data.phaseLabel}</Badge>
                <Badge k="Lifecycle">{data.lifecycle ?? "—"}</Badge>
                {data.direction && (
                  <Badge k="Verdict" dot={verdictColor(data.direction.verdict)}>
                    {VERDICT_LABEL[data.direction.verdict] ?? data.direction.verdict}
                  </Badge>
                )}
                {data.dispatchCap > 0 && (
                  <Badge k="Dispatched">{data.actionsDispatched} / {data.dispatchCap}</Badge>
                )}
                {data.usage && data.usage.totalTokens > 0 && (
                  <Badge k="Usage" title={`cognition ${fmtTokens(data.usage.cognitionTokensIn + data.usage.cognitionTokensOut)} tok · workers ${fmtTokens(data.usage.workerInputTokens + data.usage.workerOutputTokens)} tok over ${data.usage.tasksWithUsage} task${data.usage.tasksWithUsage === 1 ? "" : "s"}`}>
                    {fmtTokens(data.usage.totalTokens)} tok
                    {data.usage.totalCostUsd > 0 ? ` · $${data.usage.totalCostUsd.toFixed(2)}` : ""}
                  </Badge>
                )}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                {flash && <span className="mono secondary" style={{ fontSize: 12 }}>{flash}</span>}
                <button
                  className="btn sm"
                  disabled={busy !== null || terminal}
                  title={
                    data.strictness === "strict"
                      ? "Strict — browser & review gate failures BLOCK this goal. Click to switch to Trust."
                      : "Trust — browser & review gate failures ship with a caveat in the PR (the human merge is the backstop). Click to switch to Strict."
                  }
                  onClick={doToggleStrictness}
                >
                  {busy === "strictness" ? "…" : `Gate: ${data.strictness === "strict" ? "Strict" : "Trust"}`}
                </button>
                <button className="btn danger sm" disabled={busy !== null || terminal} onClick={() => setCancelOpen(true)}>
                  <IconStop size={14} /> Cancel
                </button>
                <button className="btn primary sm" disabled={busy !== null || terminal} onClick={() => setSteerOpen(true)}>
                  <IconSteer size={14} /> Steer
                </button>
              </div>
            </div>
          </div>

          {data.phase === "blocked" && (
            <BlockedBanner
              blockedOn={data.blockedOn}
              hasUnknowns={hasUnknowns}
              busy={busy}
              onResume={doResume}
              onAnswer={() => setAnswerOpen(true)}
            />
          )}

          <Tabs tabs={tabs} active={tab} onChange={setTab} />

          <div style={{ paddingTop: 22 }}>
            {tab === "timeline" && <Timeline data={data} />}
            {tab === "tasks" && (
              <MilestoneTasks tasks={data.tasks ?? []} emptyLabel="No tasks dispatched yet — the heartbeat files them here." />
            )}
            {tab === "prs" && <PRList goalId={data.id} />}
            {tab === "activity" && <EventFeed goalId={data.id} />}
            {tab === "schedule" && <GoalRunWindow goalId={data.id} />}
          </div>
        </>
      )}

      {steerOpen && (
        <Modal
          title="Steer this goal"
          onClose={() => setSteerOpen(false)}
          footer={
            <>
              <button className="btn" onClick={() => setSteerOpen(false)}>Cancel</button>
              <button className="btn primary" disabled={!steerMsg.trim()} onClick={doSteer}>Send steer</button>
            </>
          }
        >
          <p className="secondary" style={{ fontSize: 12.5, margin: "0 0 12px" }}>
            A nudge to the goal's direction — applied on the next heartbeat. It refines the objective, it doesn't replace it.
          </p>
          <textarea
            className="field"
            rows={4}
            autoFocus
            value={steerMsg}
            onChange={(e) => setSteerMsg(e.target.value)}
            placeholder="e.g. Prioritize the settings page before the dashboard."
          />
        </Modal>
      )}

      {cancelOpen && (
        <Modal
          title={`Cancel ${id}?`}
          onClose={() => setCancelOpen(false)}
          footer={
            <>
              <button className="btn" onClick={() => setCancelOpen(false)}>Keep running</button>
              <button className="btn danger" onClick={doCancel}>Cancel goal</button>
            </>
          }
        >
          <p style={{ fontSize: 13.5, lineHeight: 1.55, margin: 0 }}>
            This tears down any in-flight work and moves the goal to a terminal state. It can't be resumed — you'd file a new goal.
          </p>
        </Modal>
      )}

      {answerOpen && data && (
        <Modal
          title="Answer to unblock"
          onClose={() => setAnswerOpen(false)}
          footer={
            <>
              <button className="btn" onClick={() => setAnswerOpen(false)}>Cancel</button>
              <button className="btn primary" disabled={!answersComplete} onClick={doAnswer}>Send answers</button>
            </>
          }
        >
          <p className="secondary" style={{ fontSize: 12.5, margin: "0 0 16px" }}>
            The goal is waiting on these before it can proceed. Answer every question.
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {data.unknowns.map((u) => (
              <div key={u.id}>
                <div style={{ fontSize: 13, fontWeight: 550, marginBottom: 2 }}>{u.question}</div>
                {u.why && <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>{u.why}</div>}
                {(u.options ?? []).length > 0 && (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 6 }}>
                    {(u.options ?? []).map((opt) => (
                      <button
                        key={opt}
                        type="button"
                        className={`btn${(answers[u.id] ?? "") === opt ? " primary" : ""}`}
                        style={{ fontSize: 12 }}
                        onClick={() => setAnswers((a) => ({ ...a, [u.id]: opt }))}
                      >
                        {opt}
                      </button>
                    ))}
                  </div>
                )}
                <textarea
                  className="field"
                  rows={2}
                  value={answers[u.id] ?? ""}
                  onChange={(e) => setAnswers((a) => ({ ...a, [u.id]: e.target.value }))}
                />
                {u.defaultIfNoAnswer && (
                  <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
                    Suggested default: {u.defaultIfNoAnswer}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Modal>
      )}
    </div>
  );
}

function BlockedBanner({
  blockedOn,
  hasUnknowns,
  busy,
  onResume,
  onAnswer,
}: {
  blockedOn: string | null;
  hasUnknowns: boolean;
  busy: string | null;
  onResume: () => void;
  onAnswer: () => void;
}) {
  const isDispatchCap = (blockedOn ?? "").toLowerCase().includes("dispatch cap");
  return (
    <div
      className="card"
      style={{ display: "flex", gap: 12, padding: "13px 15px", margin: "0 0 20px", alignItems: "center", borderColor: "color-mix(in srgb, var(--amber) 45%, var(--border))", background: "var(--amber-soft)" }}
    >
      <span style={{ color: "var(--amber)", flexShrink: 0 }}><IconAlert size={17} /></span>
      <div style={{ flex: 1, minWidth: 0, fontSize: 13, lineHeight: 1.5 }}>
        <span style={{ fontWeight: 600 }}>Blocked — waiting on you.</span>{" "}
        <span className="secondary">{blockedOn ?? "Reason unknown — check the activity log."}</span>
        {isDispatchCap && <span className="muted"> Merge an open PR under Pull requests to unblock the loop.</span>}
      </div>
      {hasUnknowns ? (
        <button className="btn primary sm" disabled={busy !== null} onClick={onAnswer} style={{ flexShrink: 0 }}>
          Answer
        </button>
      ) : (
        <button className="btn primary sm" disabled={busy !== null} onClick={onResume} style={{ flexShrink: 0 }}>
          {busy === "resume" ? "…" : "Resume"}
        </button>
      )}
    </div>
  );
}

function Timeline({ data }: { data: GD }) {
  const nodes = data.timeline;
  const current = nodes.findIndex((n) => n.current);
  const pct = nodes.length > 1 ? ((current === -1 ? 0 : current) / (nodes.length - 1)) * 100 : 0;
  return (
    <div className="card" style={{ padding: "28px 26px" }}>
      <div style={{ position: "relative", margin: "0 6px" }}>
        <div style={{ position: "absolute", top: 5, left: 0, right: 0, height: 2, background: "var(--border)" }} />
        <div style={{ position: "absolute", top: 5, left: 0, width: `${pct}%`, height: 2, background: "var(--accent)" }} />
        <div style={{ position: "relative", display: "flex", justifyContent: "space-between" }}>
          {nodes.map((n) => (
            <div key={n.name} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 8, width: 74 }}>
              <StatusDot
                color={n.current || n.reached ? "var(--accent)" : "var(--border-strong)"}
                live={n.current}
              />
              <span style={{ fontSize: 11.5, fontWeight: n.current ? 600 : 500, color: n.current ? "var(--text)" : n.reached ? "var(--text-secondary)" : "var(--text-muted)", textAlign: "center" }}>
                {n.name}
              </span>
              <span className="mono muted" style={{ fontSize: 10 }}>{shortTime(n.timestampMs)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
