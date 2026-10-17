import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

export type Language = "en" | "zh";

const M: Record<string, string> = {
  Leaderboard: "排行榜", Tasks: "任务", Events: "事件", Storage: "存储", Settings: "设置", Docs: "文档",
  "log out": "退出登录", "daemon unreachable": "daemon 无法连接", running: "运行中", queued: "排队中", failed: "失败",
  "cores free": "空闲核心", "no such page": "页面不存在", "Paste the featxd token.": "请输入 featxd token。",
  "Log in": "登录", token: "token", "No runset has a post-processing summary yet.": "还没有包含后处理摘要的 runset。",
  metric: "指标", class: "类别", "best per node": "每个节点取最佳", "show incomplete": "显示未完成任务",
  "lowest first ▴": "最低优先 ▴", "highest first ▾": "最高优先 ▾", runset: "runset", points: "点数", purpose: "目的", binary: "二进制", created: "创建时间",
  "no events": "没有事件", "include acknowledged": "包含已确认事件", "Acknowledge all shown": "确认显示的全部事件", ack: "确认",
  Capacity: "容量", "run cores": "运行核心", "build cores": "构建核心", Set: "设置", "show finished": "显示已完成", Active: "活动任务", Finished: "已完成", Hosts: "主机",
  "filter by node / runset / task id": "按节点 / runset / 任务 ID 筛选", "filter nodes": "筛选节点", "no nodes yet: add a category": "还没有节点：请添加分类",
  "add a category": "添加分类", "Loading…": "加载中…", "Dry run": "试运行", Delete: "删除", Cancel: "取消", Save: "保存", Revert: "还原",
  "Import": "导入", "Copying…": "复制中…", "Checking…": "检查中…", Rescan: "重新扫描", Scanning: "扫描中…", Create: "创建", "Creating…": "创建中…",
  "Move": "移动", "Moving…": "移动中…", Remove: "删除", "Removing…": "删除中…", "Add baseline": "添加基线", "remove": "移除", "remove anyway": "仍然移除",
  "Delete these…": "删除这些…", "Delete permanently?": "永久删除？", "empty": "空", Nodes: "节点", "Binary cache": "二进制缓存", Trash: "回收站", "Garbage collection": "垃圾回收",
  "featx.yaml": "featx.yaml", Doctor: "Doctor", Baselines: "基线", "Checkpoint sets": "Checkpoint 集合", "Import a binary into the cache": "导入二进制到缓存",
  "Add a category": "添加分类", path: "路径", hypothesis: "假设", worktree: "worktree", "name (a/b creates both)": "名称（a/b 会同时创建）",
  "what this node is meant to show": "描述该节点要验证的内容", "create a git worktree and branch now (takes a while)": "立即创建 git worktree 和分支（需要一些时间）",
  "new path": "新路径", options: "选项", "create missing parents": "创建缺失的父节点", "enter a different path": "请输入不同的路径",
  "checking…": "检查中…", "type the path to confirm": "输入完整路径以确认", "also delete the worktrees": "同时删除 worktree",
  "Loading": "加载中", "done": "完成", "estimated time remaining": "预计剩余时间",
  stages: "阶段", build: "构建", run: "运行", post: "后处理", workload: "工作负载", checkpoint: "checkpoint", coverage: "覆盖率",
  priority: "优先级", "post steps": "后处理步骤", overrides: "覆盖参数", Submit: "提交", "reset form": "重置表单",
  "what this runset is meant to show": "描述该 runset 要验证的内容", "optional subset, comma or space separated": "可选子集，用逗号或空格分隔",
  "optional subset of the post recipe's steps": "可选的后处理步骤子集", "optional: bincache key, runset ref, or /abs/path/emu": "可选：bincache key、runset 引用或 /abs/path/emu",
  "this task has no per-point jobs.": "该任务没有按点划分的 job。", job: "job", state: "状态", host: "主机", attempt: "尝试次数", progress: "进度", failure: "失败原因",
  "Relaunch failed": "重新运行失败任务", "Resubmit…": "重新提交…", "Append an entry": "追加记录", problem: "问题", solution: "解决方案", code: "代码", Append: "追加",
  "what failed and the evidence for the suspected cause": "描述失败内容以及支持怀疑原因的证据", "what was changed": "描述修改内容", "how the code change solves it (optional)": "描述代码修改如何解决问题（可选）",
  runsets: "运行集合", submit: "提交", recipes: "配方", scripts: "脚本", verdict: "结论", evidence: "证据", comment: "备注",
  "Add evidence": "添加证据", "Save note": "保存 note", "verdict note": "结论 note", "your thoughts on this node: ideas, doubts, what to try next": "记录你对该节点的想法、疑问和下一步计划",
  edit: "编辑", "edit this entry": "编辑此条目", "remove this entry": "移除此条目", "runset ref or note (Ctrl+Enter adds)": "runset 引用或 note（Ctrl+Enter 添加）",
  "Add child…": "添加子节点…", "Move…": "移动…", "Remove…": "移除…", Children: "子节点", "Save notes": "保存 notes",
};

function initialLanguage(): Language {
  const saved = localStorage.getItem("featx.language");
  if (saved === "zh" || saved === "en") return saved;
  return "en";
}

type I18nValue = { language: Language; setLanguage: (language: Language) => void; t: (text: string) => string };
const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(initialLanguage);
  const setLanguage = (next: Language) => {
    setLanguageState(next);
    localStorage.setItem("featx.language", next);
  };
  const value = useMemo(() => ({ language, setLanguage, t: (text: string) => language === "zh" ? (M[text] ?? text) : text }), [language]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n must be used below I18nProvider");
  return value;
}

export function LanguageToggle() {
  const { language, setLanguage } = useI18n();
  return (
    <span className="language-toggle" role="group" aria-label="language">
      <button className={`link ${language === "en" ? "active" : ""}`} onClick={() => setLanguage("en")}>EN</button>
      <span className="muted">/</span>
      <button className={`link ${language === "zh" ? "active" : ""}`} onClick={() => setLanguage("zh")}>中文</button>
    </span>
  );
}
