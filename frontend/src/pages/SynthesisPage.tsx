import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api,
  humanizeError,
  ROLE_RANK,
  type SynthesisConclusion,
  type SynthesisDetail,
  type SynthesisReportRef,
} from "../api";
import { useAuth } from "../auth";
import { formatDate, formatDateTime } from "../format";
import { TASK_STATUS_LABEL, useTaskPolling } from "../task";

/** 综合分析结果页（spec 用户故事 17~20）：共性结论/共识标的/分歧点，
 * 结论带原文引用回链（R 序位 → 研报详情）；版本切换 + 手动刷新。 */
export function SynthesisPage() {
  const { id } = useParams();
  const synthesisId = Number(id);
  const navigate = useNavigate();
  const { user } = useAuth();

  const [data, setData] = useState<SynthesisDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [refreshTaskId, setRefreshTaskId] = useState<number | null>(null);
  const { task: refreshTask } = useTaskPolling(refreshTaskId);

  useEffect(() => {
    if (!Number.isFinite(synthesisId)) return;
    let cancelled = false;
    api
      .getSynthesis(synthesisId)
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(humanizeError(e, "加载综合分析失败"));
      });
    return () => {
      cancelled = true;
    };
  }, [synthesisId]);

  // 刷新任务终态：成功跳新版本，失败亮出错误（停留在当前版本）
  useEffect(() => {
    if (refreshTask == null) return;
    if (refreshTask.status === "done") {
      const newId = Number(refreshTask.result?.synthesis_id ?? 0);
      setRefreshTaskId(null);
      if (newId > 0) {
        navigate(`/syntheses/${newId}`, { replace: true });
      }
    } else if (refreshTask.status === "failed") {
      setRefreshTaskId(null);
      setRefreshError(
        `${refreshTask.result?.error_code ?? "failed"}：${refreshTask.result?.error ?? "刷新失败，请重试"}`,
      );
    }
  }, [refreshTask, navigate]);

  if (!Number.isFinite(synthesisId)) {
    return <p className="form-error">无效的综合分析 id</p>;
  }
  if (error) return <p className="form-error">{error}</p>;
  if (!data) return <div className="page-loading">加载中…</div>;

  const canRefresh = user != null && ROLE_RANK[user.role] >= ROLE_RANK.analyst;
  const r = data.result;

  const onRefresh = () => {
    setRefreshError(null);
    api
      .refreshSynthesis(data.id)
      .then((out) => setRefreshTaskId(out.task_id))
      .catch((e) => setRefreshError(humanizeError(e, "刷新失败")));
  };

  return (
    <section>
      <div className="list-head">
        <h1 className="page-title">
          综合分析 ·{" "}
          <Link to={`/themes/${data.theme_id}`}>{data.theme_name}</Link>
        </h1>
        <div className="field">
          <select
            value={data.id}
            onChange={(e) => navigate(`/syntheses/${e.target.value}`)}
            aria-label="版本切换"
          >
            {data.versions.map((v) => (
              <option key={v.id} value={v.id}>
                v{v.version}（{formatDateTime(v.created_at)}）
              </option>
            ))}
          </select>
          {canRefresh && (
            <button
              type="button"
              className="btn btn-primary"
              onClick={onRefresh}
              disabled={refreshTaskId != null}
            >
              {refreshTaskId != null
                ? `刷新中（${TASK_STATUS_LABEL[refreshTask?.status ?? "uploaded"]}）`
                : "手动刷新"}
            </button>
          )}
        </div>
      </div>

      {refreshError && <p className="form-error">{refreshError}</p>}

      <div className="card">
        <p className="hint">
          v{data.version} · {formatDateTime(data.created_at)} · 输入 {data.report_ids.length} 篇研报 ·
          模型 {data.model} · {data.prompt_tokens + data.completion_tokens} tokens ·{" "}
          {(data.duration_ms / 1000).toFixed(1)}s
          {r.truncated &&
            ` · 输入已截断（题材下共 ${r.input_total} 篇，按发布日期取最近 ${data.report_ids.length} 篇）`}
        </p>
        <p className="hint">
          缓存口径：输入研报集合不变则命中缓存不重算；词表/研报更新后用「手动刷新」纳入最新集合。
        </p>
      </div>

      <h2 className="section-title">共性结论（{r.common_conclusions.length}）</h2>
      <ConclusionList items={r.common_conclusions} reports={data.reports} />

      <h2 className="section-title">共识标的（{r.consensus_targets.length}）</h2>
      <div className="card table-card">
        <table className="data-table">
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>共识观点</th>
              <th>引用</th>
            </tr>
          </thead>
          <tbody>
            {r.consensus_targets.length === 0 ? (
              <tr>
                <td colSpan={4} className="empty-cell">
                  暂无共识标的
                </td>
              </tr>
            ) : (
              r.consensus_targets.map((t, i) => (
                <tr key={`${t.name}-${i}`}>
                  <td className="mono cell-nowrap">{t.code ?? "—"}</td>
                  <td className="cell-nowrap">{t.name}</td>
                  <td>{t.view || "—"}</td>
                  <td>
                    <RefChips refs={t.report_refs} reports={data.reports} />
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <h2 className="section-title">分歧点（{r.divergences.length}）</h2>
      <ConclusionList items={r.divergences} reports={data.reports} />

      <h2 className="section-title">证据边界（{data.reports.length} 篇）</h2>
      <div className="card table-card">
        <table className="data-table">
          <thead>
            <tr>
              <th>编号</th>
              <th>标题</th>
              <th>券商</th>
              <th>发布日期</th>
            </tr>
          </thead>
          <tbody>
            {data.reports.map((rep, i) => (
              <tr key={rep.id} className="row-link">
                <td className="mono cell-nowrap">R{i + 1}</td>
                <td>
                  <Link to={`/reports/${rep.id}`}>{rep.title}</Link>
                </td>
                <td className="cell-nowrap">{rep.broker || "—"}</td>
                <td className="cell-nowrap">{formatDate(rep.publish_date)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** 结论/分歧点卡片列表：每条带引用回链（悬停见券商与日期，点击进研报详情读正文）。 */
function ConclusionList({
  items,
  reports,
}: {
  items: SynthesisConclusion[];
  reports: SynthesisReportRef[];
}) {
  if (items.length === 0) {
    return (
      <div className="card">
        <p className="empty-state">暂无内容</p>
      </div>
    );
  }
  return (
    <>
      {items.map((c, i) => (
        <div key={i} className="card synthesis-item">
          <p>{c.text}</p>
          <p className="hint">
            <RefChips refs={c.report_refs} reports={reports} />
          </p>
        </div>
      ))}
    </>
  );
}

function RefChips({ refs, reports }: { refs: number[]; reports: SynthesisReportRef[] }) {
  return (
    <>
      {refs.map((n) => {
        const rep = reports[n - 1];
        if (rep == null) return null;
        return (
          <Link
            key={n}
            className="chip chip-muted"
            to={`/reports/${rep.id}`}
            title={`${rep.broker} · ${formatDate(rep.publish_date)} · ${rep.title}`}
          >
            R{n}
          </Link>
        );
      })}
    </>
  );
}
