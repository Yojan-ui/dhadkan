// Demo mode: replay the real NASA IMS bearing failure on Loom 1 and send the alert to a judge's WhatsApp.
import { useState } from "react";
import { post, type State } from "../api";
import { alertWord } from "./Diagnosis";

export function DemoMode({ state }: { state: State }) {
  const [to, setTo] = useState("");
  const [lang, setLang] = useState("hi");
  const [err, setErr] = useState<string | null>(null);
  const d = state.demo;
  const alert = state.alerts.find((a) => a.machine === (d.machine ?? "A-G1") && a.kind === "alert");
  const start = async (on: boolean) => {
    setErr(null);
    try {
      await post("/api/demo", { on, to: to.trim() || null, lang });
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <div className="demo">
      <p className="small">
        Replays a real bearing failure: <b>NASA IMS test 2, bearing 1</b> (outer race, February 2004), run to
        destruction over 7 days, played as Loom 1&rsquo;s motor bearing. When its health score crosses the
        alarm, the fault-type classifier (trained on CWRU) names the fault from the vibration and the alert
        goes out on WhatsApp.
      </p>
      <label className="small">
        Judge&rsquo;s WhatsApp number
        <input
          className="input"
          inputMode="tel"
          placeholder="+91 98xxx xxxxx"
          value={to}
          onChange={(e) => setTo(e.target.value)}
        />
      </label>
      <label className="small">
        Language{" "}
        <select className="input" value={lang} onChange={(e) => setLang(e.target.value)}>
          <option value="hi">Hindi</option>
          <option value="hi_latn">Hindi (Roman script)</option>
          <option value="mr">Marathi</option>
          <option value="en">English</option>
        </select>
      </label>
      <div className="row gap">
        <button className="btn yes" onClick={() => start(true)} disabled={d.on}>
          {d.on ? "Running…" : "Start Demo mode"}
        </button>
        <button className="btn ghost" onClick={() => start(false)} disabled={!d.on}>
          Stop
        </button>
      </div>
      <p className="small muted">
        WhatsApp provider: <span className="mono">{state.whatsapp.provider}</span>
        {state.whatsapp.provider === "dry_run" &&
          " (no keys in .env: the message is built and logged, not sent; a Meta test number only reaches numbers registered on it)"}
      </p>
      {d.phase !== "off" && (
        <div className="small mono demo-status">
          <div>
            {d.phase}
            {d.readings ? ` · reading ${d.at ?? 0} of ${d.readings}` : ""}
          </div>
          {d.classified && (
            <div>
              classifier: {d.classified.class.replace("_", " ")} (
              {Object.entries(d.classified.p)
                .map(([k, v]) => `${k.replace("_", " ")} ${Math.round(v * 100)}%`)
                .join(", ")}
              ) · IMS truth: outer race
            </div>
          )}
          {alert && <div>WhatsApp {alertWord(alert)}</div>}
        </div>
      )}
      {err && <p className="small accent">{err}</p>}
    </div>
  );
}
