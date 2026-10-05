import { useCallback, useRef, useState, type DragEvent, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { UploadSimple } from "@phosphor-icons/react";
import { api, humanizeError, type ReportCreated } from "../api";
import { formatBytes } from "../format";
import { taskStatusLabel, useTaskPolling } from "../task";

const ACCEPTED_EXTENSIONS = [".pdf", ".docx"];

function accepted(file: File): boolean {
  const name = file.name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => name.endsWith(ext));
}

/** 一次上传的任务卡：轮询到终态，失败可见原因并支持重试。 */
function TaskCard({ upload }: { upload: UploadItem }) {
  const [refreshKey, setRefreshKey] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const { task, error } = useTaskPolling(upload.task_id, refreshKey);

  const onRetry = async () => {
    setRetrying(true);
    setRetryError(null);
    try {
      await api.retryTask(upload.task_id);
      setRefreshKey((k) => k + 1);
    } catch (e) {
      setRetryError(humanizeError(e, "重试失败"));
    } finally {
      setRetrying(false);
    }
  };

  const status = task?.status ?? "uploaded";
  const failed = status === "failed";
  const done = status === "done";
  const failure = failed && task?.result ? String(task.result.error ?? "") : "";
  const failureCode = failed && task?.result ? String(task.result.error_code ?? "") : "";
  const cleanedChars = done && task?.result ? Number(task.result.chars_cleaned ?? 0) : 0;

  return (
    <li className={`card task-card ${failed ? "task-failed" : done ? "task-done" : ""}`}>
      <div className="task-card-head">
        <span className="task-filename" title={upload.filename}>
          {upload.filename}
        </span>
        <span className={`chip ${failed ? "chip-danger" : done ? "chip-ok" : "chip-muted"}`}>
          {done || failed ? "" : <span className="spinner" aria-hidden />}
          {taskStatusLabel(task)}
        </span>
      </div>
      <div className="task-card-body">
        {upload.merged && <p className="hint">已并入既有研报（相同标题/券商/日期）。</p>}
        {done && (
          <p>
            转换完成，正文 {cleanedChars.toLocaleString()} 字符。{" "}
            <Link to={`/reports/${upload.report_id}`}>查看研报 →</Link>
          </p>
        )}
        {failed && (
          <div>
            <p className="form-error">
              {failure}
              {failureCode && failureCode !== "internal" && `（${failureCode}）`}
            </p>
            <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry} disabled={retrying}>
              {retrying ? "重试中…" : "重试"}
            </button>
            {retryError && <p className="form-error">{retryError}</p>}
          </div>
        )}
        {!done && !failed && <p className="hint">任务 #{upload.task_id} 处理中，页面自动刷新状态…</p>}
        {error && <p className="form-error">状态查询失败：{error}（自动重试中）</p>}
      </div>
    </li>
  );
}

interface UploadItem extends ReportCreated {
  filename: string;
}

function todayIso(): string {
  const now = new Date();
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${mm}-${dd}`;
}

export function UploadPage() {
  const [file, setFile] = useState<File | null>(null);
  const [broker, setBroker] = useState("");
  const [publishDate, setPublishDate] = useState(todayIso());
  const [title, setTitle] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  const pickFile = useCallback((f: File | null) => {
    setError(null);
    if (f === null) return;
    if (!accepted(f)) {
      setError("仅支持 PDF / DOCX 文件");
      return;
    }
    setFile(f);
    if (!title) setTitle(f.name.replace(/\.(pdf|docx)$/i, ""));
  }, [title]);

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    pickFile(e.dataTransfer.files.item(0));
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (file === null) {
      setError("请先拖入或选择文件");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const created = await api.uploadReport({
        file,
        broker: broker.trim(),
        publish_date: publishDate,
        title: title.trim() || undefined,
      });
      setUploads((prev) => [{ ...created, filename: file.name }, ...prev]);
      setFile(null);
      setTitle("");
      if (inputRef.current) inputRef.current.value = "";
    } catch (err) {
      setError(humanizeError(err, "上传失败，请重试"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="narrow">
      <h1 className="page-title">上传研报</h1>

      <form className="card upload-form" onSubmit={onSubmit}>
        <div
          className={`dropzone ${dragOver ? "dropzone-over" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          onClick={() => inputRef.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
          }}
        >
          {file ? (
            <p>
              <strong>{file.name}</strong>（{formatBytes(file.size)}）
            </p>
          ) : (
            <>
              <UploadSimple aria-hidden />
              <p>把 PDF / DOCX 拖到这里，或点击选择文件</p>
            </>
          )}
          <input
            ref={inputRef}
            type="file"
            accept=".pdf,.docx,application/pdf"
            hidden
            onChange={(e) => pickFile(e.target.files?.item(0) ?? null)}
          />
        </div>

        <div className="form-row">
          <label className="field">
            <span>券商 *</span>
            <input
              value={broker}
              onChange={(e) => setBroker(e.target.value)}
              placeholder="如：中信证券"
              required
              maxLength={128}
            />
          </label>
          <label className="field">
            <span>发布日期 *</span>
            <input
              type="date"
              value={publishDate}
              onChange={(e) => setPublishDate(e.target.value)}
              required
            />
          </label>
        </div>
        <label className="field">
          <span>标题（可选，默认取文件名）</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={512} />
        </label>

        {error && <p className="form-error">{error}</p>}
        <button type="submit" className="btn btn-primary" disabled={submitting}>
          {submitting ? "上传中…" : "上传并转换"}
        </button>
        <p className="hint">
          相同（标题、券商、日期）的研报会自动并入既有条目；扫描版/加密 PDF 会被拒收并提示原因。
        </p>
      </form>

      {uploads.length > 0 && (
        <>
          <h2 className="section-title">本次上传</h2>
          <ul className="task-list">
            {uploads.map((u) => (
              <TaskCard key={u.task_id} upload={u} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
