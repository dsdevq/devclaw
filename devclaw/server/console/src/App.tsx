import { useEffect, useState } from "react";
import { AppShell } from "./components/AppShell";
import { applyTheme, initialTheme, ThemeContext, type Theme } from "./theme";

// Layout route element: owns theme state, provides it, renders the shell.
export function App() {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const toggle = () => setTheme((t) => (t === "dark" ? "light" : "dark"));

  return (
    <ThemeContext.Provider value={{ theme, toggle }}>
      <AppShell />
    </ThemeContext.Provider>
  );
}
