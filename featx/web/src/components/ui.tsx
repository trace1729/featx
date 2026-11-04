import { useEffect, useRef, useState, type ReactNode } from "react";
import { copyText, fmtDuration } from "../util";
import { useI18n } from "../i18n";

export function Badge({ kind, title, children }: { kind?: string; title?: string; children: ReactNode }) {
  return <span className={`badge ${kind ?? ""}`} title={title}>{children}</span>;
}

export function StateBadge({ state }: { state: string }) {
  const { t } = useI18n();
  return <Badge kind={state}>{t(state)}</Badge>;
}

const VERDICT_MARK: Record<string, string> = { supported: "✔", refuted: "✘", inconclusive: "?", open: "·" };

export function VerdictMark({ verdict }: { verdict: string }) {
  const { t } = useI18n();
  return (
    <span className={`verdict ${verdict}`} title={t("verdict: {verdict}", { verdict: t(verdict) })}>
      {VERDICT_MARK[verdict] ?? "·"}
    </span>
  );
}

/** A page title that shows only the short name; clicking copies the full path or ref. */
export function CopyTitle({ text, children }: { text: string; children: ReactNode }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  const copy = async () => {
    if (!(await copyText(text))) return;
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1200);
  };
  return (
    <h1 style={{ margin: 0 }} className="mono copyable" title={t("click to copy {text}", { text })} onClick={copy}>
      {children}
      {copied && <span className="muted small"> {t("copied")}</span>}
    </h1>
  );
}

export function Progress({ pct, label }: { pct: number | null | undefined; label?: string }) {
  const p = Math.max(0, Math.min(100, pct ?? 0));
  return (
    <div className="progress" title={label}>
      <div style={{ width: `${p}%` }} />
      <span>{pct === null || pct === undefined ? label ?? "" : `${p.toFixed(0)}%${label ? ` · ${label}` : ""}`}</span>
    </div>
  );
}

export function Eta({ seconds }: { seconds: number | null | undefined }) {
  const { t } = useI18n();
  if (seconds === 0) return <span className="muted">{t("done")}</span>;
  return <span title={t("estimated time remaining")}>{fmtDuration(seconds)}</span>;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="error">{error instanceof Error ? error.message : String(error)}</div>;
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: readonly T[]; value: T; onChange: (t: T) => void }) {
  const { t: translate } = useI18n();
  return (
    <div className="tabs">
      {tabs.map((tab) => (
        <button key={tab} className={tab === value ? "active" : ""} onClick={() => onChange(tab)}>
          {translate(tab)}
        </button>
      ))}
    </div>
  );
}

export function Dialog({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: ReactNode }) {
  const { t } = useI18n();
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal?.();
    if (!open && el.open) el.close?.();
  }, [open]);
  if (!open) return null;
  return (
    <dialog ref={ref} onClose={onClose} onCancel={onClose}>
      <h2>{t(title)}</h2>
      {children}
    </dialog>
  );
}

/** Run an async action from a button; keeps the busy flag and the last error. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e);
      return undefined;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run, clear: () => setError(null) };
}

export function Loading({ what }: { what?: string }) {
  const { t } = useI18n();
  return <div className="muted">{t("Loading")} {what ? t(what) : ""}…</div>;
}

export function Json({ value }: { value: unknown }) {
  return <pre className="mono">{JSON.stringify(value, null, 2)}</pre>;
}
