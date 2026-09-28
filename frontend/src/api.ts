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

export type TaskStatus = "uploaded" | "converting" | "analyzing" | "running" | "done" | "failed";

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
  current_analysis_id?: number | null;
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

export interface TargetSummary {
  code: string;
  name: string;
  exchange: string;
  sw_l1_name: string | null;
  sw_l2_name: string | null;
  sw_l3_name: string | null;
}

export interface TargetList {
  items: TargetSummary[];
}

export interface TargetCandidate {
  code: string;
  name: string;
  exchange: string;
  sw_l1_name: string | null;
  score: number;
}

/** 人工确认队列条目（reason: inferred_code | code_not_in_master | multi_candidate | no_hit） */
export interface TargetMatchItem {
  id: number;
  report_id: number;
  report_title: string;
  analysis_id: number;
  raw_name: string;
  raw_code: string | null;
  reason: string;
  status: string;
  candidates: TargetCandidate[];
  created_at: string;
}

export interface TargetMatchList {
  items: TargetMatchItem[];
}

/** 研报当前分析的标的关联（#16 回写产物） */
export interface ReportTargetItem {
  seq: number;
  raw_name: string;
  raw_code: string | null;
  target_code: string | null;
  target_name: string | null;
  exchange: string | null;
  sw_l1_name: string | null;
  stance: string;
  view: string;
  has_forecast: boolean;
  code_source: string | null;
  match_id: number | null;
  match_status: string | null;
}

