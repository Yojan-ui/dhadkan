// Agent chat (bottom dock, collapsible): type or speak in Hindi, Marathi, Gujarati, Tamil, Kannada, Telugu
// or English; the agent replies in the same language and shows the tool steps it took.
import { useRef, useState } from "react";
import { post } from "../api";

interface Step {
  step: string;
  tool: string;
  machine: string;
  summary: string;
}
interface Reply {
  reply: string;
  lang: string;
  engine: string;
  steps: Step[];
  heard?: string;
  fallback_reason?: string;
}
interface Turn {
  who: "owner" | "agent";
  text: string;
  meta?: Reply;
}

const EXAMPLES = ["Loom 1 ki awaaz kyun badal gayi?", "लूम 1 की आवाज़ क्यों बदल गई?", "லூம் 1 ஒலி ஏன் மாறியது?"];

export function AgentDock({ factory }: { factory: string }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [rec, setRec] = useState<MediaRecorder | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const chunks = useRef<Blob[]>([]);

  const send = async (q: string) => {
    if (!q.trim()) return;
    setTurns((t) => [...t, { who: "owner", text: q }]);
    setText("");
    setBusy(true);
    setErr(null);
    try {
      const r = await post<Reply>("/api/agent/chat", { text: q, factory });
      setTurns((t) => [...t, { who: "agent", text: r.reply, meta: r }]);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const voice = async () => {
    if (rec) {
      rec.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const r = new MediaRecorder(stream);
      chunks.current = [];
      r.ondataavailable = (e) => chunks.current.push(e.data);
      r.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        setRec(null);
        setBusy(true);
        try {
          const blob = new Blob(chunks.current, { type: r.mimeType });
          const res = await fetch(`/api/agent/voice?factory=${factory}`, { method: "POST", body: blob });
          if (!res.ok) throw new Error(`voice note: ${res.status}`);
          const out: Reply = await res.json();
          setTurns((t) => [
            ...t,
            { who: "owner", text: `(voice) ${out.heard ?? ""}` },
            { who: "agent", text: out.reply, meta: out },
          ]);
        } catch (e) {
          setErr((e as Error).message);
        } finally {
          setBusy(false);
        }
      };
      r.start();
      setRec(r);
    } catch {
      setErr("Microphone not available in this browser.");
    }
  };

  return (
    <section className={`dock ${open ? "open" : ""}`} aria-label="agent chat">
      <header onClick={() => setOpen(!open)}>
        <span>Agent</span>
        <small>observe → diagnose → plan → ask owner → act → verify · {open ? "hide" : "show"}</small>
      </header>
      {open && (
        <>
          <div className="turns">
            {turns.length === 0 && (
              <div className="dim small">
                Ask in your language, for example:{" "}
                {EXAMPLES.map((e) => (
                  <button key={e} className="ex" onClick={() => void send(e)}>
                    {e}
                  </button>
                ))}
              </div>
            )}
            {turns.map((t, i) => (
              <div key={i} className={`turn ${t.who}`}>
                <p>{t.text}</p>
                {t.meta && (
                  <ol className="timeline">
                    {t.meta.steps.map((s, k) => (
                      <li key={k}>
                        <b>{s.step}</b> <code>{s.tool}</code> {s.summary}
                      </li>
                    ))}
                    <li className="dim">
                      {t.meta.engine === "claude" ? "Claude Fable 5.2" : "rules (offline / no key)"} ·{" "}
                      {t.meta.lang}
                      {t.meta.fallback_reason ? ` · fallback: ${t.meta.fallback_reason}` : ""}
                    </li>
                  </ol>
                )}
              </div>
            ))}
          </div>
          <form
            className="ask"
            onSubmit={(e) => {
              e.preventDefault();
              void send(text);
            }}
          >
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Loom 1 ki awaaz kyun badal gayi?"
              aria-label="message to the agent"
            />
            <button
              type="button"
              className={rec ? "rec" : ""}
              onClick={() => void voice()}
              aria-label="voice note"
            >
              {rec ? "Stop" : "Voice"}
            </button>
            <button type="submit" disabled={busy}>
              {busy ? "…" : "Send"}
            </button>
          </form>
          {err && <p className="err small">{err}</p>}
        </>
      )}
    </section>
  );
}
