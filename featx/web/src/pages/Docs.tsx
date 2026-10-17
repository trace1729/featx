import { useQuery } from "@tanstack/react-query";
import { NavLink, useParams } from "react-router-dom";
import { api } from "../api";
import { Markdown } from "../components/FileViewer";
import { useI18n } from "../i18n";
import { ErrorBox, Loading } from "../components/ui";
import type { DocsIndex, DocsTopic } from "../types";

/** docs/reference.md by topic (the same text as `featx help <topic>` and the MCP docs tool). */
export function DocsPage() {
  const { t } = useI18n();
  const topic = useParams()["*"] || "concepts";
  const index = useQuery({ queryKey: ["docs"], queryFn: () => api.get<DocsIndex>("/help"), staleTime: 300000 });
  const body = useQuery({
    queryKey: ["docs", topic],
    queryFn: () => api.get<DocsTopic>(`/help/${encodeURIComponent(topic)}`),
    staleTime: 300000,
  });
  return (
    <div className="docs">
      <div className="docs-index">
        <h1>{t("Docs")}</h1>
        {index.error && <ErrorBox error={index.error} />}
        {(index.data?.topics ?? []).map((t) => (
          <NavLink key={t.topic} to={`/docs/${t.topic}`} title={t.title}>
            {t.topic}
          </NavLink>
        ))}
        <NavLink to="/docs/all">all</NavLink>
        {index.data && <div className="muted small">{index.data.path}</div>}
        <div className="muted small">CLI: featx help {topic}</div>
      </div>
      <div className="docs-body">
        {body.isLoading ? <Loading what={`docs: ${topic}`} /> : body.error ? <ErrorBox error={body.error} /> : <Markdown text={body.data!.text} />}
      </div>
    </div>
  );
}
