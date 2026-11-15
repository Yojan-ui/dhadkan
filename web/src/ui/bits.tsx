// Small 2D pieces shared by the desktop overlay and phone mode.
import { useEffect, useRef } from "react";
import { HISTORY, store, type Machine } from "../api";
import { C, fill, ringCSS } from "../theme";

/** Same ring as on the 3D floor: fills clockwise toward this machine's alarm level. */
export function HealthRing({ m, size = 34 }: { m: Machine; size?: number }) {
  const r = size / 2 - 3,
    c = 2 * Math.PI * r,
    f = fill(m);
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      aria-label={`${Math.round(f * 100)}% of alarm level`}
    >
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={C.track} strokeWidth={3} />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={ringCSS(m)}
        strokeWidth={3}
        strokeDasharray={`${f * c} ${c}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        strokeLinecap="butt"
      />
    </svg>
  );
}

/** Risk per reading over the last 7 simulated days, with the alarm line at 1.0. */
export function Trend({
  m,
  tickHours,
  w = 300,
  h = 64,
}: {
  m: Machine;
  tickHours: number;
  w?: number;
  h?: number;
}) {
  const n = Math.round((7 * 24) / tickHours);
  const pts = m.history.slice(-n);
  if (pts.length < 2) return <div className="muted small">Trend appears after two readings.</div>;
  const ymax = Math.max(1.4, ...pts.map((p) => p.risk));
  const x = (i: number) => 4 + (i / (n - 1)) * (w - 8);
  const y = (v: number) => h - 4 - (v / ymax) * (h - 10);
  const off = n - pts.length;
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i + off).toFixed(1)},${y(p.risk).toFixed(1)}`).join("");
  const days = (pts.length * tickHours) / 24;
  return (
    <figure className="trend">
      <svg
        width="100%"
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        role="img"
        aria-label="risk per reading, last 7 days"
      >
        <line
          x1={0}
          x2={w}
          y1={y(1)}
          y2={y(1)}
          stroke={C.accent}
          strokeDasharray="3 3"
          strokeWidth={1}
          opacity={0.7}
        />
        <path d={d} fill="none" stroke={C.ink} strokeWidth={1.4} />
        {pts.map(
          (p, i) => p.match && <circle key={i} cx={x(i + off)} cy={y(p.risk)} r={2.4} fill={C.accent} />,
        )}
      </svg>
      <figcaption className="small muted">
        <span>
          7 days · {pts.length} readings ({days.toFixed(1)} d recorded)
        </span>
        <span className="mono">dashed = alarm level · dots = registry match</span>
      </figcaption>
    </figure>
  );
}

/** Flat scrolling spectrogram (phone mode stand-in for the 3D waterfall). */
export function Spectro2D({ m, height = 110 }: { m: Machine; height?: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(
    () =>
      store.subscribe(() => {
        const cv = ref.current;
        if (!cv) return;
        const g = cv.getContext("2d");
        if (!g) return;
        const rows = store.spec[m.id] ?? [];
        const W = HISTORY,
          H = 64;
        const img = g.createImageData(W, H);
        const hot = (m.band_z ?? []).map((z) => z > 3);
        for (let t = 0; t < W; t++) {
          const col = rows[rows.length - W + t];
          for (let f = 0; f < H; f++) {
            const v = col ? col[f] : 0;
            const i = ((H - 1 - f) * W + t) * 4;
            const [r, gg, b] = hot[f]
              ? [37 + v * 218, 36 + v * 70, 35]
              : [37 + v * 160, 39 + v * 158, 43 + v * 160];
            img.data[i] = r;
            img.data[i + 1] = gg;
            img.data[i + 2] = b;
            img.data[i + 3] = 255;
          }
        }
        g.putImageData(img, 0, 0);
      }),
    [m.id, m.band_z],
  );
  return (
    <canvas
      ref={ref}
      width={HISTORY}
      height={64}
      className="spectro"
      style={{ height }}
      aria-label="live spectrogram"
    />
  );
}

export function Sound({ id }: { id: string }) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const play = (kind: "live" | "normal") => {
    audio.current?.pause();
    const a = new Audio(`/api/audio/${id}/${kind}.wav?t=${Date.now()}`);
    audio.current = a;
    a.play();
  };
  useEffect(() => () => audio.current?.pause(), [id]);
  return (
    <div className="row gap">
      <button className="btn" onClick={() => play("live")}>
        Play live sound
      </button>
      <button className="btn ghost" onClick={() => play("normal")}>
        Play normal reference
      </button>
      <button className="btn ghost" aria-label="stop" onClick={() => audio.current?.pause()}>
        Stop
      </button>
    </div>
  );
}
