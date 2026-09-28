import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { ROLE_LABEL, ROLE_RANK, type Role } from "../api";
import { useAuth } from "../auth";

function roleAtLeast(role: Role, min: Role): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[min];
}

/** 应用外壳：顶栏导航（入口按角色显隐）+ 内容区（Outlet）。 */
export function AppLayout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = async () => {
    await logout();
    navigate("/login", { replace: true });
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-inner">
          <nav className="topbar-nav">
            <span className="brand">MineReport</span>
            <NavLink to="/" end>
              研报库
            </NavLink>
            {user && roleAtLeast(user.role, "analyst") && <NavLink to="/upload">上传</NavLink>}
            {user && roleAtLeast(user.role, "analyst") && <NavLink to="/targets">标的队列</NavLink>}
            {user && user.role === "admin" && <NavLink to="/invitations">邀请管理</NavLink>}
          </nav>
          <div className="topbar-user">
            {user && (
              <>
                <span className="user-chip" title={user.email}>
                  {user.display_name} · {ROLE_LABEL[user.role]}
                </span>
                <button type="button" className="btn btn-ghost" onClick={handleLogout}>
                  登出
                </button>
              </>
            )}
          </div>
        </div>
      </header>
      <main className="page">
        <Outlet />
      </main>
    </div>
  );
}
