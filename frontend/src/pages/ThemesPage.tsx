import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Tag } from "@phosphor-icons/react";
import {
  api,
  humanizeError,
  ROLE_RANK,
  THEME_SOURCE_LABEL,
  THEME_STATUS_LABEL,
  themeStatusChipClass,
  type AuthorCoverage,
  type Role,
  type ThemeList,
  type ThemeStatus,
  type ThemeSummary,
} from "../api";
import { useAuth } from "../auth";
import { isTaskSettled, taskStatusLabel, useTaskPolling } from "../task";
import { EmptyState } from "../components/EmptyState";
import { ListSkeleton } from "../components/ListSkeleton";

const PAGE_SIZE = 50;

type StatusTab = ThemeStatus;

const STATUS_TABS: { key: StatusTab; label: string }[] = [
  { key: "active", label: "在册" },
  { key: "pending", label: "待审" },
  { key: "retired", label: "已停用" },
  { key: "merged", label: "已合并" },
];

function themeSourceLabel(source: string): string {
  return THEME_SOURCE_LABEL[source] ?? source;
}

/** 单条题材卡片：浏览信息 + 按状态的 admin 治理操作（审核/停用/合并）。 */
function ThemeCard({
  theme,
  isAdmin,
  activeThemes,
  onAction,
}: {
  theme: ThemeSummary;
  isAdmin: boolean;
  activeThemes: ThemeSummary[];
  onAction: (id: number, action: "approve" | "retire" | "merge", mergeIntoId?: number) => Promise<unknown>;
}) {
  const [mergeInto, setMergeInto] = useState<number | "">("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(humanizeError(e, "操作失败"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card match-card">
      <div className="match-head">
        <div>
          <Link className="match-name" to={`/themes/${theme.id}`}>
            {theme.name}
          </Link>
          <span className={themeStatusChipClass(theme.status)}>{THEME_STATUS_LABEL[theme.status]}</span>
          <span className="chip chip-muted">{themeSourceLabel(theme.source)}</span>
        </div>
        <span className="hint">
          {theme.report_count} 篇研报 · {theme.member_count} 只标的
        </span>
      </div>
      {theme.definition && <p className="hint">{theme.definition}</p>}
      {theme.synonyms.length > 0 && (
        <p className="hint">同义词：{theme.synonyms.join("、")}</p>
      )}
      {theme.merged_into_id != null && (
        <p className="hint">
          已合并入{" "}
          <Link className="hint" to={`/themes/${theme.merged_into_id}`}>
            #{theme.merged_into_id}
          </Link>
        </p>
      )}

      {isAdmin && theme.status !== "merged" && theme.status !== "retired" && (
        <div className="match-actions">
          {theme.status === "pending" && (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy}
              onClick={() => act(() => onAction(theme.id, "approve"))}
            >
              审核在册
            </button>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={() => act(() => onAction(theme.id, "retire"))}
          >
            {theme.status === "pending" ? "驳回" : "停用"}
          </button>
          {activeThemes.length > 0 && (
            <>
              <select value={mergeInto} onChange={(e) => setMergeInto(e.target.value ? Number(e.target.value) : "")}>
                <option value="">合并到…</option>
                {activeThemes
                  .filter((t) => t.id !== theme.id)
                  .map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
              </select>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={busy || mergeInto === ""}
                onClick={() => act(() => onAction(theme.id, "merge", mergeInto as number))}
              >
                合并
              </button>
            </>
          )}
        </div>
      )}
      {error && <p className="form-error">{error}</p>}
    </div>
  );
}

/** 分析师覆盖查询（观点迁移追踪）：复用题材浏览页承载。 */
function CoveragePanel() {
  const [name, setName] = useState("");
  const [broker, setBroker] = useState("");
  const [data, setData] = useState<AuthorCoverage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onSearch = (e: FormEvent) => {
    e.preventDefault();
    const q = name.trim();
    if (!q) return;
    setBusy(true);
    setError(null);
    api
      .getAuthorCoverage(q, undefined, broker.trim() || undefined)
      .then(setData)
      .catch((err) => setError(humanizeError(err, "覆盖查询失败")))
      .finally(() => setBusy(false));
  };

  return (
    <div className="card">
      <h2 className="section-title">分析师覆盖查询</h2>
      <p className="hint measure">按署名看其覆盖的题材与标的，追踪观点迁移（署名由 LLM 从研报提取）。</p>
      <form className="form-row" onSubmit={onSearch}>
        <label className="field">
          <span>分析师姓名</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：王明星" />
        </label>
        <label className="field">
          <span>券商（可选，区分同名）</span>
          <input value={broker} onChange={(e) => setBroker(e.target.value)} placeholder="如：东吴证券" />
        </label>
        <div className="field field-btn">
          <button type="submit" className="btn btn-primary" disabled={busy}>
            查询覆盖
          </button>
        </div>
      </form>
      {error && <p className="form-error">{error}</p>}
      {data && (
        <div>
          <p className="hint">
            {data.name}
            {data.broker ? ` · ${data.broker}` : ""} · 覆盖 {data.reports_total} 篇研报
          </p>
          {data.reports_total === 0 ? (
            <p className="hint">未找到该分析师的研报（署名以分析提取为准）。</p>
          ) : (
            <>
              <p className="hint">
                题材：{" "}
                {data.themes.length === 0
                  ? "—"
                  : data.themes.map((t) => (
                      <span key={`${t.theme_id}-${t.name}`} className="chip chip-muted">
                        {t.theme_id != null ? (
                          <Link to={`/themes/${t.theme_id}`}>{t.name}</Link>
                        ) : (
                          t.name
                        )}
                        （{t.report_ids.length}）
                      </span>
                    ))}
              </p>
              <p className="hint">
                标的：{" "}
                {data.targets.length === 0
                  ? "—"
                  : data.targets.map((t) => (
                      <span key={t.code} className="chip chip-muted">
                        <span className="mono">{t.code}</span> {t.name}（{t.report_ids.length}）
                      </span>
                    ))}
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}

export function ThemesPage() {
  const { user } = useAuth();
  const isAdmin = !!user && user.role === "admin";
  const canPropose = !!user && ROLE_RANK[user.role as Role] >= ROLE_RANK.analyst;

  const [tab, setTab] = useState<StatusTab>("active");
  const [q, setQ] = useState("");
  const [appliedQ, setAppliedQ] = useState("");
  const [page, setPage] = useState(0);
  const [data, setData] = useState<ThemeList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 合并目标候选（admin 打开列表时懒加载一次在册题材）
  const [activeThemes, setActiveThemes] = useState<ThemeSummary[]>([]);

  // 种子导入（admin，202 + 任务轮询）
  const [importTaskId, setImportTaskId] = useState<number | null>(null);
  const [importRefresh, setImportRefresh] = useState(0);
  const { task: importTask } = useTaskPolling(importTaskId, importRefresh);
  const [importError, setImportError] = useState<string | null>(null);

  // 提议表单
  const [propName, setPropName] = useState("");
  const [propDefinition, setPropDefinition] = useState("");
  const [propSynonyms, setPropSynonyms] = useState("");
  const [propMsg, setPropMsg] = useState<string | null>(null);
  const [propError, setPropError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    api
      .listThemes({ status: tab, q: appliedQ.trim() || undefined, limit: PAGE_SIZE, offset: page * PAGE_SIZE })
      .then((res) => {
        setData(res);
        setError(null);
      })
      .catch((e) => setError(humanizeError(e, "加载题材列表失败")))
      .finally(() => setLoading(false));
  }, [tab, appliedQ, page]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!isAdmin || activeThemes.length > 0) return;
    api
      .listThemes({ status: "active", limit: 200 })
      .then((r) => setActiveThemes(r.items))
      .catch(() => setActiveThemes([]));
  }, [isAdmin, activeThemes.length]);

  const onAction = async (id: number, action: "approve" | "retire" | "merge", mergeIntoId?: number) => {
    await api.patchTheme(id, { action, merge_into_id: mergeIntoId });
    load();
  };

  const onImport = async () => {
    setImportError(null);
    try {
      const r = await api.importThemes();
      setImportTaskId(r.task_id);
      setImportRefresh((k) => k + 1);
    } catch (e) {
      setImportError(humanizeError(e, "触发种子导入失败"));
    }
  };

  const onPropose = (e: FormEvent) => {
    e.preventDefault();
    const name = propName.trim();
    if (!name) return;
    setPropMsg(null);
    setPropError(null);
    api
      .proposeTheme({
        name,
        definition: propDefinition.trim() || undefined,
        synonyms: propSynonyms
          .split(/[,，、]/)
          .map((s) => s.trim())
          .filter(Boolean),
      })
      .then(() => {
        setPropMsg(`已提议「${name}」，待管理员审核`);
        setPropName("");
        setPropDefinition("");
        setPropSynonyms("");
        load();
      })
      .catch((err) => setPropError(humanizeError(err, "提议失败")));
  };

  const importRunning = importTaskId !== null && !isTaskSettled(importTask);
  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <section>
      <div className="list-head">
        <h1 className="page-title">题材词表</h1>
        {isAdmin && (
          <button
            type="button"
            className="btn btn-primary"
            onClick={onImport}
            disabled={importRunning}
          >
            {importRunning
              ? `导入${taskStatusLabel(importTask)}…`
              : "导入题材种子"}
          </button>
        )}
      </div>
      {isAdmin && (
        <p className="hint measure">
          种子 = 东财概念（内置快照，滤行情类噪音）+ 申万二级骨架，成分股直接入标的池
          （幂等，可重复执行；需先导入标的主数据）。快照零网络、随版本发布；更新数据在
          backend/ 下重跑 scripts/refresh_em_concept_snapshot.py 并提交发版。
          {importTask?.status === "done" && importTask.result && "themes_created" in importTask.result
            ? ` 上次新建 ${String(importTask.result.themes_created)} 个题材。`
            : ""}
          {importTask?.status === "failed" ? ` 上次导入失败：${JSON.stringify(importTask.result?.error ?? "")}` : ""}
        </p>
      )}
      {importError && <p className="form-error">{importError}</p>}

      <div className="tab-row" role="tablist" aria-label="题材状态">
        {STATUS_TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={t.key === tab}
            className={t.key === tab ? "tab active" : "tab"}
            onClick={() => {
              setTab(t.key);
              setPage(0);
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      <form
        className="card form-row"
        onSubmit={(e) => {
          e.preventDefault();
          setPage(0);
          setAppliedQ(q);
        }}
      >
        <label className="field field-grow">
          <span>搜索（名称 / 同义词）</span>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="如：算力" />
        </label>
        <div className="field field-btn">
          <button type="submit" className="btn btn-primary">
            查询
          </button>
        </div>
      </form>

      {canPropose && (
        <form className="card form-row" onSubmit={onPropose}>
          <label className="field">
            <span>提议新题材</span>
            <input value={propName} onChange={(e) => setPropName(e.target.value)} placeholder="题材名" required />
          </label>
          <label className="field field-grow">
            <span>定义（可选）</span>
            <input value={propDefinition} onChange={(e) => setPropDefinition(e.target.value)} placeholder="一句话定义" />
          </label>
          <label className="field field-grow">
            <span>同义词（可选，逗号分隔）</span>
            <input value={propSynonyms} onChange={(e) => setPropSynonyms(e.target.value)} placeholder="如：eVTOL、飞行汽车" />
          </label>
          <div className="field field-btn">
            <button type="submit" className="btn btn-primary">
              提议
            </button>
          </div>
        </form>
      )}
      {propMsg && <p className="hint">{propMsg}</p>}
      {propError && <p className="form-error">{propError}</p>}

      <CoveragePanel />

      {error && <p className="form-error">{error}</p>}
      {loading ? (
        <ListSkeleton rows={5} />
      ) : !data || data.items.length === 0 ? (
        <EmptyState
          icon={<Tag />}
          title="该状态下暂无题材"
          hint={
            tab === "active"
              ? "可先导入题材种子，或由分析师提议新题材。"
              : "换个状态页签，或先到「在册」看看。"
          }
        />
      ) : (
        data.items.map((t) => (
          <ThemeCard
            key={t.id}
            theme={t}
            isAdmin={isAdmin}
            activeThemes={tab === "active" ? data.items : activeThemes}
            onAction={onAction}
          />
        ))
      )}

      {data && data.total > PAGE_SIZE && (
        <div className="pager">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={page === 0}
            onClick={() => setPage((p) => p - 1)}
          >
            上一页
          </button>
          <span>
            第 {page + 1} / {totalPages} 页 · 共 {data.total} 条
          </span>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={page + 1 >= totalPages}
            onClick={() => setPage((p) => p + 1)}
          >
            下一页
          </button>
        </div>
      )}
    </section>
  );
}
