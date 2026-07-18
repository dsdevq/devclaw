import { useEffect, useState } from "react";
import { fetchGoalPrs, mergePr, type PRRow } from "../api";
import { prMeta } from "../status";
import { IconMerge } from "../icons";
import { EmptyState, Modal, StatusDot } from "../ui";

// Per-PR review rows for a goal. Reads /goals/{id}/prs.json (live `gh pr view`
// enriched on the backend). Merge fires POST /prs/merge.

export function PRList({ goalId }: { goalId: string }) {
  const [rows, setRows] = useState<PRRow[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<PRRow | null>(null);

  const load = () => fetchGoalPrs(goalId).then(setRows).catch(() => setRows([]));

  useEffect(() => {
    let alive = true;
    setRows(null);
    fetchGoalPrs(goalId).then((r) => alive && setRows(r)).catch(() => alive && setRows([]));
    return () => {
      alive = false;
    };
  }, [goalId]);

  const doMerge = async (row: PRRow) => {
    setConfirm(null);
    setBusy(row.prUrl);
    setFlash(null);
    try {
      const r = await mergePr(row.prUrl);
      setFlash(r.merged ? `#${row.prNumber} merged` : `merge failed: ${r.error ?? "unknown"}`);
      load();
    } catch (e) {
      setFlash(String(e));
    } finally {
      setBusy(null);
    }
  };

  if (rows === null) return <EmptyState title="Loading pull requests…" />;
  if (rows.length === 0) return <EmptyState title="No pull requests yet" hint="Delivered PRs will appear here." />;

  return (
    <>
      {flash && (
        <div className="mono secondary" style={{ fontSize: 12, marginBottom: 10 }}>{flash}</div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {rows.map((row) => {
          const m = prMeta(row);
          const isBusy = busy === row.prUrl;
          return (
            <div
              key={row.prUrl}
              className="card"
              style={{ display: "flex", alignItems: "center", gap: 12, padding: "11px 14px" }}
            >
              <span className="mono muted" style={{ fontSize: 12, flexShrink: 0 }}>#{row.prNumber}</span>
              <a
                href={row.prUrl}
                target="_blank"
                rel="noreferrer"
                className="truncate"
                style={{ flex: 1, fontSize: 13 }}
                title={row.repo}
              >
                {row.title || row.actionLabel || row.prUrl}
              </a>
              <span className="badge" style={{ color: m.color, flexShrink: 0 }}>
                <StatusDot color={m.color} />
                {m.label}
              </span>
              <button
                className="btn good sm"
                disabled={!m.canMerge || busy !== null}
                onClick={() => setConfirm(row)}
                style={{ flexShrink: 0 }}
              >
                <IconMerge size={14} />
                {isBusy ? "…" : "Merge"}
              </button>
            </div>
          );
        })}
      </div>

      {confirm && (
        <Modal
          title={`Merge ${confirm.repo}#${confirm.prNumber}?`}
          onClose={() => setConfirm(null)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirm(null)}>Cancel</button>
              <button className="btn good" onClick={() => doMerge(confirm)}>Merge & delete branch</button>
            </>
          }
        >
          <div style={{ fontSize: 13.5, lineHeight: 1.55 }}>{confirm.title}</div>
          <div className="mono muted" style={{ fontSize: 12, marginTop: 10 }}>
            Squash-merges into the default branch and deletes the source branch.
          </div>
        </Modal>
      )}
    </>
  );
}
