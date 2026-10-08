import { describe, expect, it } from "vitest";
import { formatBytes, formatDate, formatDateTime } from "./format";

describe("format 辅助", () => {
  it("formatDate 取 YYYY-MM-DD 前缀", () => {
    expect(formatDate("2026-09-28")).toBe("2026-09-28");
  });

  it("formatDateTime 输出 zh-CN 日期时间", () => {
    expect(formatDateTime("2026-09-28T11:52:46Z")).toMatch(/^2026\/9\/28/);
  });

  it("formatDateTime 对缺失/非法输入返回 —，不裸渲染 Invalid Date", () => {
    expect(formatDateTime(undefined)).toBe("—");
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime("not-a-date")).toBe("—");
  });

  it("formatBytes 按量级选择单位", () => {
    expect(formatBytes(512)).toBe("1 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});
