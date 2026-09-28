import { describe, expect, it } from "vitest";
import { isTerminal, TASK_STATUS_LABEL } from "./task";
import type { TaskStatus } from "./api";

describe("task 状态词表", () => {
  it("六个状态都有中文标签", () => {
    const statuses: TaskStatus[] = ["uploaded", "converting", "analyzing", "running", "done", "failed"];
    for (const s of statuses) {
      expect(TASK_STATUS_LABEL[s]).toBeTruthy();
    }
  });

  it("终态判定：done/failed 为终态，其余继续轮询", () => {
    expect(isTerminal("done")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
    expect(isTerminal("uploaded")).toBe(false);
    expect(isTerminal("converting")).toBe(false);
    expect(isTerminal("analyzing")).toBe(false);
    expect(isTerminal("running")).toBe(false);
  });
});
