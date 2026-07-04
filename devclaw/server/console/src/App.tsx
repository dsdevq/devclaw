import { Outlet } from "react-router-dom";
import { palettes, sans } from "./theme";

export function App() {
  const p = palettes.dark;
  return (
    <div
      style={{
        height: "100vh",
        overflow: "hidden",
        background: p.bg,
        color: p.textPrimary,
        fontFamily: sans,
      }}
    >
      <Outlet />
    </div>
  );
}
