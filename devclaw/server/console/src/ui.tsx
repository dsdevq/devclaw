// Thin primitives over the index.css design system. Components stay tiny — the
// styling lives in CSS classes + variables, not inline objects.
import { useEffect, type ReactNode } from "react";

export function StatusDot({ color, live }: { color: string; live?: boolean }) {
  return (
    <span
      className={`dc-dot${live ? " live" : ""}`}
      style={{ background: color, ...(live ? { ["--pulse" as string]: color } : {}) }}
    />
  );
}

export function Badge({
  k,
  children,
  dot,
}: {
  k?: string;
  children: ReactNode;
  dot?: string;
}) {
  return (
    <span className="badge">
      {dot && <StatusDot color={dot} />}
      {k && <span className="k">{k}</span>}
      <span>{children}</span>
    </span>
  );
}

export function SectionLabel({
  children,
  count,
  right,
}: {
  children: ReactNode;
  count?: number;
  right?: ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: 12,
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <span className="eyebrow">{children}</span>
        {count !== undefined && (
          <span className="mono muted" style={{ fontSize: 11 }}>
            {count}
          </span>
        )}
      </div>
      {right}
    </div>
  );
}

export function Tabs<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: T; label: string; count?: number }[];
  active: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={t.id === active}
          className={`tab${t.id === active ? " active" : ""}`}
          onClick={() => onChange(t.id)}
        >
          {t.label}
          {t.count !== undefined && <span className="count">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="scrim" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
        <div
          style={{
            padding: "16px 20px",
            borderBottom: "1px solid var(--border)",
            fontSize: 14,
            fontWeight: 600,
          }}
        >
          {title}
        </div>
        <div style={{ padding: 20 }}>{children}</div>
        {footer && (
          <div
            style={{
              padding: "14px 20px",
              borderTop: "1px solid var(--border)",
              display: "flex",
              justifyContent: "flex-end",
              gap: 10,
            }}
          >
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div
      style={{
        padding: "48px 24px",
        textAlign: "center",
        color: "var(--text-muted)",
      }}
    >
      <div style={{ fontSize: 13.5, color: "var(--text-secondary)", marginBottom: 4 }}>
        {title}
      </div>
      {hint && <div style={{ fontSize: 12.5 }}>{hint}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div style={{ padding: "40px 6px", fontSize: 13, color: "var(--text-muted)" }}>
      {label}
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div style={{ padding: "16px 0", fontSize: 13, color: "var(--red)" }}>{children}</div>
  );
}
