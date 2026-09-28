/** 后端 API 类型定义与 fetch 客户端（同源 /api，cookie 会话）。 */

export type Role = "admin" | "analyst" | "reader";

export const ROLE_RANK: Record<Role, number> = { reader: 0, analyst: 1, admin: 2 };

export const ROLE_LABEL: Record<Role, string> = {
  admin: "管理员",
  analyst: "分析师",
  reader: "读者",
};

export interface User {
  id: number;
  email: string;
  display_name: string;
  role: Role;
}

export type TaskStatus = "uploaded" | "converting" | "analyzing" | "done" | "failed";

export interface Task {
  id: number;
  kind: string;
  status: TaskStatus;
  payload: Record<string, unknown> | null;
  /** done：{report_id, report_file_id, chars_raw, chars_cleaned}；failed：{error_code, error, stage} */
  result: Record<string, unknown> | null;
  attempts: number;
}

export interface ReportFile {
  id: number;
  filename: string;
  content_type: string | null;
  size_bytes: number;
  file_sha256: string;
  converted_at: string | null;
}

export interface Report {
  id: number;
  title: string;
  broker: string;
  publish_date: string;
  created_by: number;
  created_at: string;
  files: ReportFile[];
}

export interface ReportList {
  items: Report[];
  total: number;
  limit: number;
  offset: number;
}

export interface ReportCreated {
  task_id: number;
  report_id: number;
  file_id: number;
  merged: boolean;
}

export interface AppConfig {
  allow_reader_download: boolean;
}

export interface Invitation {
  id: number;
  token: string;
  role: Role;
  email: string | null;
  expires_at: string;
  used_at: string | null;
  created_at: string;
}

/** 会话失效（401）时广播，AuthProvider 监听后清空登录态。 */
export const UNAUTHORIZED_EVENT = "mr:unauthorized";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
  }
}

/** 后端英文 detail 的中文映射（未命中原样展示）。 */
export function humanizeError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    const map: Record<string, string> = {
      "invalid email or password": "邮箱或密码错误",
      "user disabled": "账号已被禁用，请联系管理员",
      "invalid invitation": "邀请码无效或已被使用",
      "invitation expired": "邀请码已过期",
      "invitation is bound to another email": "该邀请码绑定了其他邮箱",
      "email already registered": "该邮箱已注册，请直接登录",
    };
    return map[err.detail] ?? err.detail;
  }
  return err instanceof Error ? err.message : fallback;
}

function detailToString(status: number, body: unknown): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    // FastAPI 校验错误是数组 [{msg, loc, ...}]
    if (Array.isArray(detail)) {
      const msgs = detail
        .map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : null))
        .filter((m): m is string => m !== null);
      if (msgs.length > 0) return msgs.join("；");
    }
  }
  return `请求失败（HTTP ${status}）`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body != null && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (res.status === 204) {
    return undefined as T;
  }
  const body: unknown = res.headers.get("content-type")?.includes("application/json")
    ? await res.json()
    : await res.text();
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    }
    throw new ApiError(res.status, detailToString(res.status, body));
  }
  return body as T;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const s = search.toString();
  return s ? `?${s}` : "";
}

// ---------- auth ----------

export const api = {
  login: (email: string, password: string) =>
    request<User>("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),

  register: (body: { token: string; email: string; password: string; display_name?: string }) =>
    request<User>("/api/auth/register", { method: "POST", body: JSON.stringify(body) }),

  logout: () => request<void>("/api/auth/logout", { method: "POST" }),

  me: () => request<User>("/api/auth/me"),

  getConfig: () => request<AppConfig>("/api/config"),

  // ---------- admin ----------

  createInvitation: (role: Role, email?: string) =>
    request<Invitation>("/api/admin/invitations", {
      method: "POST",
      body: JSON.stringify({ role, email: email || null }),
    }),

  listInvitations: () => request<Invitation[]>("/api/admin/invitations"),

  // ---------- tasks ----------

  getTask: (taskId: number) => request<Task>(`/api/tasks/${taskId}`),

  retryTask: (taskId: number) => request<Task>(`/api/tasks/${taskId}/retry`, { method: "POST" }),

  // ---------- reports ----------

  uploadReport: (form: { file: File; broker: string; publish_date: string; title?: string }) => {
    const data = new FormData();
    data.set("file", form.file);
    data.set("broker", form.broker);
    data.set("publish_date", form.publish_date);
    if (form.title) data.set("title", form.title);
    return request<ReportCreated>("/api/reports", { method: "POST", body: data });
  },

  listReports: (filters: { broker?: string; date_from?: string; date_to?: string; limit: number; offset: number }) =>
    request<ReportList>(`/api/reports${qs(filters)}`),

  getReport: (id: number) => request<Report>(`/api/reports/${id}`),

  /** 正文 markdown（text/markdown 响应）；未就绪时后端返回 404 */
  getReportMarkdown: (id: number) => request<string>(`/api/reports/${id}/markdown`),
};
