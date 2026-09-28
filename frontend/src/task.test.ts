import { describe, expect, it } from "vitest";
import {
  isTaskSettled,
  isTerminal,
  taskFailureText,
  taskResultId,
  taskStatusLabel,
  TASK_STATUS_LABEL,
} from "./task";
import type { Task, TaskStatus } from "./api";

function mkTask(status: TaskStatus, result?: Task["result"]): Task {
  return { id: 1, kind: "synthesize", status, payload: null, result: result ?? null, attempts: 0 };
}

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

  it("isTaskSettled：null 不算 settled（taskId 已设即视为进行中，防首拍重入），终态才算", () => {
    expect(isTaskSettled(null)).toBe(false);
    expect(isTaskSettled(mkTask("uploaded"))).toBe(false);
    expect(isTaskSettled(mkTask("converting"))).toBe(false);
    expect(isTaskSettled(mkTask("done"))).toBe(true);
    expect(isTaskSettled(mkTask("failed"))).toBe(true);
  });
});

describe("task 终态键契约", () => {
  it("taskResultId：done 从 result.synthesis_id 取产物 id", () => {
    expect(taskResultId(mkTask("done", { synthesis_id: 42 }))).toBe(42);
  });

  it("taskResultId：键缺席/非法时归零（页面以 >0 判可跳转）", () => {
    expect(taskResultId(mkTask("done"))).toBe(0);
    expect(taskResultId(mkTask("done", { synthesis_id: "x" }))).toBe(0);
  });

  it("taskFailureText：error_code：error 全在", () => {
    expect(taskFailureText(mkTask("failed", { error_code: "convert_error", error: "扫描版 PDF" }), "兜底")).toBe(
      "convert_error：扫描版 PDF",
    );
  });

  it("taskFailureText：逐级缺省——code 落 failed，error 落页面兜底文案", () => {
    expect(taskFailureText(mkTask("failed", { error: "超时" }), "兜底")).toBe("failed：超时");
    expect(taskFailureText(mkTask("failed"), "生成失败，请重试")).toBe("failed：生成失败，请重试");
  });
});

describe("task 按钮态状态词", () => {
  it("null（尚未拉到）视作已入队", () => {
    expect(taskStatusLabel(null)).toBe("已入队");
  });

  it("在途/终态查词表，未知状态原样兜底", () => {
    expect(taskStatusLabel(mkTask("converting"))).toBe("转换中");
    expect(taskStatusLabel(mkTask("done"))).toBe("已完成");
    // 伪造词表外状态——查不到标签时须原样兜底，不至于渲染空白
    expect(taskStatusLabel(mkTask("queued" as TaskStatus))).toBe("queued");
  });
});
