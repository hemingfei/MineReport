import { describe, expect, it } from "vitest";
import { formatBytes, formatDate } from "./format";

describe("format 辅助", () => {
  it("formatDate 取 YYYY-MM-DD 前缀", () => {
    expect(formatDate("2026-09-28")).toBe("2026-09-28");
  });

  it("formatBytes 按量级选择单位", () => {
    expect(formatBytes(512)).toBe("1 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});
