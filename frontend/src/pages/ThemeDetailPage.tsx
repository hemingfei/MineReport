import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api,
  humanizeError,
  ROLE_RANK,
  THEME_SOURCE_LABEL,
  THEME_STATUS_LABEL,
  themeStatusChipClass,
  type ThemeMemberList,
  type ThemeReportList,
  type ThemeSummary,
} from "../api";
import { useAuth } from "../auth";
import { formatDate, formatDateTime } from "../format";
import { taskStatusLabel, useTaskPolling, useTaskTerminal } from "../task";

const REPORT_PAGE_SIZE = 20;

/** 题材浏览页：题材全貌 = 题材下研报列表 + 标的池（spec 用户故事 14）+ 综合分析入口（#20）。 */
export function ThemeDetailPage() {
  const { id } = useParams();
  const themeId = Number(id);
  const navigate = useNavigate();
  const { user } = useAuth();

  const [theme, setTheme] = useState<ThemeSummary | null>(null);
  const [reports, setReports] = useState<ThemeReportList | null>(null);
  const [members, setMembers] = useState<ThemeMemberList | null>(null);
  const [activeOnly, setActiveOnly] = useState(true);
  const [reportPage, setReportPage] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [genError, setGenError] = useState<string | null>(null);
  const [genTaskId, setGenTaskId] = useState<number | null>(null);
  const { task: genTask } = useTaskPolling(genTaskId);

  useEffect(() => {
    if (!Number.isFinite(themeId)) return;
    let cancelled = false;
    Promise.all([
      api.getTheme(themeId),
      api.listThemeReports(themeId, REPORT_PAGE_SIZE, reportPage * REPORT_PAGE_SIZE),
      api.listThemeMembers(themeId, activeOnly),
    ])
      .then(([t, r, m]) => {
        if (cancelled) return;
        setTheme(t);
        setReports(r);
        setMembers(m);
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError(humanizeError(e, "加载题材失败"));
      });
    return () => {
      cancelled = true;
    };
  }, [themeId, reportPage, activeOnly]);

  // 生成任务终态：成功跳结果页；失败亮错误（缓存命中在点击时直接跳转）
  useTaskTerminal(genTask, "生成失败，请重试", {
    onDone: (sid) => {
      setGenTaskId(null);
      if (sid > 0) navigate(`/syntheses/${sid}`);
    },
    onFailed: (text) => {
      setGenTaskId(null);
      setGenError(text);
    },
  });

  if (!Number.isFinite(themeId)) {
    return <p className="form-error">无效的题材 id</p>;
  }
  if (error) return <p className="form-error">{error}</p>;
  if (!theme || !reports || !members) return <div className="page-loading">加载中…</div>;

  const canGenerate = user != null && ROLE_RANK[user.role] >= ROLE_RANK.analyst;

  const onGenerate = () => {
    setGenError(null);
    api
      .createSynthesis(themeId)
      .then((out) => {
        if (out.cached && out.synthesis_id != null) navigate(`/syntheses/${out.synthesis_id}`);
        else if (out.task_id != null) setGenTaskId(out.task_id);
      })
      .catch((e) => setGenError(humanizeError(e, "生成综合分析失败")));
  };

  const totalReportPages = Math.max(1, Math.ceil(reports.total / REPORT_PAGE_SIZE));

  return (
    <section>
      <div className="list-head">
        <h1 className="page-title">{theme.name}</h1>
        <Link className="btn btn-ghost" to="/themes">
          返回题材列表
        </Link>
      </div>

      <div className="card">
        <p>
          <span className={themeStatusChipClass(theme.status)}>{THEME_STATUS_LABEL[theme.status]}</span>{" "}
          <span className="chip chip-muted">{THEME_SOURCE_LABEL[theme.source] ?? theme.source}</span>{" "}
          <span className="hint">
            {theme.report_count} 篇研报 · {theme.member_count} 只在池标的
          </span>
        </p>
        {theme.definition && <p className="hint">{theme.definition}</p>}
        {theme.synonyms.length > 0 && <p className="hint">同义词：{theme.synonyms.join("、")}</p>}
        {theme.merged_into_id != null && (
          <p className="hint">
            已合并入{" "}
            <Link to={`/themes/${theme.merged_into_id}`}>#{theme.merged_into_id}</Link>
          </p>
        )}
        <p className="hint">
          {theme.latest_synthesis != null ? (
            <>
              最新综合分析{" "}
              <Link to={`/syntheses/${theme.latest_synthesis.id}`}>
                v{theme.latest_synthesis.version}
              </Link>
              （{formatDateTime(theme.latest_synthesis.created_at)}）
            </>
          ) : (
            "尚无综合分析"
          )}
          {canGenerate && (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={onGenerate}
              disabled={genTaskId != null}
              style={{ marginLeft: 12 }}
              title="输入未变时命中缓存直接查看；重算请到结果页手动刷新"
            >
              {genTaskId != null
                ? `生成中（${taskStatusLabel(genTask)}）`
                : theme.latest_synthesis != null
                  ? "查看/重生成综合分析"
                  : "生成综合分析"}
            </button>
          )}
        </p>
        {genError && <p className="form-error">{genError}</p>}
      </div>

      <h2 className="section-title">题材下研报（{reports.total}）</h2>
      <div className="card table-card">
        <table className="data-table">
          <thead>
            <tr>
              <th>发布日期</th>
              <th>标题</th>
              <th>券商</th>
            </tr>
          </thead>
          <tbody>
            {reports.items.length === 0 ? (
              <tr>
                <td colSpan={3} className="empty-cell">
                  暂无研报（当前分析版本未关联本题材）
                </td>
              </tr>
            ) : (
              reports.items.map((r) => (
                <tr
                  key={r.id}
                  className="row-link"
                  onClick={() => navigate(`/reports/${r.id}`)}
                  title="点击查看详情"
                >
                  <td className="cell-nowrap">{formatDate(r.publish_date)}</td>
                  <td>{r.title}</td>
                  <td className="cell-nowrap">{r.broker}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      {reports.total > REPORT_PAGE_SIZE && (
        <div className="pager">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={reportPage === 0}
            onClick={() => setReportPage((p) => p - 1)}
          >
            上一页
          </button>
          <span>
            第 {reportPage + 1} / {totalReportPages} 页
          </span>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={reportPage + 1 >= totalReportPages}
            onClick={() => setReportPage((p) => p + 1)}
          >
            下一页
          </button>
        </div>
      )}

      <h2 className="section-title">
        标的池（{members.items.length}
        {activeOnly ? " 活跃" : ""}）
      </h2>
      <p className="hint">
        <label className="field">
          <input
            type="checkbox"
            checked={!activeOnly}
            onChange={(e) => setActiveOnly(!e.target.checked)}
          />
          <span>显示已退池成员</span>
        </label>
      </p>
      <div className="card table-card">
        <table className="data-table">
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>交易所</th>
              <th>申万一级行业</th>
              <th>来源</th>
              <th>加入日期</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {members.items.length === 0 ? (
              <tr>
                <td colSpan={7} className="empty-cell">
                  标的池为空（导入种子或等待分析回填）
                </td>
              </tr>
            ) : (
              members.items.map((m) => (
                <tr key={m.code}>
                  <td className="mono cell-nowrap">{m.code}</td>
                  <td>{m.name}</td>
                  <td className="cell-nowrap">{m.exchange}</td>
                  <td className="cell-nowrap">{m.sw_l1_name ?? "—"}</td>
                  <td className="cell-nowrap">
                    {m.source === "seed" ? "种子" : m.source === "analysis" ? "分析" : "人工"}
                  </td>
                  <td className="cell-nowrap">{formatDate(m.joined_at)}</td>
                  <td className="cell-nowrap">
                    {m.is_active ? (
                      <span className="chip chip-ok">在池</span>
                    ) : (
                      <span className="chip chip-muted">已退池</span>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
