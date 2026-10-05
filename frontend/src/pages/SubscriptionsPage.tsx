import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  REF_STATUS_LABEL,
  api,
  humanizeError,
  refStatusChipClass,
  type ConnectorQuota,
  type RefItem,
  type Subscription,
  type ThemeSummary,
} from "../api";
import { useAuth } from "../auth";
import { formatDateTime } from "../format";
import { ListSkeleton } from "../components/ListSkeleton";

function SubscriptionRow({
  sub,
  isAdmin,
  onPatch,
  onDelete,
  busy,
}: {
  sub: Subscription;
  isAdmin: boolean;
  onPatch: (id: number, body: Record<string, unknown>) => void;
  onDelete: (id: number) => void;
  busy: boolean;
}) {
  const health =
    sub.consecutive_failures >= 5
      ? "连续死信告警"
      : sub.consecutive_failures > 0
        ? `连续 ${sub.consecutive_failures} 轮死信`
        : sub.attempt > 0
          ? `退避重试中（第 ${sub.attempt} 次）`
          : "正常";
  return (
    <tr className={sub.enabled ? undefined : "row-muted"}>
      <td data-label="题材">
        <Link to={`/themes/${sub.theme_id}`}>{sub.theme_name}</Link>
      </td>
      <td className="mono" data-label="连接器">
        {sub.connector_id}
      </td>
      <td data-label="间隔">每 {sub.interval_hours}h</td>
      <td data-label="自动下载">
        <span className={sub.auto_download ? "chip chip-ok" : "chip chip-muted"}>
          {sub.auto_download ? "自动下载" : "仅元数据"}
        </span>
      </td>
      <td className="cell-nowrap" data-label="下轮">
        {sub.next_run_at ? formatDateTime(sub.next_run_at) : "—"}
      </td>
      <td className="cell-nowrap" data-label="上次成功">
        {sub.last_success_at ? formatDateTime(sub.last_success_at) : "—"}
      </td>
      <td data-label="健康">
        <span
          className={
            sub.consecutive_failures > 0 || sub.attempt > 0 ? "chip chip-warn" : "chip chip-ok"
          }
        >
          {health}
        </span>
      </td>
      <td className="cell-nowrap" data-label="操作">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          disabled={busy}
          onClick={() => onPatch(sub.id, { enabled: !sub.enabled })}
        >
          {sub.enabled ? "停用" : "启用"}
        </button>{" "}
        {isAdmin && (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={() => onPatch(sub.id, { auto_download: !sub.auto_download })}
          >
            {sub.auto_download ? "关自动下载" : "开自动下载"}
          </button>
        )}{" "}
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          disabled={busy}
          onClick={() => onDelete(sub.id)}
        >
          删除
        </button>
      </td>
    </tr>
  );
}

function RefRow({
  ref: refItem,
  quota,
  onDownload,
  busy,
}: {
  ref: RefItem;
  quota: ConnectorQuota | null;
  onDownload: (ref: RefItem) => void;
  busy: boolean;
}) {
  const quotaHint = quota?.hints[refItem.connector_id] ?? null;
  const downloadable = refItem.status === "seen" || refItem.status === "fetch_failed";
  return (
    <tr>
      <td data-label="标题">
        {refItem.report_url ? (
          <a href={refItem.report_url} target="_blank" rel="noreferrer">
            {refItem.title}
          </a>
        ) : (
          refItem.title
        )}
        {refItem.last_error && <div className="hint text-danger">{refItem.last_error}</div>}
      </td>
      <td data-label="券商">{refItem.broker ?? "—"}</td>
      <td className="cell-nowrap" data-label="发布日期">
        {refItem.publish_date}
      </td>
      <td className="mono" data-label="来源">
        {refItem.connector_id}
      </td>
      <td data-label="状态">
        <span className={refStatusChipClass(refItem.status)}>
          {REF_STATUS_LABEL[refItem.status]}
        </span>
      </td>
      <td className="cell-nowrap" data-label="操作">
        {refItem.report_id ? (
          <Link to={`/reports/${refItem.report_id}`}>查看研报</Link>
        ) : downloadable ? (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={busy}
            title={quotaHint ?? undefined}
            onClick={() => onDownload(refItem)}
          >
            下载入库
          </button>
        ) : (
          "—"
        )}
      </td>
    </tr>
  );
}

