import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import type { FileList, FileRead } from "../types";
import { fmtBytes, parseCsv } from "../util";
import { ErrorBox, Loading } from "./ui";

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

export function CsvTable({ text }: { text: string }) {
  const rows = useMemo(() => parseCsv(text), [text]);
  const [sort, setSort] = useState<{ col: number; desc: boolean } | null>(null);
  const [head, ...body] = rows;
  const sorted = useMemo(() => {
    if (!sort) return body;
    const num = (s: string) => (s !== "" && !isNaN(Number(s)) ? Number(s) : null);
    return [...body].sort((a, b) => {
      const x = a[sort.col] ?? "";
      const y = b[sort.col] ?? "";
      const nx = num(x);
      const ny = num(y);
      const c = nx !== null && ny !== null ? nx - ny : x.localeCompare(y);
      return sort.desc ? -c : c;
    });
  }, [body, sort]);
  if (!head) return <div className="muted">empty</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {head.map((h, i) => (
              <th key={i} className="sortable" onClick={() => setSort({ col: i, desc: sort?.col === i ? !sort.desc : false })}>
                {h}
                {sort?.col === i ? (sort.desc ? " ▾" : " ▴") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r, i) => (
            <tr key={i}>
              {r.map((c, j) => (
                <td key={j} className={c !== "" && !isNaN(Number(c)) ? "num mono" : "mono"}>
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function FileView({ path, tail, refetchMs }: { path: string; tail?: number; refetchMs?: number }) {
  const q = useQuery({
    queryKey: ["file", path, tail],
    queryFn: () => api.get<FileRead>("/files", { path, tail }),
    refetchInterval: refetchMs,
  });
  if (q.isLoading) return <Loading what={path} />;
  if (q.error) return <ErrorBox error={q.error} />;
  const f = q.data!;
  const ext = path.slice(path.lastIndexOf(".") + 1).toLowerCase();
  return (
    <div>
      <div className="muted small mono">
        {f.path} · {fmtBytes(f.size)}
        {f.truncated ? " · truncated preview" : ""}
      </div>
      {ext === "csv" ? <CsvTable text={f.text} /> : ext === "md" ? <Markdown text={f.text} /> : <pre className="logbox">{f.text || "(empty)"}</pre>}
    </div>
  );
}

/** Lists the previewable files below `dir` and shows the selected one. */
export function FileBrowser({ dir, depth }: { dir: string; depth?: number }) {
  const [sel, setSel] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["files", dir, depth], queryFn: () => api.get<FileList>("/files/list", { path: dir, depth: depth ?? 2 }) });
  if (q.isLoading) return <Loading what="files" />;
  if (q.error) return <ErrorBox error={q.error} />;
  const files = q.data!.files;
  if (!files.length) return <div className="muted">no files</div>;
  return (
    <div>
      <div className="chips" style={{ marginBottom: 8 }}>
        {files.map((f) =>
          f.preview ? (
            <button key={f.path} className={`chip ${sel === f.path ? "active" : ""}`} onClick={() => setSel(sel === f.path ? null : f.path)}>
              <span className="mono">{f.path}</span>
              <span className="muted small">{fmtBytes(f.size)}</span>
            </button>
          ) : (
            <span key={f.path} className="chip muted" title="binary: not previewed">
              <span className="mono">{f.path}</span>
              <span className="small">{fmtBytes(f.size)}</span>
            </span>
          ),
        )}
      </div>
      {q.data!.truncated && <div className="notice">listing truncated</div>}
      {sel && <FileView path={`${q.data!.root}/${sel}`} />}
    </div>
  );
}
