// Theme = a data-theme attribute on <html>; every color lives in index.css as a
// CSS variable, so switching is a single attribute flip (no React re-style).
import { createContext, useContext } from "react";

export type Theme = "dark" | "light";

const STORAGE_KEY = "devclaw:theme";

export function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* storage disabled — fall through */
  }
  return "dark";
}

export function applyTheme(t: Theme): void {
  document.documentElement.dataset.theme = t;
  try {
    localStorage.setItem(STORAGE_KEY, t);
  } catch {
    /* ignore */
  }
}

interface ThemeCtx {
  theme: Theme;
  toggle: () => void;
}

export const ThemeContext = createContext<ThemeCtx>({
  theme: "dark",
  toggle: () => {},
});

export const useTheme = () => useContext(ThemeContext);
