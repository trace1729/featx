import { yaml } from "@codemirror/lang-yaml";
import CodeMirror from "@uiw/react-codemirror";

const dark = () => typeof window !== "undefined" && !!window.matchMedia?.("(prefers-color-scheme: dark)").matches;

export function YamlEditor({ value, onChange, readOnly, height }: {
  value: string;
  onChange?: (text: string) => void;
  readOnly?: boolean;
  height?: string;
}) {
  return (
    <CodeMirror
      value={value}
      height={height ?? "320px"}
      theme={dark() ? "dark" : "light"}
      extensions={[yaml()]}
      editable={!readOnly}
      basicSetup={{ foldGutter: false, highlightActiveLine: !readOnly }}
      onChange={onChange}
    />
  );
}
