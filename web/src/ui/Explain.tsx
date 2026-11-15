// Why it is flagged, by sound: the band that changed, by how much, and a 3 s before/now pair.
import { useState } from "react";
import type { Explanation } from "../api";
import { fmtClock } from "../theme";
import type { Loaded } from "./hooks";
import { playClip } from "./hooks";

const hz = (f?: number) => (f == null ? "" : f >= 1000 ? `${(f / 1000).toFixed(1)} kHz` : `${f} Hz`);

export function ExplainCard({ id, ex }: { id: string; ex: Loaded<Explanation> }) {
  const [err, setErr] = useState<string | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  if (ex.loading && !ex.data) return <div className="skel" style={{ height: 96 }} aria-label="loading" />;
  if (ex.error && !ex.data)
    return <p className="small muted">Couldn&rsquo;t work out what changed: {ex.error}.</p>;
  const e = ex.data;
  if (!e) return null;
  const play = (which: "before" | "now", then?: () => void) => {
    setErr(null);
    setPlaying(which);
    playClip(
      `/api/explain/${id}/${which}.wav`,
      (m) => {
        setErr(m);
        setPlaying(null);
      },
      () => {
        setPlaying(null);
        then?.();
      },
    );
  };
  return (
    <section className="why" aria-label="why it is flagged">
      <b className="small">What changed in the sound</b>
      {e.status === "ok" ? (
        <>
          <p className="why-head">
            {e.word} ({hz(e.lo_hz)}–{hz(e.hi_hz)}){" "}
            <span className="accent">
              {(e.change_pct ?? 0) >= 0 ? "up" : "down"} {Math.abs(e.change_pct ?? 0)}%
            </span>{" "}
            since {e.before ? fmtClock(e.before.ts) : "its first reading"}
          </p>
          <p className="small">
            {e.hint} <span className="muted">(rule of thumb, not checked on this data)</span>
          </p>
          {e.owner_said && (
            <p className="small muted">
              Owner&rsquo;s word for it: &ldquo;{e.owner_said}&rdquo;
              {e.fault ? ` → ${e.fault.replace("_", " ")}` : ""}
            </p>
          )}
          <div className="row gap">
            <button className="btn" onClick={() => play("before")} aria-pressed={playing === "before"}>
              Before · 3 s
            </button>
            <button className="btn" onClick={() => play("now")} aria-pressed={playing === "now"}>
              Now · 3 s
            </button>
            <button className="btn ghost" onClick={() => play("before", () => play("now"))}>
              Play both
            </button>
          </div>
          {err && <p className="small accent">{err}</p>}
        </>
      ) : e.status === "no_clear_band" ? (
        <p className="small muted">No single band stands out; the change is spread across the whole sound.</p>
      ) : (
        <p className="small muted">There is no earlier normal reading of this machine to compare with yet.</p>
      )}
    </section>
  );
}
