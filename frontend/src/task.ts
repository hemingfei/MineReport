/** 任务轮询 hook 与状态展示词表（spec：前端 3~5s 轮询）。 */

import { useEffect, useState } from "react";
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
