import { useEffect, useState } from "react";
import {
  RUN_EVENT_LABEL,
  api,
  humanizeError,
  runEventChipClass,
  type ConnectorQuota,
  type ConnectorRunItem,
  type ConnectorRunEvent,
} from "../api";
import { formatDateTime } from "../format";
import { ListSkeleton } from "../components/ListSkeleton";

function RunRow({ run }: { run: ConnectorRunItem }) {
  const stats = run.stats ?? {};
  const quota = typeof stats.downloaded === "number" ? `，下载 ${stats.downloaded} 次` : "";
  const found = typeof stats.found === "number" ? `，命中 ${stats.found}` : "";
  return (
    <tr>
      <td className="cell-nowrap" data-label="时间">
        {formatDateTime(run.created_at)}
      </td>
      <td className="mono" data-label="连接器">
        {run.connector_id}
      </td>
      <td data-label="事件">
        <span className={runEventChipClass(run.event)}>
          {RUN_EVENT_LABEL[run.event as ConnectorRunEvent] ?? run.event}
        </span>
      </td>
      <td data-label="订阅">{run.subscription_id != null ? `#${run.subscription_id}` : "—"}</td>
      <td data-label="成败">{run.ok ? "✓" : "✗"}</td>
      <td data-label="详情">
        {run.message}
        {found}
        {quota}
      </td>
    </tr>
  );
}

/** 连接器运行日志（admin，spec 用户故事 24）：成功/失败/死信/告警/额度。 */
export function ConnectorLogPage() {
  const [runs, setRuns] = useState<ConnectorRunItem[]>([]);
  const [total, setTotal] = useState(0);
  const [quota, setQuota] = useState<ConnectorQuota | null>(null);
  const [eventFilter, setEventFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([
      api.listConnectorRuns(eventFilter ? { event: eventFilter, limit: 100 } : { limit: 100 }),
      api.getConnectorQuota(),
    ])
      .then(([runList, quotaOut]) => {
        if (cancelled) return;
        setRuns(runList.items);
        setTotal(runList.total);
        setQuota(quotaOut);
      })
      .catch((e) => {
        if (!cancelled) setError(humanizeError(e, "加载连接器日志失败"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [eventFilter]);

  return (
    <section>
      <h1 className="page-title">连接器日志</h1>

      {quota && (
        <div className="card">
          <p>
            下载额度消耗：今日 <strong>{quota.downloads_today}</strong> 次 / 累计{" "}
            <strong>{quota.downloads_total}</strong> 次（自动 + 手动）
          </p>
        </div>
      )}

      <div className="form-row">
        <label className="field">
          <span>事件过滤</span>
          <select value={eventFilter} onChange={(e) => setEventFilter(e.target.value)}>
            <option value="">全部</option>
            <option value="run">执行</option>
            <option value="retry">退避重试</option>
            <option value="dead_letter">死信</option>
            <option value="alert">告警</option>
            <option value="manual_download">手动下载</option>
          </select>
        </label>
      </div>

      {loading ? (
        <ListSkeleton rows={5} />
      ) : error ? (
        <p className="form-error">{error}</p>
      ) : (
        <div className="card table-card">
          <table className="data-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>连接器</th>
                <th>事件</th>
                <th>订阅</th>
                <th>成败</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {runs.length === 0 ? (
                <tr>
                  <td colSpan={6} className="empty-cell">
                    暂无日志（共 {total} 条匹配）
                  </td>
                </tr>
              ) : (
                runs.map((r) => <RunRow key={r.id} run={r} />)
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
