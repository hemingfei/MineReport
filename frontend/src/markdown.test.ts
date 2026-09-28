import { describe, expect, it } from "vitest";
import GithubSlugger from "github-slugger";
import { extractHeadings } from "./markdown";

describe("extractHeadings", () => {
  it("提取各级标题并保留层级", () => {
    const md = ["# 一级", "", "## 二级", "### 三级", "#### 四级", "##### 五级"].join("\n");
    const headings = extractHeadings(md);
    expect(headings.map((h) => h.level)).toEqual([1, 2, 3, 4, 5]);
    expect(headings.map((h) => h.text)).toEqual(["一级", "二级", "三级", "四级", "五级"]);
  });

  it("跳过围栏代码块内的井号行", () => {
    const md = ["## 真", "", "\u0060\u0060\u0060python", "# 这是代码注释", "\u0060\u0060\u0060", "", "## 也真"].join("\n");
    const headings = extractHeadings(md);
    expect(headings.map((h) => h.text)).toEqual(["真", "也真"]);
  });

  it("剥内联标记后生成与 rehype-slug 一致的 id", () => {
    const md = "## **AI 算力** 深度研究";
    const [heading] = extractHeadings(md);
    expect(heading.text).toBe("AI 算力 深度研究");
    // rehype-slug 对同一标题文本（hast 文本节点拼接）生成的 id
    const slugger = new GithubSlugger();
    expect(heading.id).toBe(slugger.slug("AI 算力 深度研究"));
  });

  it("重复标题文本生成去重 id（-2 后缀）", () => {
    const md = "## 风险提示\n\n## 风险提示";
    const [first, second] = extractHeadings(md);
    expect(first.id).not.toBe(second.id);
    expect(second.id.startsWith(first.id)).toBe(true);
  });

  it("非标题行与尾部井号装饰不误判", () => {
    const headings = extractHeadings("普通文本带 # 井号\n## 标题 ###");
    expect(headings).toHaveLength(1);
    expect(headings[0].text).toBe("标题");
  });

  it("空文档与无标题返回空数组", () => {
    expect(extractHeadings("")).toEqual([]);
    expect(extractHeadings("只有段落\n没有标题")).toEqual([]);
  });
});