/** 订阅管理（analyst+；admin 全量并可控自动下载）：建订阅、启停/立即运行、发现记录手动下载。 */
export function SubscriptionsPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  const [subs, setSubs] = useState<Subscription[]>([]);
  const [refs, setRefs] = useState<RefItem[]>([]);
  const [quota, setQuota] = useState<ConnectorQuota | null>(null);
  const [themes, setThemes] = useState<ThemeSummary[]>([]);
  const [refFilter, setRefFilter] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [themeId, setThemeId] = useState<number | null>(null);
  const [intervalHours, setIntervalHours] = useState(6);
  const [keywords, setKeywords] = useState("");

  const reload = useCallback(async () => {
    try {
      const [subList, refList, quotaOut, themeList] = await Promise.all([
        api.listSubscriptions(),
        api.listRefs(refFilter ? { status_filter: refFilter } : {}),
        api.getConnectorQuota(),
        api.listThemes({ status: "active", limit: 100 }),
      ]);
      setSubs(subList.items);
      setRefs(refList.items);
      setQuota(quotaOut);
      setThemes(themeList.items);
    } catch (e) {
      setError(humanizeError(e, "加载订阅数据失败"));
    } finally {
      setLoading(false);
    }
  }, [refFilter]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const onCreate = async (e: FormEvent) => {
    e.preventDefault();
    setActionError(null);
    if (themeId == null) {
      setActionError("请选择题材");
      return;
    }
    setBusy(true);
    try {
      await api.createSubscription({
        theme_id: themeId,
        interval_hours: intervalHours,
        keywords: keywords.trim() ? keywords.split(/[,，\s]+/).filter(Boolean) : undefined,
      });
      setKeywords("");
      await reload();
    } catch (err) {
      setActionError(humanizeError(err, "创建订阅失败"));
    } finally {
      setBusy(false);
    }
  };

  const onPatch = async (id: number, body: Record<string, unknown>) => {
    setActionError(null);
    setBusy(true);
    try {
      await api.patchSubscription(id, body);
      await reload();
    } catch (err) {
      setActionError(humanizeError(err, "更新订阅失败"));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (id: number) => {
    setActionError(null);
    setBusy(true);
    try {
      await api.deleteSubscription(id);
      await reload();
    } catch (err) {
      setActionError(humanizeError(err, "删除订阅失败"));
    } finally {
      setBusy(false);
    }
  };

  const onDownload = async (ref: RefItem) => {
    const hint = quota?.hints[ref.connector_id];
    // spec 用户故事 25：手动下载前额度提示（扣 VIP 权益额度）
    if (hint && !window.confirm(hint)) return;
    setActionError(null);
    setBusy(true);
    try {
      await api.downloadRef(ref.id);
      await reload();
    } catch (err) {
      setActionError(humanizeError(err, "下载失败"));
      await reload();
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <div className="list-head">
        <h1 className="page-title">订阅管理</h1>
        {quota && (
          <span className="hint">
            下载额度：今日 {quota.downloads_today} 次 / 累计 {quota.downloads_total} 次
          </span>
        )}
      </div>

      <form className="card form-row" onSubmit={onCreate}>
        <label className="field">
          <span>题材（名 + 同义词自动作查询词）</span>
          <select
            value={themeId ?? ""}
            onChange={(e) => setThemeId(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">选择在册题材…</option>
            {themes.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>间隔（小时）</span>
          <input
            type="number"
            min={1}
            max={720}
            value={intervalHours}
            onChange={(e) => setIntervalHours(Number(e.target.value) || 6)}
          />
        </label>
        <label className="field field-grow">
          <span>额外查询词（可选，逗号/空格分隔）</span>
          <input
            type="text"
            value={keywords}
            onChange={(e) => setKeywords(e.target.value)}
            placeholder="如：AIDC、液冷"
          />
        </label>
        <div className="field field-btn">
          <button type="submit" className="btn btn-primary" disabled={busy || loading}>
            创建订阅
          </button>
        </div>
      </form>
      {actionError && <p className="form-error">{actionError}</p>}

      {loading ? (
        <ListSkeleton rows={4} />
      ) : error ? (
        <p className="form-error">{error}</p>
      ) : (
        <>
          <div className="card table-card">
            <table className="data-table">
              <thead>
                <tr>
                  <th>题材</th>
                  <th>连接器</th>
                  <th>间隔</th>
                  <th>自动下载</th>
                  <th>下轮</th>
                  <th>上次成功</th>
                  <th>健康</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {subs.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="empty-cell">
                      还没有订阅
                    </td>
                  </tr>
                ) : (
                  subs.map((s) => (
                    <SubscriptionRow
                      key={s.id}
                      sub={s}
                      isAdmin={!!isAdmin}
                      onPatch={onPatch}
                      onDelete={onDelete}
                      busy={busy}
                    />
                  ))
                )}
              </tbody>
            </table>
          </div>

          <h2 className="section-title">发现记录（手动下载工作池）</h2>
          <div className="form-row">
            <label className="field">
              <span>状态过滤</span>
              <select value={refFilter} onChange={(e) => setRefFilter(e.target.value)}>
                <option value="">全部</option>
                <option value="seen">待下载</option>
                <option value="ingested">已入库</option>
                <option value="duplicate">库内已有</option>
                <option value="fetch_failed">下载失败</option>
              </select>
            </label>
          </div>
          <div className="card table-card">
            <table className="data-table">
              <thead>
                <tr>
                  <th>标题</th>
                  <th>券商</th>
                  <th>发布日期</th>
                  <th>来源</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {refs.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="empty-cell">
                      暂无发现记录
                    </td>
                  </tr>
                ) : (
                  refs.map((r) => (
                    <RefRow key={r.id} ref={r} quota={quota} onDownload={onDownload} busy={busy} />
                  ))
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
