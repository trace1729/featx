"""Small runtime language layer for user-facing CLI text.

Machine-readable output and recipe keys stay unchanged. Set FEATX_LANG=zh (or use
``featx --lang zh``) to translate help and common status messages.
"""

from __future__ import annotations

import os

ZH = {
    "featx CLI": "featx 命令行工具",
    "Feature-tree nodes (category/feature/subfeature).": "功能树节点（分类/功能/子功能）。",
    "build/run/post recipes (inherited down the tree).": "build/run/post 配方（沿功能树继承）。",
    "Per-node git worktrees.": "每个节点的 git worktree。",
    "Scheduler tasks.": "调度任务。",
    "featxd control.": "featxd 控制。",
    "Named baselines (reference result roots).": "命名基线（参考结果根目录）。",
    "Binary cache.": "二进制缓存。",
    "problem-analysis.log entries.": "problem-analysis.log 记录。",
    "featx.yaml (user layer over the package defaults).": "featx.yaml（覆盖包默认值的用户配置层）。",
    "Post-processing plugins (manifests).": "后处理插件（manifest）。",
    "SimPoint checkpoint sets for exec runs (run.checkpoint).": "exec 运行使用的 SimPoint checkpoint 集合（run.checkpoint）。",
    "ok": "正常", "warn": "警告", "fail": "失败", "created": "已创建", "adopted": "已接管",
    "next:": "下一步：", "(no tasks)": "（没有任务）", "(no active tasks; -a for all)": "（没有活动任务；使用 -a 查看全部）",
    "no post summaries yet": "还没有后处理摘要", "no complete runset in this class (use -a to list incomplete ones)": "此类别没有已完成的 runset（使用 -a 查看未完成项）",
    "unchanged": "未修改", "sealed": "已封存", "open": "开放", "running": "运行中", "queued": "排队中",
}


def language() -> str:
    return "zh" if os.environ.get("FEATX_LANG", "en").lower().split("-")[0] == "zh" else "en"


def tr(text: str) -> str:
    return ZH.get(text, text) if language() == "zh" else text
