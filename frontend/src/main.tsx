import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider, useRouteError } from "react-router-dom";
import { AuthProvider, RequireRole } from "./auth";
import { AppLayout } from "./components/AppLayout";
import { EmptyState } from "./components/EmptyState";
import {
  ConnectorLogPage,
  InvitationsPage,
  LoginPage,
  NotFoundPage,
  RegisterPage,
  ReportDetailPage,
  ReportsPage,
  SubscriptionsPage,
  TargetQueuePage,
  ThemeDetailPage,
  ThemesPage,
  SynthesisPage,
  UploadPage,
} from "./pages";
import "./styles.css";

/** 路由渲染兜底：页面抛错时给出可恢复的界面，而不是框架默认的开发者错误页。 */
function RouteError() {
  const error = useRouteError();
  const message = error instanceof Error ? error.message : String(error ?? "未知错误");
  return (
    <EmptyState
      title="页面出错了"
      hint={message}
      action={
        <a className="btn btn-ghost" href="/">
          返回研报库
        </a>
      }
    />
  );
}

const router = createBrowserRouter([
  { path: "/login", element: <LoginPage />, errorElement: <RouteError /> },
  { path: "/register", element: <RegisterPage />, errorElement: <RouteError /> },
  {
    path: "/",
    element: (
      <RequireRole>
        <AppLayout />
      </RequireRole>
    ),
    errorElement: <RouteError />,
    children: [
      { index: true, element: <ReportsPage />, errorElement: <RouteError /> },
      {
        path: "upload",
        element: (
          <RequireRole min="analyst">
            <UploadPage />
          </RequireRole>
        ),
        errorElement: <RouteError />,
      },
      { path: "reports/:id", element: <ReportDetailPage />, errorElement: <RouteError /> },
      { path: "themes", element: <ThemesPage />, errorElement: <RouteError /> },
      { path: "themes/:id", element: <ThemeDetailPage />, errorElement: <RouteError /> },
      { path: "syntheses/:id", element: <SynthesisPage />, errorElement: <RouteError /> },
      {
        path: "targets",
        element: (
          <RequireRole min="analyst">
            <TargetQueuePage />
          </RequireRole>
        ),
        errorElement: <RouteError />,
      },
      {
        path: "subscriptions",
        element: (
          <RequireRole min="analyst">
            <SubscriptionsPage />
          </RequireRole>
        ),
        errorElement: <RouteError />,
      },
      {
        path: "connector-log",
        element: (
          <RequireRole min="admin">
            <ConnectorLogPage />
          </RequireRole>
        ),
        errorElement: <RouteError />,
      },
      {
        path: "invitations",
        element: (
          <RequireRole min="admin">
            <InvitationsPage />
          </RequireRole>
        ),
        errorElement: <RouteError />,
      },
      { path: "*", element: <NotFoundPage />, errorElement: <RouteError /> },
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
