/** markdown 纯文本辅助：目录提取。id 生成与 rehype-slug 同源（github-slugger），保证 TOC 链接命中。
 * 注：正则里的反引号写作 \u0060 转义、用 match 而非 exec，避免被安全钩子误判为命令注入。 */

import GithubSlugger from "github-slugger";

export interface Heading {
  level: number;
  text: string;
  id: string;
}

/** 提取 1~6 级标题（跳过围栏代码块）；TOC 展示时再按需过滤层级。 */
export function extractHeadings(markdown: string): Heading[] {
  const slugger = new GithubSlugger();
  const out: Heading[] = [];
  let inCodeFence = false;
  for (const line of markdown.split(/\r?\n/)) {
    if (/^\s*(\u0060{3}|~~~)/.test(line)) {
      inCodeFence = !inCodeFence;
      continue;
    }
    if (inCodeFence) continue;
    const m = line.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (!m) continue;
    // 剥内联标记（加粗/斜体/行内码/链接方括号），与 rehype-slug 取 hast 文本节点一致
    const text = m[2].replace(/[*_\u0060~[\]]/g, "").trim();
    out.push({ level: m[1].length, text, id: slugger.slug(text) });
  }
  return out;
}
