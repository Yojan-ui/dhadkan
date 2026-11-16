// HUD panels around the Digital Twin. Every number comes from /api/twin or the live frame stream.
import { useEffect, useRef } from "react";
import { post, store, type Machine, type State } from "../api";
import { P, statusColour } from "./palette";

export interface Twin {
  clock: string | null;
  chips: { ok: number; learning: number; watch: number; alert: number; total: number };
  shifts: { shift: string; mean: number; max: number; readings: number }[];
  ranking: { id: string; name: string; factory: string; risk: number; status: string }[];
  focus: string | null;
  focus_name: string | null;
  trend: number[];
  rupees_at_risk_2d: number;
  health_pct: number | null;
  health_rule: string;
  checklist: { step: string; done: boolean }[];
  agent_run: string | null;
}

function Panel({
  title,
  note,
  className,
  children,
}: {
  title: string;
  note?: string;
  className: string;
  children: React.ReactNode;
}) {
  return (
    <section className={`hud ${className}`} aria-label={title}>
      <header>
        <span>{title}</span>
        {note && <small>{note}</small>}
      </header>
      {children}
    </section>
  );
}

export function Chips({ t, state }: { t: Twin | null; state: State }) {
  const c = t?.chips;
  const ist = t?.clock
    ? new Date(t.clock).toLocaleString("en-IN", {
        weekday: "short",
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "no readings yet";
  return (
    <Panel title="Status" className="hud-tl">
      <div className="chips">
        <span className="chip">
          <i style={{ background: P.teal }} />
          OK {c ? c.ok + c.learning : "–"}
        </span>
        <span className="chip">
          <i style={{ background: P.amber }} />
          Watch {c?.watch ?? "–"}
        </span>
        <span className="chip">
          <i style={{ background: P.alert }} />
          Alert {c?.alert ?? "–"}
        </span>
        <span className="chip">
          <i style={{ background: state.link.up ? P.cyan : P.muted }} />
          {state.link.up ? "Online" : `Offline (${state.link.pending} queued)`}
        </span>
      </div>
      <div className="mono dim">
        {ist} IST · factory clock{state.demo.on ? " · Demo mode (NASA IMS)" : ""}
      </div>
    </Panel>
  );
}

export function ShiftChart({ t }: { t: Twin | null }) {
  const s = t?.shifts ?? [];
  const W = 520,
    H = 120,
    top = Math.min(3, Math.max(1.5, ...s.map((x) => x.max)));
  const bw = s.length ? (W - 20) / s.length : 0;
  const y = (v: number) => H - 16 - (Math.min(v, top) / top) * (H - 28);
  return (
    <Panel
      title="Anomaly score per shift"
      note="bars = mean, line = max · 1.0 = alarm · axis clipped at 3×"
      className="hud-tc"
    >
      {s.length === 0 ? (
        <p className="dim">No readings yet.</p>
      ) : (
        <svg viewBox={`0 0 ${W} ${H}`} className="chart">
          <line
            x1={10}
            x2={W - 10}
            y1={y(1)}
            y2={y(1)}
            stroke={P.alert}
            strokeDasharray="4 4"
            strokeWidth={1}
            opacity={0.7}
          />
          {s.map((x, i) => (
            <g key={x.shift}>
              <rect
                x={14 + i * bw}
                y={y(x.mean)}
                width={bw * 0.55}
                height={H - 16 - y(x.mean)}
                fill={P.teal}
                opacity={0.85}
              />
              <text x={14 + i * bw + bw * 0.27} y={H - 3} textAnchor="middle" className="ax">
                {x.shift.slice(8, 10)}
                {x.shift.slice(-1)}
              </text>
            </g>
          ))}
          <polyline
            fill="none"
            stroke={P.cyan}
            strokeWidth={2}
            points={s.map((x, i) => `${14 + i * bw + bw * 0.27},${y(x.max)}`).join(" ")}
          />
          {s.map((x, i) => (
            <circle key={x.shift} cx={14 + i * bw + bw * 0.27} cy={y(x.max)} r={2.6} fill={P.cyan} />
          ))}
        </svg>
      )}
    </Panel>
  );
}

/** Score vs alarm level: a percentage, or "339×" once it is far past the alarm. */
export const riskText = (r: number) => (r >= 10 ? `${Math.round(r)}×` : `${Math.round(r * 100)}%`);

export function RiskList({ t, onPick }: { t: Twin | null; onPick: (id: string) => void }) {
  const rows = (t?.ranking ?? []).slice(0, 8);
  const top = Math.max(1.2, ...rows.map((r) => r.risk));
  return (
    <Panel title="Machines by risk" note="score / own alarm level" className="hud-tr">
      {rows.length === 0 && <p className="dim">No readings yet.</p>}
      <ul className="bars">
        {rows.map((r) => (
          <li key={r.id} onClick={() => onPick(r.id)}>
            <span className="nm">
              {r.name} <small>{r.factory}</small>
            </span>
            <span className="bar">
              <i
                style={{
                  width: `${Math.min(100, (r.risk / top) * 100)}%`,
                  background: statusColour(r.status),
                }}
              />
            </span>
            <span className="mono">{riskText(r.risk)}</span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

export function Spectrum({ m }: { m: Machine | null }) {
  const ref = useRef<SVGSVGElement>(null);
  useEffect(() => {
    const draw = () => {
      const el = ref.current;
      if (!el || !m) return;
      const spec = store.frame[m.id]?.spec;
      if (!spec) return;
      const rects = el.querySelectorAll("rect");
      spec.forEach((v, i) => {
        const r = rects[i];
        if (!r) return;
        const h = Math.max(1, v * 150);
        r.setAttribute("y", String(160 - h));
        r.setAttribute("height", String(h));
      });
    };
    draw();
    return store.subscribe(draw);
  }, [m]);
  return (
    <Panel title="Spectrum" note={m ? `${m.name} · 64 mel bands, live` : "no machine"} className="hud-l">
      <svg ref={ref} viewBox="0 0 192 160" className="spec">
        {Array.from({ length: 64 }, (_, i) => (
          <rect key={i} x={i * 3} y={159} width={2.2} height={1} fill={i % 8 === 0 ? P.cyan : P.teal} />
        ))}
      </svg>
    </Panel>
  );
}

const SEG: Record<string, string> = {
  "0": "abcdef",
  "1": "bc",
  "2": "abged",
  "3": "abgcd",
  "4": "fgbc",
  "5": "afgcd",
  "6": "afgedc",
  "7": "abc",
  "8": "abcdefg",
  "9": "abfgcd",
  "-": "g",
};
function SevenSeg({ text, h = 44 }: { text: string; h?: number }) {
  const w = h * 0.56;
  const t = h * 0.12;
  const seg = (k: string, x0: number): React.ReactNode => {
    const H = h / 2;
    const p: Record<string, [number, number, number, number]> = {
      a: [x0 + t, 0, w - 2 * t, t],
      g: [x0 + t, H - t / 2, w - 2 * t, t],
      d: [x0 + t, h - t, w - 2 * t, t],
      f: [x0, t, t, H - 1.5 * t],
      b: [x0 + w - t, t, t, H - 1.5 * t],
      e: [x0, H + t / 2, t, H - 1.5 * t],
      c: [x0 + w - t, H + t / 2, t, H - 1.5 * t],
    };
    const [x, y, ww, hh] = p[k];
    return <rect key={`${x0}${k}`} x={x} y={y} width={ww} height={hh} rx={t / 3} />;
  };
  let x = 0;
  const parts: React.ReactNode[] = [];
  for (const ch of text) {
    if (ch === "," || ch === ".") {
      parts.push(<rect key={`c${x}`} x={x} y={h - t} width={t} height={t} />);
      x += t * 2;
      continue;
    }
    for (const k of SEG[ch] ?? "") parts.push(seg(k, x));
    x += w + t * 1.4;
  }
  return (
    <svg viewBox={`0 0 ${x} ${h}`} height={h} className="seven" aria-label={text}>
      {parts}
    </svg>
  );
}

export function TrendReadout({ t }: { t: Twin | null }) {
  const tr = t?.trend ?? [];
  const W = 240,
    H = 80,
    top = Math.max(1.5, ...tr);
  const pts = tr
    .map((v, i) => `${(i / Math.max(1, tr.length - 1)) * W},${H - (Math.min(v, top) / top) * (H - 6)}`)
    .join(" ");
  const rupees = Math.round(t?.rupees_at_risk_2d ?? 0).toLocaleString("en-IN");
  return (
    <Panel
      title="₹ at risk, next 2 days"
      note="all machines · costs are the factory's assumptions"
      className="hud-r"
    >
      <div className="readout">
        <span className="rs">₹</span>
        <SevenSeg text={rupees} />
      </div>
      <div className="dim small">
        Trend: {t?.focus_name ?? "–"} (last {tr.length} readings)
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="trend">
        <line
          x1={0}
          x2={W}
          y1={H - (1 / top) * (H - 6)}
          y2={H - (1 / top) * (H - 6)}
          stroke={P.alert}
          strokeDasharray="3 4"
          opacity={0.6}
        />
        {tr.length > 1 && <polyline points={pts} fill="none" stroke={P.teal} strokeWidth={2} />}
      </svg>
    </Panel>
  );
}

export function SpectroStrip({ m }: { m: Machine | null }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    if (m) void store.prefill(m.id);
  }, [m]);
  useEffect(() => {
    const draw = () => {
      const cv = ref.current;
      const g = cv?.getContext("2d");
      if (!cv || !g || !m) return;
      const cols = store.spec[m.id] ?? [];
      g.fillStyle = P.bg0;
      g.fillRect(0, 0, cv.width, cv.height);
      const cw = cv.width / Math.max(60, cols.length);
      cols.forEach((col, x) =>
        col.forEach((v, b) => {
          const a = Math.max(0, Math.min(1, v));
          g.fillStyle = `rgba(${Math.round(47 + 208 * a * a)}, ${Math.round(80 + 132 * a)}, ${Math.round(110 + 53 * a)}, ${0.15 + 0.85 * a})`;
          g.fillRect(
            x * cw,
            cv.height - ((b + 1) * cv.height) / col.length,
            cw + 0.5,
            cv.height / col.length + 0.5,
          );
        }),
      );
    };
    draw();
    return store.subscribe(draw);
  }, [m]);
  return (
    <Panel
      title="Spectrogram"
      note={m ? `${m.name} · last 30 s · time →, pitch ↑` : "no machine"}
      className="hud-bl"
    >
      <canvas ref={ref} width={640} height={110} className="strip" />
    </Panel>
  );
}

export function Gauge({ t }: { t: Twin | null }) {
  const v = t?.health_pct ?? 0;
  const R = 62,
    cx = 80,
    cy = 78;
  const ang = Math.PI * (1 - v / 100);
  const arc = (a0: number, a1: number) =>
    `M ${cx + R * Math.cos(a0)} ${cy - R * Math.sin(a0)} A ${R} ${R} 0 0 1 ${cx + R * Math.cos(a1)} ${cy - R * Math.sin(a1)}`;
  return (
    <Panel title="Overall health" note={t?.health_rule} className="hud-bc">
      <svg viewBox="0 0 160 96" className="gauge">
        <path d={arc(Math.PI, 0)} stroke={P.dark} strokeWidth={12} fill="none" />
        {t?.health_pct != null && (
          <path
            d={arc(Math.PI, ang)}
            stroke={v >= 80 ? P.teal : v >= 50 ? P.amber : P.alert}
            strokeWidth={12}
            fill="none"
          />
        )}
        <text x={cx} y={cy - 6} textAnchor="middle" className="gv">
          {t?.health_pct ?? "–"}%
        </text>
      </svg>
    </Panel>
  );
}

export function Actions({ t, state }: { t: Twin | null; state: State }) {
  const toggle = (label: string, on: boolean, act: () => Promise<unknown>) => (
    <button className={`tog ${on ? "on" : ""}`} onClick={() => void act()}>
      <i />
      {label}
    </button>
  );
  return (
    <Panel
      title="Actions"
      note={t?.agent_run ? `agent run ${t.agent_run}` : "agent: no run yet"}
      className="hud-br"
    >
      <div className="togs">
        {toggle("Demo mode (NASA IMS)", state.demo.on, () =>
          post("/api/demo", { on: !state.demo.on, lang: "hi_latn" }),
        )}
        {toggle("Scripted replay", state.replay.on, () => post("/api/replay", { on: !state.replay.on }))}
        {toggle("Shed signal", state.link.up, () => post("/api/link", { up: !state.link.up }))}
      </div>
      <ol className="check">
        {(t?.checklist ?? []).map((c) => (
          <li key={c.step} className={c.done ? "done" : ""}>
            <i />
            {c.step}
          </li>
        ))}
      </ol>
    </Panel>
  );
}
