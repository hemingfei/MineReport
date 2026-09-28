import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, humanizeError, type TargetMatchItem, type TargetSummary } from "../api";
import { useAuth } from "../auth";
import { formatDateTime } from "../format";
import { isTaskSettled, taskStatusLabel, useTaskPolling } from "../task";

const REASON_LABEL: Record<string, string> = {
  inferred_code: "LLM 补码不可信",
  code_not_in_master: "代码不在主数据",
  multi_candidate: "多个候选",
  no_hit: "无匹配",
};

/** 手动指定代码：输名称搜主数据（/api/targets）或直接敲 6 位码。 */
function CodePicker({ onPick }: { onPick: (code: string) => void }) {
  const [text, setText] = useState("");
  const [suggestions, setSuggestions] = useState<TargetSummary[]>([]);

  useEffect(() => {
    const q = text.trim();
    if (q.length < 2) {
      setSuggestions([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      api
        .searchTargets(q, 6)
        .then((r) => {
          if (!cancelled) setSuggestions(r.items);
        })
        .catch(() => {
          if (!cancelled) setSuggestions([]);
        });
    }, 250); // 防抖
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [text]);

  return (
    <div className="code-picker">
      <input
        placeholder="名称或 6 位代码"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      {suggestions.length > 0 && (
        <ul className="candidate-list">
          {suggestions.map((s) => (
            <li key={s.code}>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => onPick(s.code)}>
                <span className="mono">{s.code}</span> {s.name}
                {s.sw_l1_name ? ` · ${s.sw_l1_name}` : ""}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** 每条队列：候选一键选用 + 手动搜码/输码确认 + 驳回。确认/驳回后从列表移除。 */
function MatchCard({
  item,
  onResolved,
}: {
  item: TargetMatchItem;
  onResolved: (id: number) => void;
}) {
  const [code, setCode] = useState<string>(item.raw_code ?? item.candidates[0]?.code ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onResolved(item.id);
    } catch (e) {
      setError(humanizeError(e, "操作失败"));
      setBusy(false);
    }
  };

  return (
    <div className="card match-card">
      <div className="match-head">
        <div>
          <span className="match-name">{item.raw_name}</span>
          {item.raw_code && <span className="chip chip-muted mono">{item.raw_code}</span>}
          <span className="chip chip-warn">{REASON_LABEL[item.reason] ?? item.reason}</span>
        </div>
        <Link className="hint" to={`/reports/${item.report_id}`}>
          {item.report_title}
        </Link>
      </div>
      <p className="hint">入队 {formatDateTime(item.created_at)}</p>

      {item.candidates.length > 0 && (
        <ul className="candidate-list">
          {item.candidates.map((c) => (
            <li key={c.code}>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={busy}
                onClick={() => setCode(c.code)}
              >
                <span className="mono">{c.code}</span> {c.name}
                {c.sw_l1_name ? ` · ${c.sw_l1_name}` : ""}
                {c.score ? ` · ${(c.score * 100).toFixed(0)}%` : ""}
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="match-actions">
        <input
          className="mono"
          inputMode="numeric"
          maxLength={6}
          placeholder="6 位代码"
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
        />
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={busy || code.length !== 6}
          onClick={() => act(() => api.confirmTargetMatch(item.id, code))}
        >
          确认
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          disabled={busy}
          onClick={() => act(() => api.dismissTargetMatch(item.id))}
        >
          不是标的
        </button>
      </div>
      {error && <p className="form-error">{error}</p>}
      <CodePicker onPick={setCode} />
    </div>
  );
}

export function TargetQueuePage() {
  const { user } = useAuth();
  const [items, setItems] = useState<TargetMatchItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 主数据导入（admin，幂等；202 + 任务轮询）
  const [importTaskId, setImportTaskId] = useState<number | null>(null);
  const [importRefresh, setImportRefresh] = useState(0);
  const { task: importTask } = useTaskPolling(importTaskId, importRefresh);
  const [importError, setImportError] = useState<string | null>(null);
  const [withHistory, setWithHistory] = useState(false);

  const load = useCallback(() => {
    api
      .listTargetMatches()
      .then((r) => setItems(r.items))
      .catch((e) => setError(humanizeError(e, "加载队列失败")))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onImport = async () => {
    setImportError(null);
    try {
      const r = await api.importTargets(withHistory);
      setImportTaskId(r.task_id);
      setImportRefresh((k) => k + 1);
    } catch (e) {
      setImportError(humanizeError(e, "触发导入失败"));
    }
  };

  const importRunning = importTaskId !== null && !isTaskSettled(importTask);

  return (
    <section>
      <h1 className="page-title">标的确认队列</h1>

      {user?.role === "admin" && (
        <div className="card form-row">
          <span className="hint">
            主数据：akshare 全量代码+名称 + 申万行业快照（幂等，可重复执行）。
            {importTask && importTask.result && "targets_total" in importTask.result
              ? ` 上次导入 ${String(importTask.result.targets_total)} 只标的。`
              : ""}
          </span>
          <label className="field">
            <input
              type="checkbox"
              checked={withHistory}
              onChange={(e) => setWithHistory(e.target.checked)}
            />
            <span>同时回填曾用名（约 30 分钟）</span>
          </label>
          <div className="field field-btn">
            <button
              type="button"
              className="btn btn-primary"
              onClick={onImport}
              disabled={importRunning}
            >
              {importRunning ? `导入${taskStatusLabel(importTask)}…` : "导入 / 刷新主数据"}
            </button>
          </div>
        </div>
      )}
      {importError && <p className="form-error">{importError}</p>}

      {loading ? (
        <div className="page-loading">加载中…</div>
      ) : error ? (
        <p className="form-error">{error}</p>
      ) : items.length === 0 ? (
        <div className="empty-state">
          <p>队列已清空：所有未能自动确认的标的都已处理。</p>
        </div>
      ) : (
        items.map((m) => (
          <MatchCard key={m.id} item={m} onResolved={(id) => setItems((prev) => prev.filter((x) => x.id !== id))} />
        ))
      )}
    </section>
  );
}
