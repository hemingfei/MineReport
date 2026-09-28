import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { AuthProvider, RequireRole } from "./auth";
import { AppLayout } from "./components/AppLayout";
import {
  InvitationsPage,
  LoginPage,
  NotFoundPage,
  RegisterPage,
  ReportDetailPage,
  ReportsPage,
  UploadPage,
} from "./pages";
import "./styles.css";

const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  { path: "/register", element: <RegisterPage /> },
  {
    path: "/",
    element: (
      <RequireRole>
        <AppLayout />
      </RequireRole>
    ),
    children: [
      { index: true, element: <ReportsPage /> },
      {
        path: "upload",
        element: (
          <RequireRole min="analyst">
            <UploadPage />
          </RequireRole>
        ),
      },
      { path: "reports/:id", element: <ReportDetailPage /> },
      {
        path: "invitations",
        element: (
          <RequireRole min="admin">
            <InvitationsPage />
          </RequireRole>
        ),
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthProvider>
      <RouterProvider router={router} />
    </AuthProvider>
  </StrictMode>,
);
