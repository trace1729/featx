"""docs/reference.md as help topics (CLI `featx help`, MCP `docs`, UI Docs page).

Every `## <topic> — <title>` heading of the reference is a topic. The plugin section is
generated from the manifests (between the plugins markers) so it can't drift:
`python -m featx.docs` rewrites it; a test fails when it is stale. At runtime the `plugins`
topic is rendered live, so plugins added under $FEATX_ROOT/plugins show up too.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from . import config

REFERENCE = config.REPO_DIR / "docs" / "reference.md"
BEGIN, END = "<!-- plugins:begin (generated: python -m featx.docs) -->", "<!-- plugins:end -->"
HEAD_RE = re.compile(r"^## (\S+) — (.+)$", re.M)


def text() -> str:
    return REFERENCE.read_text()


def topics(src: str | None = None) -> dict[str, tuple[str, str]]:
    """topic -> (title, markdown body including its heading)."""
    src = src if src is not None else text()
    heads = list(HEAD_RE.finditer(src))
    out = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(src)
        out[m.group(1)] = (m.group(2).strip(), src[m.start():end].rstrip() + "\n")
    return out


def index() -> dict:
    return {"path": str(REFERENCE),
            "topics": [{"topic": k, "title": t} for k, (t, _) in topics().items()]}


def topic(name: str) -> str:
    if name in ("all", "reference"):
        return _with_live_plugins(text())
    found = topics()
    if name not in found:
        from .post import plugins
        if name in plugins.available():
            return plugin_markdown({name: plugins.get(name)}, level=2)
        raise KeyError(f"no help topic {name!r}; topics: {', '.join(found)} (or a plugin name)")
    body = found[name][1]
    return _with_live_plugins(body) if name == "plugins" else body


def _with_live_plugins(src: str) -> str:
    """The plugin section rendered from the installed manifests, without the markers
    (a Markdown viewer would show the HTML comments as text)."""
    from .post import plugins
    out = splice(src, plugin_markdown(plugins.available()))
    return out.replace(BEGIN + "\n", "").replace(END + "\n", "").replace(END, "")


def splice(src: str, generated: str) -> str:
    i, j = src.find(BEGIN), src.find(END)
    if i < 0 or j < 0:
        return src
    return src[: i + len(BEGIN)] + "\n" + generated.rstrip() + "\n" + src[j:]


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def plugin_markdown(manifests: dict[str, dict], level: int = 3) -> str:
    """Parameters (default + meaning), dependencies and outputs of each plugin manifest."""
    h = "#" * level
    parts = []
    for name, man in sorted(manifests.items()):
        lines = [f"{h} {name}", "", " ".join(str(man.get("description") or "").split()), ""]
        after = man.get("after") or []
        if after:
            lines += [f"Runs after: {', '.join(f'`{a}`' for a in after)} (when that step is in the recipe).", ""]
        params, docs = man.get("with") or {}, man.get("params") or {}
        if params:
            lines += ["| param | default | meaning |", "|---|---|---|"]
            for key, default in params.items():
                shown = f"`{_cell(default)}`" if default not in ("", None) else ('`""`' if default == "" else "`null`")
                lines.append(f"| `{key}` | {shown} | {_cell(docs.get(key, ''))} |")
            lines.append("")
        outputs = man.get("outputs") or {}
        if outputs:
            lines.append("Outputs (in `${out}` = `<runset>/post/<step>/`):")
            lines.append("")
            lines += [f"- `{k}`: {_cell(v)}" for k, v in outputs.items()]
            lines.append("")
        if man.get("_path") and not str(man["_path"]).startswith(str(config.PKG_DIR)):
            lines += [f"Manifest: `{man['_path']}`", ""]
        parts.append("\n".join(lines))
    return "\n".join(parts)


def builtin_plugins() -> dict[str, dict]:
    import yaml
    from .post.plugins import MANIFEST_DIR
    out = {}
    for p in sorted(MANIFEST_DIR.glob("*.yaml")):
        data = yaml.safe_load(p.read_text()) or {}
        out[data.get("name", p.stem)] = data
    return out


def regenerate() -> bool:
    src = text()
    new = splice(src, plugin_markdown(builtin_plugins()))
    if new != src:
        REFERENCE.write_text(new)
        return True
    return False


if __name__ == "__main__":
    print("updated" if regenerate() else "up to date", REFERENCE, file=sys.stderr)
