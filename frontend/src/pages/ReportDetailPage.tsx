import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSlug from "rehype-slug";
import { ApiError, ROLE_RANK, api, humanizeError, type Report, type ReportTargetItem } from "../api";
import { useAuth } from "../auth";
import { formatBytes, formatDate, formatDateTime } from "../format";
import { extractHeadings } from "../markdown";
import { TASK_POLL_INTERVAL_MS } from "../task";

const FONT_SIZE_KEY = "mr:article-font-size";
const FONT_SIZES = ["s", "m", "l"] as const;
type FontSize = (typeof FONT_SIZES)[number];
const FONT_SIZE_LABEL: Record<FontSize, string> = { s: "小", m: "中", l: "大" };

function useArticleFontSize(): [FontSize, (size: FontSize) => void] {
  const [size, setSize] = useState<FontSize>(() => {
    const saved = window.localStorage.getItem(FONT_SIZE_KEY);
    return FONT_SIZES.includes(saved as FontSize) ? (saved as FontSize) : "m";
  });
  const set = (s: FontSize) => {
    setSize(s);
    window.localStorage.setItem(FONT_SIZE_KEY, s);
  };
  return [size, set];
}

function MarkdownView({ markdown }: { markdown: string }) {
  return (
    <Markdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeSlug]}
      components={{
        a: ({ href, children }) => (
          <a href={href} target="_blank" rel="noreferrer noopener">
            {children}
          </a>
        ),
        table: ({ children }) => (
          <div className="table-scroll">
            <table>{children}</table>
          </div>
        ),
      }}
    >
      {markdown}
    </Markdown>
  );
}

const STANCE_LABEL: Record<string, string> = { 推荐: "重点推荐", 提及: "提及", 回避: "回避" };
const CODE_SOURCE_LABEL: Record<string, string> = {
  text: "文本/瀑布",
  inferred: "待人工确认",
  manually_confirmed: "人工确认",
};

