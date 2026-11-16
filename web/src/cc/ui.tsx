// Small building blocks for the command centre: panels, icons, status dots and SVG charts (no chart library).
import type { ReactNode } from "react";

export function Panel({
  title,
  right,
  children,
  className = "",
}: {
  title: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`ccp ${className}`}>
      <header className="ccp-h">
        <span>{title}</span>
        {right}
      </header>
      <div className="ccp-b">{children}</div>
    </section>
  );
}

export function Dot({ cls }: { cls: string }) {
  return <span className={`ccdot ${cls}`} aria-hidden />;
}

const P: Record<string, string> = {
  overview: "M3 3h7v7H3zM14 3h7v4h-7zM14 10h7v11h-7zM3 13h7v8H3z",
  twin: "M12 2l9 5v10l-9 5-9-5V7zM12 2v20M3 7l9 5 9-5",
  machines: "M4 18h16M6 18V9l6-4 6 4v9M10 18v-5h4v5",
  telemetry: "M3 12h4l3-8 4 16 3-8h4",
  predict: "M3 20l6-6 4 4 8-10M15 8h6v6",
  agents: "M12 3a4 4 0 110 8 4 4 0 010-8zM4 21c0-4 4-6 8-6s8 2 8 6M19 4l2 2-2 2",
  jobs: "M4 7h16v13H4zM9 7V4h6v3M4 12h16",
  techs: "M14 7l3-3 3 3-3 3M17 4l-9 9M7 14l-4 6 6-4 3-3",
  parts:
    "M12 2l2.5 2.5L18 4l-.5 3.5L20 10l-2.5 2.5L18 16l-3.5-.5L12 18l-2.5-2.5L6 16l.5-3.5L4 10l2.5-2.5L6 4l3.5.5zM12 8a2 2 0 110 4 2 2 0 010-4z",
  analytics: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  events: "M12 3a9 9 0 110 18 9 9 0 010-18zM12 7v5l3 3",
  settings:
    "M12 8a4 4 0 110 8 4 4 0 010-8zM3 12h3M18 12h3M12 3v3M12 18v3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1",
  bell: "M6 16V11a6 6 0 1112 0v5l2 2H4zM10 20a2 2 0 004 0",
  user: "M12 3a4 4 0 110 8 4 4 0 010-8zM4 21c0-4 4-6 8-6s8 2 8 6",
  link: "M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1",
};
export function Icon({ name, size = 18 }: { name: keyof typeof P | string; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d={P[name] ?? ""} />
    </svg>
  );
}

/** Line chart for one or more series over the same x (index); optional horizontal reference and markers. */
export function Lines({
  series,
  height = 120,
  refY,
  refLabel,
  yMin,
  yMax,
  markers = [],
  band,
}: {
  series: { values: (number | null)[]; cls: string; label?: string }[];
  height?: number;
  refY?: number;
  refLabel?: string;
  yMin?: number;
  yMax?: number;
  markers?: { i: number; label: string }[];
  band?: { from: number; lo: number; hi: number };
}) {
  const W = 600;
  const H = height;
  const pad = { l: 30, r: 8, t: 8, b: 16 };
  const all = series.flatMap((s) => s.values.filter((v): v is number => v != null));
  const lo = yMin ?? Math.min(0, ...all);
  const hi = yMax ?? Math.max(refY ?? 1, ...all, 1);
  const n = Math.max(2, ...series.map((s) => s.values.length));
  const x = (i: number) => pad.l + (i / (n - 1)) * (W - pad.l - pad.r);
  const y = (v: number) =>
    pad.t + (1 - (Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo || 1)) * (H - pad.t - pad.b);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="ccchart" preserveAspectRatio="none">
      {[lo, (lo + hi) / 2, hi].map((v) => (
        <g key={v}>
          <line x1={pad.l} x2={W - pad.r} y1={y(v)} y2={y(v)} className="grid" />
          <text x={2} y={y(v) + 3} className="ax">
            {v.toFixed(1)}
          </text>
        </g>
      ))}
      {band && (
        <rect
          x={x(band.from)}
          width={W - pad.r - x(band.from)}
          y={y(band.hi)}
          height={Math.max(1, y(band.lo) - y(band.hi))}
          className="band"
        />
      )}
      {refY != null && (
        <>
          <line x1={pad.l} x2={W - pad.r} y1={y(refY)} y2={y(refY)} className="ref" />
          {refLabel && (
            <text x={W - pad.r - 4} y={y(refY) - 3} className="ax" textAnchor="end">
              {refLabel}
            </text>
          )}
        </>
      )}
      {markers.map((m) => (
        <g key={`${m.i}${m.label}`}>
          <line x1={x(m.i)} x2={x(m.i)} y1={pad.t} y2={H - pad.b} className="mark" />
          <text x={x(m.i) + 3} y={pad.t + 9} className="ax">
            {m.label}
          </text>
        </g>
      ))}
      {series.map((s, k) => {
        let d = "";
        let pen = false;
        s.values.forEach((v, i) => {
          if (v == null) {
            pen = false;
            return;
          }
          d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
          pen = true;
        });
        return <path key={k} d={d} className={`ln ${s.cls}`} />;
      })}
    </svg>
  );
}

/** Horizontal bars, 0..max. */
export function Bars({
  rows,
  max = 1,
  fmt = (v: number) => `${Math.round(v * 100)}%`,
}: {
  rows: { label: string; value: number; cls?: string; note?: string }[];
  max?: number;
  fmt?: (v: number) => string;
}) {
  return (
    <div className="ccbars">
      {rows.map((r) => (
        <div key={r.label} className="ccbar">
          <span className="lb mono">{r.label}</span>
          <span className="tr">
            <span
              className={`fl ${r.cls ?? ""}`}
              style={{ width: `${Math.min(100, (r.value / (max || 1)) * 100)}%` }}
            />
          </span>
          <span className="vl mono">{fmt(r.value)}</span>
        </div>
      ))}
    </div>
  );
}
