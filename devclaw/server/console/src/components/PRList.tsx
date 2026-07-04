import { useEffect, useState } from "react";
import { fetchGoalPrs, mergePr, type PRRow } from "../api";
import { mono, palettes } from "../theme";

// PR#8: per-PR review row on Goal Detail. Reads /goals/{id}/prs.json (traces →
// delivery → pr_url, enriched with a live `gh pr view` probe on the backend
// so state/mergeable are always current). The Merge button fires POST
// /prs/merge which shells `gh pr merge --squash --delete-branch` — same
// merge policy the mission chain already uses. Refetches after a successful
// merge so the row flips to MERGED without a page reload.

type Props = { goalId: string };

export function PRList({ goalId }: Props) {
  const p = palettes.dark;
  const [rows, setRows] = useState<PRRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const load = () => {
    fetchGoalPrs(goalId)
      .then((r) => setRows(r))
      .catch((e) => setErr(String(e)));
  };

  useEffect(() => {
    let alive = true;
    setRows(null);
    setErr(null);
    fetchGoalPrs(goalId)
      .then((r) => alive && setRows(r))
      .catch((e) => alive && setErr(String(e)));
    return () => {
      alive = false;
    };
  }, [goalId]);

  if (err) return null; // silent — the page already surfaces goal errors.
  if (rows === null || rows.length === 0) return null;

  const onMerge = async (row: PRRow) => {
    if (busy) return;
    if (!window.confirm(`Merge ${row.repo}#${row.prNumber}?\n\n${row.title}`)) return;
    setBusy(row.prUrl);
    setFlash(null);
    try {
      const r = await mergePr(row.prUrl);
      setFlash(
        r.merged
          ? `#${row.prNumber} merged`
          : `merge failed: ${r.error ?? "unknown"}`,
      );
      load();
    } catch (e) {
      setFlash(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div
      style={{
        flexShrink: 0,
        borderBottom: `1px solid ${p.border}`,
        padding: "14px 40px 16px",
        boxSizing: "border-box",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 10,
        }}
      >
        <div
          style={{
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: "0.06em",
            textTransform: "uppercase",
            color: p.textSecondary,
          }}
        >
          Pull requests ({rows.length})
        </div>
        {flash && (
          <span
            style={{
              fontFamily: mono,
              fontSize: 11.5,
              color: p.textSecondary,
            }}
          >
            {flash}
          </span>
        )}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {rows.map((row) => (
          <PRRowView
            key={row.prUrl}
            row={row}
            busy={busy === row.prUrl}
            anyBusy={busy !== null}
            onMerge={() => onMerge(row)}
          />
        ))}
      </div>
    </div>
  );
}

function PRRowView({
  row,
  busy,
  anyBusy,
  onMerge,
}: {
  row: PRRow;
  busy: boolean;
  anyBusy: boolean;
  onMerge: () => void;
}) {
  const p = palettes.dark;
  const canMerge = row.state === "OPEN" && row.mergeable === "MERGEABLE";
  const stateColor =
    row.state === "MERGED"
      ? p.green
      : row.state === "CLOSED"
        ? p.textMuted
        : row.mergeable === "CONFLICTING"
          ? p.red
          : row.mergeable === "MERGEABLE"
            ? p.green
            : p.amber;
  const stateLabel = row.state === "OPEN" ? row.mergeable : row.state;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "8px 10px",
        border: `1px solid ${p.border}`,
        borderRadius: 5,
        fontSize: 13,
        minHeight: 34,
      }}
    >
      <span
        style={{
          fontFamily: mono,
          fontSize: 11.5,
          color: p.textMuted,
          minWidth: 60,
        }}
      >
        #{row.prNumber}
      </span>
      <a
        href={row.prUrl}
        target="_blank"
        rel="noreferrer"
        title={row.repo}
        style={{
          flex: 1,
          minWidth: 0,
          color: p.textPrimary,
          textDecoration: "none",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {row.title || row.actionLabel || row.prUrl}
      </a>
      <span
        style={{
          fontFamily: mono,
          fontSize: 10.5,
          color: stateColor,
          border: `1px solid ${stateColor}`,
          borderRadius: 3,
          padding: "2px 6px",
          textTransform: "uppercase",
          letterSpacing: "0.04em",
          flexShrink: 0,
        }}
      >
        {stateLabel}
      </span>
      <button
        disabled={!canMerge || anyBusy}
        onClick={onMerge}
        style={{
          background: canMerge ? p.green : "transparent",
          color: canMerge ? "#0b0b0b" : p.textMuted,
          border: `1px solid ${canMerge ? p.green : p.border}`,
          padding: "5px 12px",
          borderRadius: 4,
          fontSize: 12,
          fontWeight: 600,
          fontFamily: "'Inter', sans-serif",
          cursor: !canMerge || anyBusy ? "not-allowed" : "pointer",
          opacity: !canMerge || anyBusy ? 0.5 : 1,
          flexShrink: 0,
        }}
      >
        {busy ? "…" : "Merge"}
      </button>
    </div>
  );
}