/** 当前分析的标的面板（#16 回写产物）：落成代码 + 主数据行业 + 队列状态。 */
function TargetsPanel({ reportId }: { reportId: number }) {
  const [items, setItems] = useState<ReportTargetItem[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.getReportTargets(reportId);
        if (!cancelled) {
          setItems(r.items);
          setLoaded(true);
        }
      } catch {
        if (!cancelled) setLoaded(true); // 面板静默降级，不打断阅读
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [reportId]);

  if (!loaded || items.length === 0) return null;
  return (
    <div className="card files-card">
      <h2 className="section-title">分析标的（{items.length}）</h2>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>标的</th>
              <th>代码</th>
              <th>申万一级</th>
              <th>观点</th>
              <th>代码来源</th>
            </tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <tr key={t.seq} className={t.target_code ? undefined : "row-muted"}>
                <td>{t.target_name ?? t.raw_name}</td>
                <td className="mono">
                  {t.target_code ?? (t.raw_code ? `${t.raw_code}?` : "—")}
                </td>
                <td>{t.sw_l1_name ?? "—"}</td>
                <td>
                  <span className={`chip ${t.stance === "推荐" ? "chip-ok" : "chip-muted"}`}>
                    {STANCE_LABEL[t.stance] ?? t.stance}
                  </span>
                  {t.view && <span className="hint"> {t.view}</span>}
                </td>
                <td>
                  {t.code_source ? (
                    <span className={`chip ${t.code_source === "inferred" ? "chip-warn" : "chip-muted"}`}>
                      {CODE_SOURCE_LABEL[t.code_source] ?? t.code_source}
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {items.some((t) => t.match_status === "pending") && (
        <p className="hint">
          有标的待人工确认，请到 <Link to="/targets">标的确认队列</Link> 处理。
        </p>
      )}
    </div>
  );
}

export function ReportDetailPage() {
  const { id } = useParams();
  const reportId = Number(id);
  const { user } = useAuth();

  const [report, setReport] = useState<Report | null>(null);
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [pending, setPending] = useState(false); // 正文尚未转换完成
  const [error, setError] = useState<string | null>(null);
  const [fontSize, setFontSize] = useArticleFontSize();
  // 读者是否可见下载按钮由 /api/config 的 ALLOW_READER_DOWNLOAD 决定（analyst 起恒可见，无需查询）
  const [readerCanDownload, setReaderCanDownload] = useState(false);
  const isReader = user !== null && ROLE_RANK[user.role] < ROLE_RANK.analyst;
  const canDownload = user !== null && (ROLE_RANK[user.role] >= ROLE_RANK.analyst || readerCanDownload);

  useEffect(() => {
    if (!isReader) return;
    let cancelled = false;
    api
      .getConfig()
      .then((c) => {
        if (!cancelled) setReaderCanDownload(c.allow_reader_download);
      })
      .catch(() => {
        // 配置查询失败按默认（无按钮）处理，不打断阅读
      });
    return () => {
      cancelled = true;
    };
  }, [isReader]);

  useEffect(() => {
    if (!Number.isInteger(reportId) || reportId <= 0) {
      setError("研报不存在（无效的编号）");
      return;
    }
    let cancelled = false;
    setReport(null);
    setMarkdown(null);
    setError(null);
    setPending(false);

    api
      .getReport(reportId)
      .then((r) => {
        if (!cancelled) setReport(r);
      })
      .catch((e) => {
        if (!cancelled) setError(humanizeError(e, "加载研报失败"));
      });

    const loadMarkdown = async () => {
      try {
        const text = await api.getReportMarkdown(reportId);
        if (!cancelled) {
          setMarkdown(text);
          setPending(false);
        }
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) {
          // 转换尚未完成：进入轮询，就绪后自动展示
          setPending(true);
          window.setTimeout(loadMarkdown, TASK_POLL_INTERVAL_MS);
        } else {
          setError(humanizeError(e, "加载正文失败"));
        }
      }
    };
    loadMarkdown();

    return () => {
      cancelled = true;
    };
  }, [reportId]);

  const toc = useMemo(() => (markdown ? extractHeadings(markdown).filter((h) => h.level <= 3) : []), [markdown]);

  if (error && report === null) {
    return (
      <div className="empty-state">
        <p className="form-error">{error}</p>
        <p>
          <Link to="/">返回研报库</Link>
        </p>
      </div>
    );
  }

  return (
    <section>
      <div className="detail-head">
        <div>
          <h1 className="detail-title">{report?.title ?? "…"}</h1>
          {report && (
            <p className="detail-meta">
              <span className="chip chip-muted">{report.broker}</span>
              <span>{formatDate(report.publish_date)}</span>
              <span className="hint">入库于 {formatDateTime(report.created_at)}</span>
            </p>
          )}
        </div>
        <div className="font-size-toggle" role="group" aria-label="正文字号">
          {FONT_SIZES.map((s) => (
            <button
              key={s}
              type="button"
              className={`btn btn-sm ${s === fontSize ? "btn-primary" : "btn-ghost"}`}
              onClick={() => setFontSize(s)}
            >
              {FONT_SIZE_LABEL[s]}
            </button>
          ))}
        </div>
      </div>

      {report && (
        <div className="card files-card">
          <h2 className="section-title">原始文件（{report.files.length}）</h2>
          <ul className="file-list">
            {report.files.map((f) => (
              <li key={f.id} className="file-row">
                <span className="file-name" title={f.filename}>
                  {f.filename}
                </span>
                <span className="hint">{formatBytes(f.size_bytes)}</span>
                <span className={`chip ${f.converted_at ? "chip-ok" : "chip-muted"}`}>
                  {f.converted_at ? "已转换" : "待转换"}
                </span>
                {canDownload && (
                  <a
                    className="btn btn-ghost btn-sm"
                    href={`/api/reports/${report.id}/file?file_id=${f.id}`}
                    download
                  >
                    下载
                  </a>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {report && <TargetsPanel reportId={report.id} />}

      {error && <p className="form-error">{error}</p>}
      {pending && (
        <div className="card converting-banner">
          <span className="spinner" aria-hidden /> 正文转换中，完成后自动展示。若长时间未就绪，任务可能失败，
          <Link to="/upload">到上传页重试</Link>。
        </div>
      )}

      {markdown !== null && (
        <div className={toc.length >= 3 ? "reader reader-with-toc" : "reader"}>
          {toc.length >= 3 && (
            <aside className="toc" aria-label="目录">
              <p className="toc-title">目录</p>
              <nav>
                {toc.map((h) => (
                  <a key={h.id} href={`#${h.id}`} className={`toc-l${h.level}`}>
                    {h.text}
                  </a>
                ))}
              </nav>
            </aside>
          )}
          <article className="article" data-font-size={fontSize}>
            <MarkdownView markdown={markdown} />
          </article>
        </div>
      )}
    </section>
  );
}
