import { useEffect } from "react";
// What we know about one machine: plain words first, then the readings behind them.
import type { Factory, Machine, Meta } from "../api";
import { store, useFrame } from "../api";
import { changedBands, mechanicLine, SHAPE_WORD, sourceWord, STATUS_WORD, pct } from "../theme";
import { HealthRing, Sound, Spectro2D, Trend } from "./bits";

export function MachinePanel({
  m,
  meta,
  tickHours,
  onClose,
  flat,
}: {
  m: Machine;
  meta: Meta;
  tickHours: number;
  onClose: () => void;
  flat?: boolean;
}) {
  const f = meta.factories.find((x) => x.id === m.factory) as Factory;
  const frame = useFrame()[m.id];
  useEffect(() => {
    store.prefill(m.id);
  }, [m.id]);
  const bands = changedBands(m.band_z, meta.mel_hz);
  const ev = meta.eval?.types[m.type];
  const head = ev?.conditions[ev.headline];
  const unit = ev?.per_unit[m.source];
  const l = m.latest;
  return (
    <aside className="panel" aria-label={`${m.name} details`}>
      <header className="row between">
        <div className="row gap">
          <HealthRing m={m} size={40} />
          <div>
            <div className="title">
              {m.name} <span className="muted mono small">{m.id}</span>
            </div>
            <div className="small muted">
              {f.name} · {f.area} · {SHAPE_WORD[m.shape]}
            </div>
          </div>
        </div>
        <button className="btn ghost" onClick={onClose} aria-label="close">
          Back
        </button>
      </header>

      <p className={`say ${m.status === "alarm" || m.status === "early_match" ? "hot" : ""}`}>
        {mechanicLine(m, meta.factories)}
      </p>

      {flat && <Spectro2D m={m} />}
      {!flat && (
        <div className="small muted">
          The surface beside the machine is this reading's sound, live: left = low pitch, right = high, height
          = level vs normal.
        </div>
      )}
      <div className="small">
        {bands.length ? (
          <>
            Changed from its normal: <b className="accent mono">{bands.join(", ")}</b>
          </>
        ) : (
          <span className="muted">No band more than 3 std from its normal.</span>
        )}
      </div>

      <Sound id={m.id} />
      <Trend m={m} tickHours={tickHours} />

      <table className="kv mono">
        <tbody>
          <tr>
            <th>status</th>
            <td>{STATUS_WORD[m.status]}</td>
          </tr>
          <tr>
            <th>last 3 readings</th>
            <td>
              {pct(m.risk3)} <span className="muted">of its alarm level</span>
            </td>
          </tr>
          <tr>
            <th>this reading</th>
            <td>
              {l ? `${pct(l.risk)} (score ${l.score.toFixed(3)}, alarm ${l.threshold.toFixed(3)})` : "--"}
            </td>
          </tr>
          <tr>
            <th>this second</th>
            <td>{frame ? `${pct(frame.risk)} · ${frame.energy.toFixed(0)} dBFS` : "--"}</td>
          </tr>
          <tr>
            <th>registry</th>
            <td>
              {l?.match_cos != null && l.match_level != null
                ? `cos ${l.match_cos.toFixed(2)} vs flag ${l.match_level.toFixed(2)}`
                : "no fingerprint of this type"}
            </td>
          </tr>
          {m.known_fault && (
            <tr>
              <th>fault</th>
              <td>
                {m.known_fault}
                {m.registry?.owner_label ? ` ("${m.registry.owner_label}")` : ""}
              </td>
            </tr>
          )}
          <tr>
            <th>mode</th>
            <td>
              {m.mode}
              {m.mode === "onset" ? ` ${m.mix.toFixed(1)} SYNTHETIC` : ""}
            </td>
          </tr>
          <tr>
            <th>clip</th>
            <td className="clip">{l?.clip ?? "--"}</td>
          </tr>
        </tbody>
      </table>
      <div className="small muted">
        Recording: {sourceWord(m)}.{" "}
        {head && (
          <>
            The {m.type} model scored AUC <span className="mono">{head.auc.toFixed(3)}</span>, pAUC{" "}
            <span className="mono">{head.pauc.toFixed(3)}</span> on held-out clips
            {unit ? (
              <>
                {" "}
                (this unit <span className="mono">{unit.auc.toFixed(3)}</span>)
              </>
            ) : null}
            , run <span className="mono">{meta.eval?.run_id}</span>.
          </>
        )}
      </div>
    </aside>
  );
}
