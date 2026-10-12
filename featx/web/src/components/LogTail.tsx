import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ErrorBox } from "./ui";

interface LogResp {
  path: string;
  exists: boolean;
  tail: string;
}

export function LogTail({ task, job, live, initialStream }: { task: string; job?: string; live: boolean; initialStream?: "out" | "err" }) {
  const [stream, setStream] = useState<"out" | "err">(initialStream ?? "out");
  const [lines, setLines] = useState(200);
  const [follow, setFollow] = useState(true);
  const box = useRef<HTMLPreElement>(null);
  const q = useQuery({
    queryKey: ["log", task, job, stream, lines],
    queryFn: () => api.get<LogResp>(`/tasks/${task}/log`, { job, stream, lines }),
    refetchInterval: live && follow ? 2000 : false,
  });
  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [q.data, follow]);
  return (
    <div>
      <div className="row" style={{ marginBottom: 6 }}>
        <select value={stream} onChange={(e) => setStream(e.target.value as "out" | "err")}>
          <option value="out">stdout</option>
          <option value="err">stderr (PERF lines hidden)</option>
        </select>
        <select value={lines} onChange={(e) => setLines(Number(e.target.value))}>
          {[80, 200, 1000, 5000].map((n) => (
            <option key={n} value={n}>
              last {n} lines
            </option>
          ))}
        </select>
        <label>
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow
        </label>
        <span className="muted small mono">{q.data?.path}</span>
      </div>
      <ErrorBox error={q.error} />
      <pre className="logbox" ref={box}>
        {q.data ? (q.data.exists ? q.data.tail || "(empty)" : "(no log file yet)") : "…"}
      </pre>
    </div>
  );
}