export interface ReportTargets {
  analysis_id: number | null;
  items: ReportTargetItem[];
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

// ---------- themes（#17：受控词表） ----------

/** 题材状态机：pending 待审 → active 在册 → merged 合并 / retired 停用 */
export type ThemeStatus = "pending" | "active" | "merged" | "retired";

export const THEME_STATUS_LABEL: Record<ThemeStatus, string> = {
  pending: "待审",
  active: "在册",
  merged: "已合并",
  retired: "已停用",
};

export const THEME_SOURCE_LABEL: Record<string, string> = {
  analysis: "LLM 提议",
  manual: "人工提议",
  seed_em: "东财概念",
  seed_sw: "申万二级",
};

/** 题材状态 → 徽章样式（题材列表/详情页共用）。 */
export function themeStatusChipClass(status: string): string {
  if (status === "active") return "chip chip-ok";
  if (status === "pending") return "chip chip-warn";
  return "chip chip-muted";
}

export interface ThemeSummary {
  id: number;
  name: string;
  status: ThemeStatus;
  definition: string;
  synonyms: string[];
  source: string;
  seed_code: string | null;
  merged_into_id: number | null;
  created_at: string;
  report_count: number;
  member_count: number;
  /** 题材最新综合分析（仅详情接口填充；生成/刷新走 /api/syntheses） */
  latest_synthesis?: { id: number; version: number; created_at: string } | null;
}

export interface ThemeList {
  items: ThemeSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ThemeReportItem {
  id: number;
  title: string;
  broker: string;
  publish_date: string;
  current_analysis_id: number | null;
}

export interface ThemeReportList {
  items: ThemeReportItem[];
  total: number;
}

export interface ThemeMemberItem {
  code: string;
  name: string;
  exchange: string;
  sw_l1_name: string | null;
  source: string;
  joined_at: string;
  is_active: boolean;
}

export interface ThemeMemberList {
  items: ThemeMemberItem[];
}

export interface AuthorItem {
  name: string;
  cert: string | null;
  broker: string | null;
  report_count: number;
}

export interface CoverageThemeItem {
  theme_id: number | null;
  name: string;
  status: ThemeStatus | null;
  report_ids: number[];
}

export interface CoverageTargetItem {
  code: string;
  name: string;
  report_ids: number[];
}

export interface AuthorCoverage {
  name: string;
  cert: string | null;
  broker: string | null;
  reports_total: number;
  themes: CoverageThemeItem[];
  targets: CoverageTargetItem[];
}

// ---------- subscriptions / connector（#19：订阅与连接器） ----------

/** 订阅：绑题材，题材名+同义词自动展开为查询词 */
export interface Subscription {
  id: number;
  theme_id: number;
  theme_name: string;
  connector_id: string;
  created_by: number;
  enabled: boolean;
  interval_hours: number;
  auto_download: boolean;
  keywords: string[] | null;
  orgs: string[] | null;
  next_run_at: string | null;
  last_run_at: string | null;
  last_success_at: string | null;
  attempt: number;
  consecutive_failures: number;
  created_at: string;
}

export interface SubscriptionList {
  items: Subscription[];
  total: number;
}

/** 发现记录状态：seen 仅元数据 | ingested 已下载入库 | duplicate 库内已有 | fetch_failed 下载失败 */
export type RefStatus = "seen" | "ingested" | "duplicate" | "fetch_failed";

export const REF_STATUS_LABEL: Record<RefStatus, string> = {
  seen: "待下载",
  ingested: "已入库",
  duplicate: "库内已有",
  fetch_failed: "下载失败",
};

export function refStatusChipClass(status: string): string {
  if (status === "ingested") return "chip chip-ok";
  if (status === "seen") return "chip chip-warn";
  if (status === "fetch_failed") return "chip chip-danger";
  return "chip chip-muted";
}

export interface RefItem {
  id: number;
  connector_id: string;
  external_id: string;
  status: RefStatus;
  title: string;
  broker: string | null;
  publish_date: string;
  industry: string | null;
  pages: number | null;
  snippet: string | null;
  report_id: number | null;
  subscription_id: number | null;
  report_url: string | null;
  discovered_at: string;
  fetched_at: string | null;
  last_error: string | null;
}

export interface RefList {
  items: RefItem[];
  total: number;
}

export interface ManualDownloadResult {
  ref: RefItem;
  report_id: number;
  task_id: number;
}

/** 连接器日志事件：run 每轮执行 | retry 退避重试 | dead_letter 死信 | alert 告警 | manual_download 手动下载 */
export type ConnectorRunEvent =
  | "run"
  | "retry"
  | "dead_letter"
  | "alert"
  | "manual_download";

export const RUN_EVENT_LABEL: Record<ConnectorRunEvent, string> = {
  run: "执行",
  retry: "退避重试",
  dead_letter: "死信",
  alert: "告警",
  manual_download: "手动下载",
};

export function runEventChipClass(event: string): string {
  if (event === "alert" || event === "dead_letter") return "chip chip-danger";
  if (event === "manual_download") return "chip chip-ok";
  return "chip chip-muted";
}

export interface ConnectorRunItem {
  id: number;
  connector_id: string;
  subscription_id: number | null;
  event: ConnectorRunEvent;
  ok: boolean;
  message: string;
  stats: Record<string, unknown> | null;
  created_at: string;
}

export interface ConnectorRunList {
  items: ConnectorRunItem[];
  total: number;
}

/** 下载额度汇总（手动下载确认框的提示数据源） */
export interface ConnectorQuota {
  downloads_today: number;
  downloads_total: number;
  hints: Record<string, string>;
}

// ---------- syntheses（#20：题材跨报告综合分析） ----------

/** 结论/分歧点条目：report_refs 是输入材料序位（1 起始），映射 evidence.reports 序位 */
export interface SynthesisConclusion {
  text: string;
  report_refs: number[];
}

export interface SynthesisConsensusTarget {
  name: string;
  code: string | null;
  view: string;
  report_refs: number[];
}

export interface SynthesisResult {
  common_conclusions: SynthesisConclusion[];
  consensus_targets: SynthesisConsensusTarget[];
  divergences: SynthesisConclusion[];
  input_total: number;
  truncated: boolean;
}

/** 证据边界研报条目（序位 = 引用编号，点击回链研报详情） */
export interface SynthesisReportRef {
  id: number;
  title: string;
  broker: string;
  publish_date: string;
}

export interface SynthesisVersionRef {
  id: number;
  version: number;
  created_at: string;
}

export interface SynthesisDetail {
  id: number;
  theme_id: number;
  theme_name: string;
  version: number;
  input_fingerprint: string;
  report_ids: number[];
  prompt_version: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  duration_ms: number;
  result: SynthesisResult;
  created_by: number;
  created_at: string;
  versions: SynthesisVersionRef[];
  reports: SynthesisReportRef[];
}

/** POST /api/syntheses 响应：cached=true 命中缓存（200）；否则 202 + task 轮询 */
export interface SynthesisCreated {
  cached: boolean;
  synthesis_id: number | null;
  version: number | null;
  task_id: number | null;
  report_count: number;
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

  listReports: (filters: { q?: string; broker?: string; date_from?: string; date_to?: string; limit: number; offset: number }) =>
    request<ReportList>(`/api/reports${qs(filters)}`),

  getReport: (id: number) => request<Report>(`/api/reports/${id}`),

  /** 正文 markdown（text/markdown 响应）；未就绪时后端返回 404 */
  getReportMarkdown: (id: number) => request<string>(`/api/reports/${id}/markdown`),

  // ---------- targets（#16：主数据 + 人工确认队列） ----------

  searchTargets: (q: string, limit: number = 20) =>
    request<TargetList>(`/api/targets${qs({ q, limit })}`),

  listTargetMatches: () => request<TargetMatchList>("/api/targets/matches"),

  /** 确认队列条目到指定 6 位代码（必须已在主数据中） */
  confirmTargetMatch: (matchId: number, code: string) =>
    request<TargetMatchItem>(`/api/targets/matches/${matchId}/confirm`, {
      method: "POST",
      body: JSON.stringify({ code }),
    }),

  /** 驳回：认定该串不是真实标的（LLM 幻觉/指数名等） */
  dismissTargetMatch: (matchId: number) =>
    request<void>(`/api/targets/matches/${matchId}/dismiss`, { method: "POST" }),

