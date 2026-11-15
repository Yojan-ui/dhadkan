// Scrub back through a machine's last 14 days. The 3D machine, waterfall and numbers follow.
import type { History } from "../api";
import { C, fmtClock } from "../theme";

export function TimeMachine({
  hist,
  idx,
  playing,
  onIdx,
  onPlay,
  onLive,
}: {
  hist: History;
  idx: number | null; // null = live
  playing: boolean;
  onIdx: (i: number) => void;
  onPlay: () => void;
  onLive: () => void;
}) {
  const rows = hist.rows;
  if (rows.length < 2) return <p className="small muted">The time machine opens after two readings.</p>;
  const n = rows.length;
  const at = idx ?? n - 1;
  const W = 320,
    H = 76;
  const ymax = Math.max(1.15, hist.fault_level.level * 1.08, ...rows.map((r) => r.risk3));
  const ymin = Math.min(0.5, ...rows.map((r) => r.risk3)) - 0.05;
  const x = (i: number) => 2 + (i / (n - 1)) * (W - 4);
  const y = (v: number) => H - 3 - ((v - ymin) / (ymax - ymin)) * (H - 14);
  const path = rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r.risk3).toFixed(1)}`).join("");
  const iw = hist.first_warning ? rows.findIndex((r) => r.tick === hist.first_warning?.tick) : -1;
  const ib = hist.fault_sound ? rows.findIndex((r) => r.tick === hist.fault_sound?.tick) : -1;
  const day0 = rows[0].tick;
  const dayOf = (i: number) => Math.floor(((rows[i].tick - day0) * hist.tick_hours) / 24) + 1;
  const row = rows[at];
  const days = Math.round(((rows[n - 1].tick - day0) * hist.tick_hours) / 24);

  return (
    <section className="tm" aria-label="time machine">
      <div className="row between small">
        <span>
          <b>Time machine</b> <span className="muted">· last {days} days</span>
        </span>
        <span className="mono">
          {idx === null ? (
            <span className="accent">live</span>
          ) : (
            <>
              day {dayOf(at)} · {fmtClock(row.ts)}
            </>
          )}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-label="risk over the last days, with markers"
      >
        <line x1={0} x2={W} y1={y(1)} y2={y(1)} stroke={C.accent} strokeDasharray="3 3" opacity={0.6} />
        <line
          x1={0}
          x2={W}
          y1={y(hist.fault_level.level)}
          y2={y(hist.fault_level.level)}
          stroke={C.muted}
          strokeDasharray="1 3"
        />
        <path d={path} fill="none" stroke={C.ink} strokeWidth={1.3} />
        {iw >= 0 && (
          <g>
            <line x1={x(iw)} x2={x(iw)} y1={0} y2={H} stroke={C.accent} strokeWidth={1.5} />
            <text x={x(iw) + 3} y={9} className="tm-mark" fill={C.accent}>
              first warning
            </text>
          </g>
        )}
        {ib >= 0 && (
          <g>
            <line
              x1={x(ib)}
              x2={x(ib)}
              y1={0}
              y2={H}
              stroke={C.ink}
              strokeWidth={1.5}
              strokeDasharray="2 2"
            />
            <text x={x(ib) - 3} y={H - 4} className="tm-mark" fill={C.ink} textAnchor="end">
              breaks down
            </text>
          </g>
        )}
        <circle cx={x(at)} cy={y(row.risk3)} r={3.5} fill={idx === null ? C.accent : C.ink} />
      </svg>
      <input
        type="range"
        min={0}
        max={n - 1}
        value={at}
        aria-label="reading"
        aria-valuetext={`day ${dayOf(at)}, ${fmtClock(row.ts)}`}
        onChange={(e) => onIdx(Number(e.target.value))}
        className="tm-range"
      />
      <div className="row gap small">
        <button className="btn ghost" onClick={onPlay} aria-pressed={playing}>
          {playing ? "Pause" : "Play 14 days"}
        </button>
        <button className="btn ghost" onClick={onLive} disabled={idx === null}>
          Back to live
        </button>
        <span className="muted">
          dashed orange = alarm level · dotted = recorded-fault level
          {iw >= 0 && ib >= 0 && (
            <>
              {" "}
              · warned{" "}
              <b className="ink">
                {(((rows[ib].tick - rows[iw].tick) * hist.tick_hours) / 24).toFixed(1)} days
              </b>{" "}
              before the fault sound
            </>
          )}
        </span>
      </div>
      {ib >= 0 && (
        <p className="small muted tm-note">
          &ldquo;Breaks down&rdquo; = the first reading that is the recorded faulty machine itself (simulated
          breakdown). Before it, the growing fault is a SYNTHETIC mix of faulty into healthy recordings.
        </p>
      )}
    </section>
  );
}
