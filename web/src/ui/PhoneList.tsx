// Under 768 px: factory > machine > health as a plain list. No three.js is loaded.
import type { Meta, State } from "../api";
import { useFrame } from "../api";
import { inr, mechanicLine, STATUS_WORD } from "../theme";
import { HealthRing } from "./bits";

export function PhoneList({
  state,
  meta,
  onPick,
}: {
  state: State;
  meta: Meta;
  onPick: (id: string) => void;
}) {
  const frame = useFrame();
  return (
    <div className="phone">
      {meta.factories.map((f) => (
        <section key={f.id}>
          <h2 className="row between">
            <span>
              <span className="mono">{f.id}</span> {f.name} <small className="muted">{f.area}</small>
            </span>
            <a
              className="report-link small"
              href={`/api/report/${f.id}.pdf`}
              target="_blank"
              rel="noreferrer"
            >
              Savings PDF
            </a>
          </h2>
          <ul>
            {state.machines
              .filter((m) => m.factory === f.id)
              .map((m) => (
                <li key={m.id}>
                  <button
                    className={`mrow ${m.status === "alarm" || m.status === "early_match" ? "hot" : ""}`}
                    onClick={() => onPick(m.id)}
                  >
                    <HealthRing m={m} />
                    <span className="mmain">
                      <span className="row between">
                        <b>{m.name}</b>
                        <span className="mono small">{STATUS_WORD[m.status]}</span>
                      </span>
                      <span className="small muted">{mechanicLine(m, meta.factories)}</span>
                      {m.rupees_at_risk > 0 && (
                        <span className="small accent mono">{inr(m.rupees_at_risk)} at risk in 2 days</span>
                      )}
                    </span>
                    <span className="mono small num">
                      {m.risk3?.toFixed(2) ?? "--"}
                      <br />
                      <span className="muted">
                        {frame[m.id] ? `${frame[m.id].risk.toFixed(2)} live` : ""}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
