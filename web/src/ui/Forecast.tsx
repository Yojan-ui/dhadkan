// Days to fault level: the estimate in words, and the last 7 days with the projected band.
import type { Forecast as F, HistoryRow, Machine } from "../api";
import { C, inr } from "../theme";

export function forecastWords(f: F | null | undefined): string {
  if (!f) return "No readings yet.";
  switch (f.status) {
    case "estimate":
      return f.high == null
        ? `Likely more than ${f.low} days before it sounds like its recorded fault.`
        : `Likely ${f.low}–${f.high} days before it sounds like its recorded fault.`;
    case "at_fault_level":
      return "Already sounds like its recorded faulty clips.";
    case "no_trend":
      return `No steady rise over the last ${f.window_days} days, so no countdown.`;
    case "too_few":
      return "Too few readings for a trend yet.";
    case "not_separable":
      return `No countdown for this machine: its recorded faults score ${f.level.toFixed(2)}× its alarm level, below the alarm, so the model can't tell how close it is.`;
  }
}

export function ForecastBlock({ m, rows, tickHours }: { m: Machine; rows: HistoryRow[]; tickHours: number }) {
  const f = m.forecast;
  const perDay = 24 / tickHours;
  const past = rows.slice(-Math.round(7 * perDay));
  const band = f?.status === "estimate" ? (f.band ?? []) : [];
  const futureDays = band.length ? band[band.length - 1].day : 0;
  const pastDays = past.length > 1 ? ((past[past.length - 1].tick - past[0].tick) * tickHours) / 24 : 0;
  const span = Math.max(1, pastDays + futureDays);
  const W = 320,
    H = 92;
  const level = f?.level ?? 1.3;
  const ymax = Math.max(1.5, level * 1.15, ...past.map((r) => r.risk), ...band.map((b) => b.hi));
  const x = (d: number) => 4 + ((d + pastDays) / span) * (W - 8); // d: days from now (negative = past)
  const y = (v: number) => H - 4 - (v / ymax) * (H - 10);
  const now = past.length ? past[past.length - 1].tick : 0;
  const dOf = (r: HistoryRow) => ((r.tick - now) * tickHours) / 24;
  const line = past.map((r, i) => `${i ? "L" : "M"}${x(dOf(r)).toFixed(1)},${y(r.risk).toFixed(1)}`).join("");
  const area = band.length
    ? `M${band.map((b) => `${x(b.day).toFixed(1)},${y(b.hi).toFixed(1)}`).join("L")}L${[...band]
        .reverse()
        .map((b) => `${x(b.day).toFixed(1)},${y(b.lo).toFixed(1)}`)
        .join("L")}Z`
    : "";
  return (
    <section className="fc">
      <div className="row between small">
        <b>Days to fault level</b>
        {m.rupees_at_risk > 0 && (
          <span className="mono accent">{inr(m.rupees_at_risk)} at risk in 2 days</span>
        )}
      </div>
      <p className={`fc-words ${f?.status === "estimate" || f?.status === "at_fault_level" ? "hot" : ""}`}>
        {forecastWords(f)}
      </p>
      {past.length > 1 && (
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          role="img"
          aria-label="last 7 days and the projected range"
        >
          <line x1={0} x2={W} y1={y(1)} y2={y(1)} stroke={C.accent} strokeDasharray="3 3" opacity={0.6} />
          <line x1={0} x2={W} y1={y(level)} y2={y(level)} stroke={C.muted} strokeDasharray="1 3" />
          <line x1={x(0)} x2={x(0)} y1={0} y2={H} stroke={C.rule} />
          {area && <path d={area} fill={C.accent} opacity={0.18} />}
          {band.length > 0 && (
            <path
              d={band.map((b, i) => `${i ? "L" : "M"}${x(b.day).toFixed(1)},${y(b.fit).toFixed(1)}`).join("")}
              stroke={C.accent}
              fill="none"
              strokeDasharray="4 2"
            />
          )}
          <path d={line} fill="none" stroke={C.ink} strokeWidth={1.3} />
          {past.map(
            (r, i) => r.match && <circle key={i} cx={x(dOf(r))} cy={y(r.risk)} r={2.2} fill={C.accent} />,
          )}
        </svg>
      )}
      <p className="small muted">
        Straight line through the last {f?.window_days ?? 6} days of readings, to the level this unit&rsquo;s{" "}
        {f?.fault_level_clips ?? 20} recorded faulty clips reach (dotted). Shaded = 80% of trend fits. An
        estimate from the trend, not a guarantee; rupees at risk use assumed costs.
      </p>
    </section>
  );
}
