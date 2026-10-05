import { useEffect, useMemo, useState, type FormEvent } from "react";
import { ROLE_LABEL, api, type Invitation, type Role } from "../api";
import { formatDateTime } from "../format";
import { humanizeError } from "../api";
import { ListSkeleton } from "../components/ListSkeleton";

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // 剪贴板不可用（如非安全上下文）时退化：直接展示文本供手动复制
      setCopied(false);
    }
  };
  return (
    <button type="button" className="btn btn-ghost btn-sm" onClick={onCopy}>
      {copied ? "已复制" : "复制"}
    </button>
  );
}

function InvitationRow({ invitation }: { invitation: Invitation }) {
  const expired = new Date(invitation.expires_at).getTime() < Date.now();
  const status = invitation.used_at
    ? `已使用（${formatDateTime(invitation.used_at)}）`
    : expired
      ? "已过期"
      : "可用";
  return (
    <tr className={invitation.used_at ? "row-muted" : undefined}>
      <td className="mono cell-nowrap" data-label="邀请码">
        {invitation.token}
      </td>
      <td data-label="角色">{ROLE_LABEL[invitation.role]}</td>
      <td data-label="绑定邮箱">{invitation.email ?? "任意邮箱"}</td>
      <td className="cell-nowrap" data-label="有效期至">
        {formatDateTime(invitation.expires_at)}
      </td>
      <td data-label="状态">{status}</td>
      <td className="cell-nowrap" data-label="创建时间">
        {formatDateTime(invitation.created_at)}
      </td>
    </tr>
  );
}

export function InvitationsPage() {
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [role, setRole] = useState<Role>("analyst");
  const [email, setEmail] = useState("");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<Invitation | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .listInvitations()
      .then((items) => {
        if (!cancelled) setInvitations(items);
      })
      .catch((e) => {
        if (!cancelled) setError(humanizeError(e, "加载邀请列表失败"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const onCreate = async (e: FormEvent) => {
    e.preventDefault();
    setCreateError(null);
    setCreating(true);
    try {
      const inv = await api.createInvitation(role, email || undefined);
      setCreated(inv);
      setInvitations((prev) => [inv, ...prev]);
      setEmail("");
    } catch (err) {
      setCreateError(humanizeError(err, "创建邀请失败"));
    } finally {
      setCreating(false);
    }
  };

  const registerLink = useMemo(
    () => (created ? `${window.location.origin}/register?token=${created.token}` : ""),
    [created],
  );

  return (
    <section>
      <h1 className="page-title">邀请管理</h1>

      <form className="card form-row" onSubmit={onCreate}>
        <label className="field">
          <span>注册后角色</span>
          <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            <option value="analyst">分析师</option>
            <option value="reader">读者</option>
            <option value="admin">管理员</option>
          </select>
        </label>
        <label className="field field-grow">
          <span>绑定邮箱（可选，留空则任意邮箱可用）</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="example@corp.com"
          />
        </label>
        <div className="field field-btn">
          <button type="submit" className="btn btn-primary" disabled={creating}>
            {creating ? "创建中…" : "创建邀请"}
          </button>
        </div>
      </form>
      {createError && <p className="form-error">{createError}</p>}

      {created && (
        <div className="card created-invitation">
          <p>
            邀请已创建（{ROLE_LABEL[created.role]}
            {created.email ? ` · ${created.email}` : ""}，有效期至{" "}
            {formatDateTime(created.expires_at)}）。把注册链接发给对方：
          </p>
          <div className="copy-line">
            <code className="mono copy-target">{registerLink}</code>
            <CopyButton text={registerLink} />
          </div>
        </div>
      )}

      {loading ? (
        <ListSkeleton rows={4} />
      ) : error ? (
        <p className="form-error">{error}</p>
      ) : (
        <div className="card table-card">
          <table className="data-table">
            <thead>
              <tr>
                <th>邀请码</th>
                <th>角色</th>
                <th>绑定邮箱</th>
                <th>有效期至</th>
                <th>状态</th>
                <th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {invitations.length === 0 ? (
                <tr>
                  <td colSpan={6} className="empty-cell">
                    还没有邀请
                  </td>
                </tr>
              ) : (
                invitations.map((inv) => <InvitationRow key={inv.id} invitation={inv} />)
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
