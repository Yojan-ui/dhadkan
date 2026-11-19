// Side drawers: Operator (demo controls), Evaluation (measured scores, including weak ones),
// Log (every agent tool call and event).
import { useState } from "react";
import { m as anim } from "framer-motion";
import { post, type Meta, type State } from "../api";
import { fmtT } from "../theme";
import { alertWord } from "./Diagnosis";

export function Drawer({
  title,
  onClose,
  children,
  reduced,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  reduced: boolean;
}) {
  return (
    <anim.section
      className="drawer"
      role="dialog"
      aria-label={title}
      initial={reduced ? false : { x: 40, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
    >
      <header className="row between">
        <h2>{title}</h2>
        <button className="btn ghost" onClick={onClose}>
          Close
        </button>
      </header>
      <div className="drawer-body">{children}</div>
    </anim.section>
  );
}

export function Operator({ state, meta }: { state: State; meta: Meta }) {
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [other, setOther] = useState(state.machines[0]?.id ?? "");
  const run =
    (name: string, path: string, body: unknown = {}) =>
    async () => {
      setErr(null);
      setBusy(name);
      try {
        await post(path, body);
      } catch (e) {
        setErr((e as Error).message);
      } finally {
        setBusy(null);
      }
    };
  const lock = state.replay.on || busy !== null;
  const lab = state.pending_label;
  const B = (p: {
    name: string;
    path: string;
    body?: unknown;
    disabled?: boolean;
    children: React.ReactNode;
  }) => (
    <button className="btn wide" disabled={lock || p.disabled} onClick={run(p.name, p.path, p.body)}>
      {busy === p.name ? "working..." : p.children}
    </button>
  );
  return (
    <div className="ops">
      {state.replay.on && (
        <p className="small accent">
          Demo replay is running ({fmtT(state.replay.t)}, {state.replay.phase}). Switch it off to use these.
        </p>
      )}
      <div className="row gap">
        <B name="step" path="/api/step" body={{ n: 1 }}>
          +1 reading
        </B>
        <B name="day" path="/api/step" body={{ n: 3 }}>
          +1 day
        </B>
      </div>

      <h3>1 · Fault in factory A</h3>
      <B name="fault" path="/api/mode" body={{ machine: meta.fault_machine, mode: "fault" }}>
        Inject fault: {meta.fault_machine}
      </B>

      <h3>2 · Owner's voice note</h3>
      <audio controls src="/api/voice_note.wav" preload="none" />
      <p className="small muted">Synthetic note (macOS text-to-speech). Transcribed here by local Whisper.</p>
      <input
        className="input"
        placeholder="or type it: ब्लोअर में खट-खट ..."
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <B name="label" path="/api/label" body={{ text: text || null }}>
        {text ? "Label typed note" : "Transcribe and label"}
      </B>
      {lab && (
        <div className="note">
          <div className="small muted">heard ({lab.via})</div>
          <div lang="hi">{lab.text}</div>
          <div className="mono small">
            {lab.sound} → <b>{lab.fault}</b> <span className="muted">(weak label, {lab.source})</span>
          </div>
          <B name="publish" path="/api/publish" disabled={lab.fault === "unknown"}>
            Confirm and publish fingerprint
          </B>
        </div>
      )}

      <h3>3 · Same blower model, factory B</h3>
      <p className="small muted">SYNTHETIC onset: abnormal clip mixed into normal, stronger each reading.</p>
      <B name="onset" path="/api/mode" body={{ machine: meta.similar_machine, mode: "onset" }}>
        Start onset: {meta.similar_machine}
      </B>

      <h3>4 · Daily digest</h3>
      <div className="row gap">
        <B name="rules" path="/api/agent" body={{ engine: "rules" }}>
          Run (rules)
        </B>
        <B name="granite" path="/api/agent" body={{ engine: "granite" }} disabled={!meta.has_llm}>
          Run (Granite)
        </B>
      </div>
      {!meta.has_llm && (
        <p className="small muted">
          Run (Granite) needs Ollama running with {meta.llm?.model ?? "the Granite model"} pulled.
        </p>
      )}

      <h3>Other machines</h3>
      <div className="row gap">
        <select className="input" value={other} onChange={(e) => setOther(e.target.value)}>
          {state.machines.map((m) => (
            <option key={m.id} value={m.id}>
              {m.id} {m.name}
            </option>
          ))}
        </select>
        {(["normal", "fault", "onset"] as const).map((md) => (
          <button
            key={md}
            className="btn"
            disabled={lock}
            onClick={run(md, "/api/mode", { machine: other, mode: md })}
          >
            {md}
          </button>
        ))}
      </div>
      <h3>Shed signal</h3>
      <p className="small muted">
        Fingerprints leave through the phone's outbox. With no signal they wait and are sent, once, when it
        returns.
      </p>
      <label className="row gap small">
        <input
          type="checkbox"
          checked={state.link.up}
          disabled={busy !== null}
          onChange={(e) => run("link", "/api/link", { up: e.target.checked })()}
        />
        Registry reachable{" "}
        {state.link.pending > 0 && <span className="accent">({state.link.pending} waiting)</span>}
      </label>
      <h3>Start over</h3>
      <B name="reset" path="/api/reset">
        Reset demo
      </B>
      {err && (
        <p className="small accent" role="alert">
          {err}
        </p>
      )}
    </div>
  );
}

const pct = (v: number | null | undefined) => (v == null ? "--" : `${Math.round(v * 100)}%`);
const f3 = (v: number | null | undefined) => (v == null ? "--" : v.toFixed(3));

export function Evaluation({ meta }: { meta: Meta }) {
  const ev = meta.eval;
  if (!ev) return <p>No evaluation run found. Run: python -m dhadkan.train</p>;
  return (
    <div>
      <p className="small muted">
        From <span className="mono">runs/eval_{ev.run_id}.json</span> (code {ev.code_sha256}). Clip-level AUC
        / pAUC (DCASE, FPR ≤ 0.1). False alarms and faults caught use each machine's own alarm level.
      </p>
      <table className="grid mono">
        <thead>
          <tr>
            <th>type</th>
            <th>condition</th>
            <th>AUC</th>
            <th>pAUC</th>
            <th>n</th>
            <th>false alarms</th>
            <th>caught</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(ev.types).flatMap(([t, rep]) =>
            Object.entries(rep.conditions).map(
              ([c, m]) =>
                m && (
                  <tr key={t + c} className={m.auc < 0.7 || m.false_alarm_rate > 0.5 ? "weak" : ""}>
                    <td>{t}</td>
                    <td>
                      {c}
                      {c !== rep.headline ? " (stress)" : ""}
                    </td>
                    <td>{f3(m.auc)}</td>
                    <td>{f3(m.pauc)}</td>
                    <td>
                      {m.n_normal}+{m.n_anomalous}
                    </td>
                    <td>{pct(m.false_alarm_rate)}</td>
                    <td>{pct(m.detection_rate)}</td>
                  </tr>
                ),
            ),
          )}
        </tbody>
      </table>
      <h3>Per recorded unit</h3>
      <table className="grid mono">
        <thead>
          <tr>
            <th>unit</th>
            <th>AUC</th>
            <th>pAUC</th>
          </tr>
        </thead>
        <tbody>
          {Object.values(ev.types).flatMap((rep) =>
            Object.entries(rep.per_unit).map(
              ([u, m]) =>
                m && (
                  <tr key={u} className={m.auc < 0.7 ? "weak" : ""}>
                    <td>{u}</td>
                    <td>{f3(m.auc)}</td>
                    <td>{f3(m.pauc)}</td>
                  </tr>
                ),
            ),
          )}
        </tbody>
      </table>
      <h3>Where it fails</h3>
      <ul className="small">
        {(() => {
          const units = Object.values(ev.types).flatMap((rep) =>
            Object.entries(rep.per_unit).flatMap(([u, m]) => (m ? [{ u, auc: m.auc }] : [])),
          );
          const worst = units.sort((a, b) => a.auc - b.auc)[0];
          return (
            worst && (
              <li>
                <b>Weakest machine</b>: {worst.u} on the official DCASE test split, AUC {f3(worst.auc)}.
              </li>
            )
          );
        })()}
        <li>
          <b>Speed / load change</b>: gearbox target domain AUC {f3(ev.types.gearbox?.conditions.target?.auc)}
          , catches {pct(ev.types.gearbox?.conditions.target?.detection_rate)} of faults.
        </li>
      </ul>
      <h3>Registry: synthetic onset 0.25</h3>
      <table className="grid mono">
        <thead>
          <tr>
            <th>case</th>
            <th>from → to</th>
            <th>alarm</th>
            <th>registry</th>
            <th>false flags</th>
          </tr>
        </thead>
        <tbody>
          {Object.values(ev.types).flatMap((rep) =>
            rep.registry.map((r) => (
              <tr key={r.from + r.to} className={r.false_flags > 0.2 ? "weak" : ""}>
                <td>{r.case}</td>
                <td>
                  {r.from} → {r.to}
                </td>
                <td>{pct(r.alarm)}</td>
                <td>{pct(r.registry)}</td>
                <td>{pct(r.false_flags)}</td>
              </tr>
            )),
          )}
        </tbody>
      </table>
      <p className="small muted">
        Phone model:{" "}
        {Object.entries(ev.types)
          .map(
            ([t, r]) =>
              `${t} ${Math.round(r.tflite.size_bytes / 1024)} KB (≤${(r.tflite.max_rel_score_diff_vs_keras * 100).toFixed(2)}% vs Keras)`,
          )
          .join(" · ")}
      </p>
    </div>
  );
}

export function Log({ state }: { state: State }) {
  return (
    <div>
      <h3>WhatsApp alerts</h3>
      {state.alerts.length === 0 && <p className="small muted">No owner alert yet.</p>}
      {state.alerts.map((a) => (
        <details key={a.id} className="call">
          <summary className="mono small">
            <span className="muted">#{a.id}</span> {a.kind} · {a.machine} · {a.severity} · {alertWord(a)}
            {a.choice ? ` · reply ${a.reply} (${a.choice})` : ""}
          </summary>
          <pre className="mono">{a.body}</pre>
        </details>
      ))}
      <h3>Agent tool calls</h3>
      {state.tool_calls.length === 0 && <p className="small muted">No agent run yet.</p>}
      {state.tool_calls.map((t) => (
        <details key={t.id} className="call">
          <summary className="mono small">
            <span className="muted">
              {t.ts} {t.actor}
            </span>{" "}
            {t.tool} <span className="muted">{t.input}</span>
          </summary>
          <pre className="mono">{JSON.stringify(JSON.parse(t.output), null, 1)}</pre>
        </details>
      ))}
      <h3>Events</h3>
      <ul className="events mono small">
        {[...state.events]
          .reverse()
          .filter((e) => e.kind !== "reading")
          .map((e) => (
            <li key={e.seq}>
              <span className="muted">
                {e.clock}
                {e.replay_t != null ? ` · ${fmtT(e.replay_t)}` : ""}
              </span>{" "}
              {e.kind} {"machine" in e ? String(e.machine) : ""}
            </li>
          ))}
      </ul>
    </div>
  );
}
