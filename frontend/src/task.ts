/** 任务轮询 hook、终态处理与状态展示词表（spec：前端 3~5s 轮询）。
 * 任务 result 的键契约（synthesis_id/error_code/error）与终态文案模板单点收敛于此，
 * 页面不手拼。 */

import { useEffect, useRef, useState } from "react";
import { api, type Task, type TaskStatus } from "./api";

export const TASK_POLL_INTERVAL_MS = 4000;

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  uploaded: "已入队",
  converting: "转换中",
  analyzing: "分析中",
  running: "处理中",
  done: "已完成",
  failed: "失败",
};

export function isTerminal(status: TaskStatus): boolean {
  return status === "done" || status === "failed";
}

/** 任务是否已拉到且到终态（null=尚未拉到，不算 settled）。
 * "进行中"判定统一为 `taskId != null && !isTaskSettled(task)`：首次点击导入后
 * 即视为进行中，消掉首轮轮询返回前的重入窗口；重复导入时任务对象仍是上一轮
 * 终态（useTaskPolling 不清旧 task），首拍窗与旧版一致，后端幂等兜底。 */
export function isTaskSettled(task: Task | null): boolean {
  return task != null && isTerminal(task.status);
}

/** done 任务的产物 id 键契约（当前唯一取数键：synthesize → synthesis_id）；
 * 非法/缺席归 0（页面以 >0 判可跳转）。 */
export function taskResultId(task: Task): number {
  return Number(task.result?.synthesis_id ?? 0) || 0;
}

/** failed 任务的单行错误文案（form-error 模板：error_code：error，缺省逐级兜底）。 */
export function taskFailureText(task: Task, fallback: string): string {
  return `${task.result?.error_code ?? "failed"}：${task.result?.error ?? fallback}`;
}

/** 按钮态状态词：任务尚未拉到（null）视作已入队；未知状态原样兜底。 */
export function taskStatusLabel(task: Task | null): string {
  const s = task?.status ?? "uploaded";
  return TASK_STATUS_LABEL[s] ?? s;
}

/**
 * 终态半场（与 useTaskPolling 的轮询半场配对）：done → onDone(产物 id)；
 * failed → onFailed(错误文案)。taskId 的清理与后续动作留给页面回调。
 * 终态只对同一个 task 对象触发一次；handlers 经 latest-ref 读取，无需 memo。
 */
export function useTaskTerminal(
  task: Task | null,
  failFallback: string,
  handlers: { onDone: (resultId: number) => void; onFailed: (text: string) => void },
) {
  const latest = useRef({ failFallback, handlers });
  latest.current = { failFallback, handlers };

  useEffect(() => {
    if (task == null) return;
    if (task.status === "done") latest.current.handlers.onDone(taskResultId(task));
    else if (task.status === "failed")
      latest.current.handlers.onFailed(taskFailureText(task, latest.current.failFallback));
  }, [task]);
}

/**
 * 轮询任务直到终态；refreshKey 变化（如失败后重试）会立即重新拉取并恢复轮询。
 * 网络错误不终止轮询（会话过期除外——由 ApiError 广播交 AuthProvider 处理）。
 */
export function useTaskPolling(taskId: number | null, refreshKey: number = 0) {
  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (taskId === null) return;
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const t = await api.getTask(taskId);
        if (cancelled) return;
        setTask(t);
        setError(null);
        if (!isTerminal(t.status)) timer = window.setTimeout(poll, TASK_POLL_INTERVAL_MS);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        timer = window.setTimeout(poll, TASK_POLL_INTERVAL_MS);
      }
    };

    poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [taskId, refreshKey]);

  return { task, error };
}
