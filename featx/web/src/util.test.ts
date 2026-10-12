import { describe, expect, it } from "vitest";
import type { TreeRow } from "./types";
import { buildTree, countsText, fmtBytes, fmtCount, fmtDuration, fmtValue, metricLabel, parseCsv, parseOverrides, splitRunset } from "./util";

const row = (path: string): TreeRow => ({
  id: path,
  path,
  name: path.split("/").pop()!,
  depth: path.split("/").length - 1,
  kind: "feature",
  verdict: "open",
  status: "active",
  hypothesis: "",
  worktree: false,
  runsets: 0,
  latest: null,
  tasks: {},
  active: [],
  eta_s: null,
});

describe("buildTree", () => {
  it("nests rows by path regardless of order", () => {
    const roots = buildTree([row("wpe/overlay/smoke"), row("wpe"), row("dev"), row("wpe/overlay"), row("wpe/real")]);
    expect(roots.map((r) => r.row.path)).toEqual(["dev", "wpe"]);
    const wpe = roots[1];
    expect(wpe.children.map((c) => c.row.path)).toEqual(["wpe/overlay", "wpe/real"]);
    expect(wpe.children[0].children[0].row.name).toBe("smoke");
  });
  it("orphans (filtered parents) become roots", () => {
    expect(buildTree([row("a/b/c"), row("x")]).map((r) => r.row.path)).toEqual(["a/b/c", "x"]);
  });
  it("does not treat a name prefix as a parent", () => {
    const roots = buildTree([row("ab"), row("a"), row("a/c")]);
    expect(roots.map((r) => r.row.path)).toEqual(["a", "ab"]);
  });
});

describe("formatting", () => {
  it("durations", () => {
    expect(fmtDuration(null)).toBe("?");
    expect(fmtDuration(45)).toBe("45s");
    expect(fmtDuration(3 * 3600 + 120)).toBe("3h02m");
  });
  it("counts and bytes", () => {
    expect(fmtCount(20_000_000)).toBe("20M");
    expect(fmtCount(1500)).toBe("1500");
    expect(fmtCount(1_234_567)).toBe("1.23M");
    expect(fmtCount(45_678.9)).toBe("45.7k");
    expect(fmtBytes(1024 * 1024 * 3)).toBe("3.0M");
  });
  it("metric values and labels", () => {
    expect(fmtValue(47.30233379850922)).toBe("47.302");
    expect(fmtValue(0.8249982)).toBe("0.825");
    expect(fmtValue("x")).toBe("x");
    expect(metricLabel("pk.self.mean_abs_err_pct")).toContain("err");
    expect(countsText({ succeeded: 2, running: 1 })).toBe("1 running · 2 succeeded");
    expect(splitRunset("wpe/overlay/smoke/20261012-2")).toEqual({ node: "wpe/overlay/smoke", name: "20261012-2" });
  });
});

describe("parseCsv", () => {
  it("handles quotes, embedded commas and CRLF", () => {
    expect(parseCsv('a,b\r\n"x,y","he said ""hi"""\n1,\n')).toEqual([
      ["a", "b"],
      ["x,y", 'he said "hi"'],
      ["1", ""],
    ]);
  });
});

describe("parseOverrides", () => {
  it("builds nested recipe overrides with typed values", () => {
    expect(parseOverrides("run.W=20000000\nrun.I=2e3x\nbuild.args.debug=true\npost.steps=[pk, cmp]\n")).toEqual({
      run: { W: 20000000, I: "2e3x" },
      build: { args: { debug: true } },
      post: { steps: ["pk", "cmp"] },
    });
  });
  it("rejects lines without a kind or =", () => {
    expect(() => parseOverrides("W=1")).toThrow(/build\., run\. or post\./);
    expect(() => parseOverrides("run.W")).toThrow(/bad override/);
  });
});
