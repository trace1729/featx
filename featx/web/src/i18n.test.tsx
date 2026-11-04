import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { I18nProvider, LanguageToggle, M, interpolate, useI18n } from "./i18n";

afterEach(() => { cleanup(); localStorage.clear(); });
const placeholders = (text: string) => [...text.matchAll(/\{([A-Za-z_]\w*)\}/g)].map((m) => m[1]).sort();

it("keeps all placeholders in Chinese templates", () => {
  for (const [key, value] of Object.entries(M)) expect(placeholders(value), key).toEqual(placeholders(key));
  expect(interpolate("{name}: {count} {unknown}", { name: "$USER", count: 0 })).toBe("$USER: 0 {unknown}");
});

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((f) => f.isDirectory() ? files(join(dir, f.name)) : [join(dir, f.name)]);
}

it("has Chinese entries for literal translation keys across the UI", () => {
  const missing = new Set<string>();
  const dynamic = new Set(["TABS", "TYPES", "STATUSES", "VERDICTS", "STATE_CHIPS", "JOB_STATES", "STATE_ORDER", "KINDS", "AGAINST_HINT", "known"]);
  const rawAllowed = new Set(["featx", "· tick", "st", "stdout", "EN", "CLI: featx help", "seq", "cat", "chiseldb", "chiselmap", "W", "I", "spec-score", "featx submit", "· featx recipe set", "-f FILE", "featx worktree create", "worktree", "· featx node update", "--notes …", "ETA", "rc", "post/", "HEAD", "bin", "checkpoint", "trace", "exec", "compare/collect-cache:"]);
  const inspect = (node: ts.Node) => {
    if (ts.isJsxText(node) && /[a-z]/i.test(node.text) && !rawAllowed.has(node.text.trim())) missing.add(`untranslated JSX: ${node.text.trim()}`);
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && dynamic.has(node.name.text) && node.initializer) {
      const visit = (n: ts.Node) => {
        // Object properties hold labels in their values; their keys are API identifiers.
        if (ts.isPropertyAssignment(n)) { visit(n.initializer); return; }
        if (ts.isStringLiteral(n) && !(n.text in M)) missing.add(n.text);
        ts.forEachChild(n, visit);
      };
      visit(node.initializer);
    }
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && ["t", "tr", "translate"].includes(node.expression.text)) {
      const check = (arg: ts.Node | undefined) => {
        if (!arg) return;
        if (ts.isStringLiteral(arg) && /[a-z]/i.test(arg.text) && !arg.text.startsWith("/")) {
          if (!(arg.text in M)) missing.add(arg.text);
        } else if (ts.isConditionalExpression(arg)) { check(arg.whenTrue); check(arg.whenFalse); }
      };
      check(node.arguments[0]);
    }
    ts.forEachChild(node, inspect);
  };
  for (const file of files(join(process.cwd(), "src")).filter((p) => /\.tsx?$/.test(p) && !p.includes(".test."))) {
    inspect(ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true));
  }
  expect([...missing].sort()).toEqual([]);
});

it("preserves language preference, English behavior and unknown-template fallback", () => {
  function Probe() {
    const { t } = useI18n();
    return <><LanguageToggle /><p>{t("Overrides")}</p><p>{t("unknown {name}", { name: "user input" })}</p></>;
  }
  localStorage.setItem("featx.language", "zh");
  render(<I18nProvider><Probe /></I18nProvider>);
  expect(screen.getByText("覆盖配置")).toBeTruthy();
  expect(screen.getByText("unknown user input")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "EN" }));
  expect(screen.getByText("Overrides")).toBeTruthy();
  expect(localStorage.getItem("featx.language")).toBe("en");
});
