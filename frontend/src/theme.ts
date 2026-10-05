import { useCallback, useEffect, useState } from "react";

export type ThemeChoice = "light" | "dark" | "system";

const THEME_KEY = "mr:theme";

function systemPrefersDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/** 把选择落到 <html data-theme> 上；system 跟随系统并即时响应系统切换。 */
export function applyTheme(choice: ThemeChoice): void {
  const dark = choice === "dark" || (choice === "system" && systemPrefersDark());
  const root = document.documentElement;
  root.dataset.theme = dark ? "dark" : "light";
  root.style.colorScheme = dark ? "dark" : "light";
}

export function getStoredTheme(): ThemeChoice {
  const saved = window.localStorage.getItem(THEME_KEY);
  return saved === "light" || saved === "dark" || saved === "system" ? saved : "system";
}

/** 主题三态（浅 / 深 / 随系统）：读写 localStorage，多组件共享同一 HTML 属性，无需 Context。 */
export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>(getStoredTheme);

  useEffect(() => {
    applyTheme(choice);
    window.localStorage.setItem(THEME_KEY, choice);
  }, [choice]);

  // 跟随系统时，系统深浅切换要即时生效
  useEffect(() => {
    if (choice !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme("system");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [choice]);

  const update = useCallback((next: ThemeChoice) => setChoice(next), []);
  return [choice, update];
}
