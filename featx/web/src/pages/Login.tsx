import { useEffect, useState } from "react";
import { probeToken, setToken } from "../api";
import { ErrorBox, useAction } from "../components/ui";

export function LoginPage() {
  const [value, setValue] = useState("");
  const [tokenFile, setTokenFile] = useState("~/.config/featx/token");
  useEffect(() => {
    // /health is public: it tells where this featxd keeps its token
    fetch("/health")
      .then((r) => (r.ok ? r.json() : null))
      .then((h) => h?.token_file && setTokenFile(h.token_file))
      .catch(() => undefined);
  }, []);
  const act = useAction();
  const submit = () =>
    act.run(async () => {
      if (!(await probeToken(value))) throw new Error("featxd rejected this token");
      setToken(value);
    });
  return (
    <div className="login card">
      <h1>featx</h1>
      <p className="muted">
        Paste the featxd token. On the daemon host: <code>cat {tokenFile}</code>
      </p>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <input type="password" autoFocus placeholder="token" style={{ flex: 1 }} value={value} onChange={(e) => setValue(e.target.value)} />
        <button className="primary" disabled={!value.trim() || act.busy}>
          Log in
        </button>
      </form>
      <ErrorBox error={act.error} />
    </div>
  );
}
