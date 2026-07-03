import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes, Navigate } from "react-router-dom";
import { App } from "./App";
import { ProjectsHome } from "./pages/ProjectsHome";
import { ProjectDetail } from "./pages/ProjectDetail";
import { GoalDetail } from "./pages/GoalDetail";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter basename="/console">
      <Routes>
        <Route element={<App />}>
          <Route index element={<ProjectsHome />} />
          <Route path="projects/:id" element={<ProjectDetail />} />
          <Route path="goals/:id" element={<GoalDetail />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
