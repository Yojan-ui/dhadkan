// Keyboard shortcuts: one listener, and the list shown by "?".
import { useEffect, useRef } from "react";

type Drawer = "operator" | "eval" | "log";
export interface Keys {
  back: () => void;
  help: () => void;
  drawer: (d: Drawer) => void;
  cluster: () => void;
  shed: (i: number) => void;
  machine: (dir: 1 | -1) => void;
  step?: (n: number) => void;
  play?: () => void;
  live?: () => void;
  replay: () => void;
}

export const KEYS: [string, string][] = [
  ["?", "show these keys"],
  ["Esc", "back one level / close a drawer"],
  ["0", "whole cluster"],
  ["1 2 3", "fly into shed A, B or C"],
  ["[ ]", "previous / next machine in the shed"],
  ["← →", "time machine: one reading back / forward"],
  ["Shift + ← →", "time machine: one day back / forward"],
  ["Space", "play / pause the 14 days"],
  ["N", "back to now (live)"],
  ["D", "demo replay on / off (restarts from empty)"],
  ["O  E  L", "operator, evaluation, decision log"],
];

export function useShortcuts(k: Keys) {
  const ref = useRef(k);
  useEffect(() => {
    ref.current = k;
  });
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) {
        if (e.key === "Escape") t.blur(); // the slider and text fields keep their own keys
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const c = ref.current;
      const map: Record<string, (() => void) | undefined> = {
        "?": c.help,
        Escape: c.back,
        "0": c.cluster,
        "1": () => c.shed(0),
        "2": () => c.shed(1),
        "3": () => c.shed(2),
        "[": () => c.machine(-1),
        "]": () => c.machine(1),
        ArrowLeft: c.step && (() => c.step?.(e.shiftKey ? -3 : -1)),
        ArrowRight: c.step && (() => c.step?.(e.shiftKey ? 3 : 1)),
        " ": c.play,
        n: c.live,
        d: c.replay,
        o: () => c.drawer("operator"),
        e: () => c.drawer("eval"),
        l: () => c.drawer("log"),
      };
      const fn = map[e.key.length === 1 ? e.key.toLowerCase() : e.key] ?? map[e.key];
      if (!fn) return;
      e.preventDefault();
      fn();
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);
}

export function Shortcuts() {
  return (
    <table className="kv">
      <tbody>
        {KEYS.map(([k, what]) => (
          <tr key={k}>
            <th>
              <kbd className="mono">{k}</kbd>
            </th>
            <td>{what}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
