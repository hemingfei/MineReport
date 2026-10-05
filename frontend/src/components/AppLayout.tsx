import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Crosshair,
  EnvelopeSimple,
  FileText,
  List as ListIcon,
  Monitor,
  Moon,
  Rss,
  Sun,
  Tag,
  TerminalWindow,
  UploadSimple,
  X,
} from "@phosphor-icons/react";
import { ROLE_LABEL, ROLE_RANK, type Role } from "../api";
import { useAuth } from "../auth";
import { useTheme, type ThemeChoice } from "../theme";

function roleAtLeast(role: Role, min: Role): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[min];
}

const THEME_OPTIONS: { key: ThemeChoice; label: string; icon: ReactNode }[] = [
  { key: "light", label: "浅色", icon: <Sun weight="bold" /> },
  { key: "dark", label: "深色", icon: <Moon weight="bold" /> },
  { key: "system", label: "随系统", icon: <Monitor weight="bold" /> },
];

function ThemeToggle() {
  const [theme, setTheme] = useTheme();
  return (
    <div className="theme-toggle" role="radiogroup" aria-label="主题">
      {THEME_OPTIONS.map((o) => (
        <button
          key={o.key}
          type="button"
          className={`theme-opt ${theme === o.key ? "active" : ""}`}
          aria-checked={theme === o.key}
          role="radio"
          title={o.label}
          aria-label={o.label}
          onClick={() => setTheme(o.key)}
        >
          {o.icon}
        </button>
      ))}
    </div>
  );
}

/** 应用外壳：顶栏导航（入口按角色显隐）+ 移动端抽屉 + 内容区（Outlet）。 */
export function AppLayout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [drawerOpen, setDrawerOpen] = useState(false);

  const isAnalyst = !!user && roleAtLeast(user.role, "analyst");
  const isAdmin = !!user && user.role === "admin";

  const navItems = [
    { to: "/", label: "研报库", end: true, icon: <FileText />, show: true },
    { to: "/themes", label: "题材", end: false, icon: <Tag />, show: true },
    { to: "/upload", label: "上传", end: false, icon: <UploadSimple />, show: isAnalyst },
    { to: "/targets", label: "标的队列", end: false, icon: <Crosshair />, show: isAnalyst },
    { to: "/subscriptions", label: "订阅", end: false, icon: <Rss />, show: isAnalyst },
    { to: "/connector-log", label: "连接器日志", end: false, icon: <TerminalWindow />, show: isAdmin },
    { to: "/invitations", label: "邀请管理", end: false, icon: <EnvelopeSimple />, show: isAdmin },
  ].filter((item) => item.show);

  // 路由变化收起抽屉；打开时锁 body 滚动、Esc 可关
  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    document.body.classList.toggle("nav-open", drawerOpen);
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.classList.remove("nav-open");
      window.removeEventListener("keydown", onKey);
    };
  }, [drawerOpen]);

  const handleLogout = async () => {
    await logout();
    navigate("/login", { replace: true });
  };

  const initial = user?.display_name?.trim()?.charAt(0) ?? "?";

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        跳到主要内容
      </a>
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" className="brand">
            <span className="brand-mark" aria-hidden>
              研
            </span>
            <span>MineReport</span>
          </NavLink>
          <nav className="topbar-nav" aria-label="主导航">
            {navItems.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.end}>
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="topbar-actions">
            <ThemeToggle />
            {user && (
              <div className="topbar-user">
                <span className="avatar" aria-hidden>
                  {initial}
                </span>
                <span className="user-chip" title={user.email}>
                  {user.display_name} <span className="role">· {ROLE_LABEL[user.role]}</span>
                </span>
              </div>
            )}
            {user && (
              <button type="button" className="btn btn-ghost" onClick={handleLogout}>
                登出
              </button>
            )}
            <button
              type="button"
              className="nav-toggle"
              aria-label="打开导航菜单"
              aria-expanded={drawerOpen}
              onClick={() => setDrawerOpen(true)}
            >
              <ListIcon weight="bold" />
            </button>
          </div>
        </div>
      </header>

      <button
        type="button"
        className={`drawer-overlay ${drawerOpen ? "open" : ""}`}
        aria-label="关闭导航菜单"
        onClick={() => setDrawerOpen(false)}
      />
      <aside className={`drawer ${drawerOpen ? "open" : ""}`} aria-label="导航菜单" aria-hidden={!drawerOpen}>
        <div className="drawer-head">
          <span className="brand">
            <span className="brand-mark" aria-hidden>
              研
            </span>
            <span>MineReport</span>
          </span>
          <button
            type="button"
            className="nav-toggle"
            style={{ display: "grid" }}
            aria-label="关闭导航菜单"
            onClick={() => setDrawerOpen(false)}
          >
            <X weight="bold" />
          </button>
        </div>
        <nav className="drawer-nav" aria-label="主导航">
          {navItems.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} tabIndex={drawerOpen ? 0 : -1}>
              {item.icon}
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="drawer-divider" />
        <div className="drawer-theme">
          <span>外观</span>
          <ThemeToggle />
        </div>
        {user && (
          <div className="drawer-user">
            <div className="who">
              <span className="avatar" aria-hidden>
                {initial}
              </span>
              <div style={{ minWidth: 0 }}>
                <div className="who-name">{user.display_name}</div>
                <div className="who-role">{user.email}</div>
              </div>
            </div>
            <button type="button" className="btn btn-ghost btn-block" style={{ margin: 0 }} onClick={handleLogout}>
              登出
            </button>
          </div>
        )}
      </aside>

      <main className="page" id="main">
        <Outlet />
      </main>
    </div>
  );
}
