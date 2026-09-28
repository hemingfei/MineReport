import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, humanizeError } from "../api";
import { useAuth } from "../auth";

export function RegisterPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { login } = useAuth();

  const [token, setToken] = useState(searchParams.get("token") ?? "");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await api.register({
        token,
        email,
        password,
        display_name: displayName || undefined,
      });
      // 注册成功即建立会话，直接登录进入
      await login(email, password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(humanizeError(err, "注册失败，请重试"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="auth-screen">
      <form className="auth-card card" onSubmit={onSubmit}>
        <h1 className="auth-title">MineReport</h1>
        <p className="auth-subtitle">凭邀请码注册</p>
        <label className="field">
          <span>邀请码</span>
          <input
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="管理员发给你的邀请码"
            required
            autoFocus
          />
        </label>
        <label className="field">
          <span>邮箱</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label className="field">
          <span>密码（至少 8 位）</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            minLength={8}
            required
          />
        </label>
        <label className="field">
          <span>显示名（可选）</span>
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} maxLength={64} />
        </label>
        {error && <p className="form-error">{error}</p>}
        <button type="submit" className="btn btn-primary btn-block" disabled={submitting}>
          {submitting ? "注册中…" : "注册并登录"}
        </button>
        <p className="auth-alt">
          已有账号？<Link to="/login">直接登录</Link>
        </p>
      </form>
    </div>
  );
}
