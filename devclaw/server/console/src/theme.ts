// Palette + type tokens lifted verbatim from the Claude Design handoff
// (Projects Home / Project Detail / Goal Detail). Do NOT drift these values —
// the design is the spec.

export type Theme = "dark" | "light";

export interface Palette {
  bg: string;
  rowHover: string;
  rowSelectedBg: string;
  border: string;
  borderStrong: string;
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
  accent: string;
  green: string;
  amber: string;
  red: string;
}

export const palettes: Record<Theme, Palette> = {
  dark: {
    bg: "#0A0A0B",
    rowHover: "#141417",
    rowSelectedBg: "rgba(94,106,210,0.10)",
    border: "rgba(255,255,255,0.08)",
    borderStrong: "rgba(255,255,255,0.13)",
    textPrimary: "#EDEDEF",
    textSecondary: "#8B8B90",
    textMuted: "#5C5C61",
    accent: "#5E6AD2",
    green: "#33D17A",
    amber: "#E3B341",
    red: "#F0554C",
  },
  light: {
    bg: "#FFFFFF",
    rowHover: "#F5F5F6",
    rowSelectedBg: "rgba(79,91,213,0.07)",
    border: "rgba(0,0,0,0.08)",
    borderStrong: "rgba(0,0,0,0.13)",
    textPrimary: "#131316",
    textSecondary: "#68686E",
    textMuted: "#9A9AA0",
    accent: "#4F5BD5",
    green: "#1A9F5C",
    amber: "#A66A17",
    red: "#C93C31",
  },
};

export const mono = "'JetBrains Mono', ui-monospace, monospace";
export const sans = "'Inter', system-ui, sans-serif";