  /** 当前分析的标的关联（含瀑布落成与队列状态） */
  getReportTargets: (reportId: number) => request<ReportTargets>(`/api/reports/${reportId}/targets`),

  /** 触发主数据全量导入（admin，幂等；withNameHistory 开启逐股曾用名回填，较慢） */
  importTargets: (withNameHistory: boolean = false) =>
    request<{ task_id: number }>("/api/targets/import", {
      method: "POST",
      body: JSON.stringify({ with_name_history: withNameHistory }),
    }),

  // ---------- themes（#17） ----------

  listThemes: (filters: { status?: ThemeStatus | "all"; q?: string; limit?: number; offset?: number }) =>
    request<ThemeList>(`/api/themes${qs(filters)}`),

  getTheme: (id: number) => request<ThemeSummary>(`/api/themes/${id}`),

  /** 提议新题材（analyst 起；与在册/待审重名 409） */
  proposeTheme: (body: { name: string; definition?: string; synonyms?: string[] }) =>
    request<ThemeSummary>("/api/themes", { method: "POST", body: JSON.stringify(body) }),

  /** 治理动作（admin）：approve / retire / merge */
  patchTheme: (
    id: number,
    body: { action: "approve" | "retire" | "merge"; definition?: string; synonyms?: string[]; merge_into_id?: number },
  ) => request<ThemeSummary>(`/api/themes/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  listThemeReports: (id: number, limit: number = 20, offset: number = 0) =>
    request<ThemeReportList>(`/api/themes/${id}/reports${qs({ limit, offset })}`),

  listThemeMembers: (id: number, activeOnly: boolean = false) =>
    request<ThemeMemberList>(`/api/themes/${id}/members${qs({ active_only: activeOnly ? "true" : undefined })}`),

  /** 触发题材种子导入（admin，幂等）：东财概念 + 申万二级骨架 */
  importThemes: () => request<{ task_id: number }>("/api/themes/import", { method: "POST", body: "{}" }),

  // ---------- syntheses（#20） ----------

  /** 一键生成：输入指纹未变命中缓存（cached=true 直接跳结果页）；变了走任务轮询（同输入在途任务复用） */
  createSynthesis: (themeId: number) =>
    request<SynthesisCreated>("/api/syntheses", {
      method: "POST",
      body: JSON.stringify({ theme_id: themeId }),
    }),

  getSynthesis: (id: number) => request<SynthesisDetail>(`/api/syntheses/${id}`),

  /** 手动刷新：强制重算 version++，纳入刷新时点的最新研报集合 */
  refreshSynthesis: (id: number) =>
    request<{ task_id: number; synthesis_id: number; report_count: number }>(`/api/syntheses/${id}/refresh`, {
      method: "POST",
    }),

  // ---------- authors（#17：覆盖查询） ----------

  searchAuthors: (q: string) => request<{ items: AuthorItem[] }>(`/api/authors${qs({ q })}`),

  /** 分析师覆盖：其研报所涉题材与标的（观点迁移追踪） */
  getAuthorCoverage: (name: string, cert?: string, broker?: string) =>
    request<AuthorCoverage>(`/api/authors/coverage${qs({ name, cert, broker })}`),

  // ---------- subscriptions / connector（#19） ----------

  listSubscriptions: () => request<SubscriptionList>("/api/subscriptions"),

  createSubscription: (body: { theme_id: number; connector_id?: string; interval_hours?: number; auto_download?: boolean; keywords?: string[]; orgs?: string[] }) =>
    request<Subscription>("/api/subscriptions", { method: "POST", body: JSON.stringify(body) }),

  /** enabled/interval/keywords/orgs 归属人可改；auto_download 仅 admin（额度管控） */
  patchSubscription: (
    id: number,
    body: { enabled?: boolean; interval_hours?: number; auto_download?: boolean; keywords?: string[]; orgs?: string[] },
  ) => request<Subscription>(`/api/subscriptions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  deleteSubscription: (id: number) => request<void>(`/api/subscriptions/${id}`, { method: "DELETE" }),

  /** 发现记录（手动下载工作池）；status 过滤可组合 */
  listRefs: (filters: { status_filter?: string; limit?: number; offset?: number } = {}) =>
    request<RefList>(`/api/subscriptions/refs${qs(filters)}`),

  /** 手动单篇下载（额度提示后的确认动作）→ 入库 + convert 任务 */
  downloadRef: (refId: number) =>
    request<ManualDownloadResult>(`/api/subscriptions/refs/${refId}/download`, { method: "POST" }),

  /** 额度汇总 + 各连接器下载提示文案 */
  getConnectorQuota: () => request<ConnectorQuota>("/api/subscriptions/quota"),

  /** 连接器运行日志（admin）：成功/失败/死信/告警/手动下载 */
  listConnectorRuns: (filters: { event?: string; subscription_id?: number; limit?: number; offset?: number } = {}) =>
    request<ConnectorRunList>(`/api/admin/connector-runs${qs(filters)}`),
};
