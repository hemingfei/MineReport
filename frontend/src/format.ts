/** 展示格式化辅助（纯函数，可单测）。 */

export function formatDateTime(iso: string | undefined | null): string {
  const d = new Date(iso ?? "");
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString("zh-CN", { hour12: false });
}

export function formatDate(iso: string): string {
  return iso.slice(0, 10);
}

export function formatBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}
