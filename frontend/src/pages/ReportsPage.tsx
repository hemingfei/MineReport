import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, humanizeError, type ReportList } from "../api";
import { ROLE_RANK, type Role } from "../api";
import { formatDate } from "../format";
import { useAuth } from "../auth";

const PAGE_SIZE = 20;

export function ReportsPage() {
  const navigate = useNavigate();
  const { user } = useAuth();

  // 提交态（点查询/回车才生效）与展示态分离，避免每敲一个字就发请求
  const [q, setQ] = useState("");
  const [broker, setBroker] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [applied, setApplied] = useState({ q: "", broker: "", dateFrom: "", dateTo: "" });
  const [page, setPage] = useState(0);

  const [data, setData] = useState<ReportList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .listReports({
        q: applied.q.trim() || undefined,
        broker: applied.broker.trim() || undefined,
        date_from: applied.dateFrom || undefined,
        date_to: applied.dateTo || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      })
      .then((res) => {
        if (!cancelled) {
          setData(res);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(humanizeError(e, "加载研报列表失败"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [applied, page]);

  const onSearch = (e: FormEvent) => {
    e.preventDefault();
    setPage(0);
    setApplied({ q, broker, dateFrom, dateTo });
  };

  const onReset = () => {
    setQ("");
    setBroker("");
    setDateFrom("");
    setDateTo("");
    setPage(0);
    setApplied({ q: "", broker: "", dateFrom: "", dateTo: "" });
  };

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <section>
      <div className="list-head">
        <h1 className="page-title">研报库</h1>
        {user && ROLE_RANK[user.role as Role] >= ROLE_RANK.analyst && (
          <Link className="btn btn-primary" to="/upload">
            上传研报
          </Link>
        )}
      </div>

      <form className="card form-row" onSubmit={onSearch}>
        <label className="field">
          <span>关键词（标题/正文/总结）</span>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="如：算力、创新药" />
        </label>
        <label className="field">
          <span>券商（精确匹配）</span>
          <input value={broker} onChange={(e) => setBroker(e.target.value)} placeholder="如：中信证券" />
        </label>
        <label className="field">
          <span>发布日期从</span>
          <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
        </label>
        <label className="field">
          <span>至</span>
          <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
        </label>
        <div className="field field-btn">
          <button type="submit" className="btn btn-primary">
            查询
          </button>
          <button type="button" className="btn btn-ghost" onClick={onReset}>
            重置
          </button>
        </div>
      </form>

      {error && <p className="form-error">{error}</p>}
      {loading ? (
        <div className="page-loading">加载中…</div>
      ) : (
        <div className="card table-card">
          <table className="data-table">
            <thead>
              <tr>
                <th>发布日期</th>
                <th>标题</th>
                <th>券商</th>
                <th>文件</th>
              </tr>
            </thead>
            <tbody>
              {!data || data.items.length === 0 ? (
                <tr>
                  <td colSpan={4} className="empty-cell">
                    没有符合条件的研报
                  </td>
                </tr>
              ) : (
                data.items.map((r) => (
                  <tr
                    key={r.id}
                    className="row-link"
                    onClick={() => navigate(`/reports/${r.id}`)}
                    title="点击查看详情"
                  >
                    <td className="cell-nowrap">{formatDate(r.publish_date)}</td>
                    <td>{r.title}</td>
                    <td className="cell-nowrap">{r.broker}</td>
                    <td className="cell-nowrap">{r.files.length}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {data && (
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
