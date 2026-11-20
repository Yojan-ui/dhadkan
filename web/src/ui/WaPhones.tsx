// Demo phones without phones: the owner's and the mechanic's WhatsApp side by side (?view=phones, and at the
// foot of the phone list). Replies go to /api/wa/simulate, which posts them into the same code path as the
// Meta / Twilio webhook. Only with the dry-run provider: nothing leaves the laptop.
import { useCallback, useEffect, useState } from "react";
import { get, post } from "../api";

interface Msg {
  ts: number;
  dir: "in" | "out";
  text: string | null;
  kind: string;
  status?: string;
  media?: boolean;
  simulated?: boolean;
}
interface Phones {
  owner: string | null;
  mechanic: string | null;
  technician: string | null;
  provider: string;
  owner_thread: Msg[];
  mechanic_thread: Msg[];
}
type Role = "owner" | "mechanic";

const QUICK: Record<Role, string[]> = {
  owner: ["1", "2", "status", "kitna kharcha", "kab aayega mistri", "band karo"],
  mechanic: ["1", "2", "3", "raste mein 20 min", "ho gaya: bearing badla"],
};

function toB64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] ?? "");
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

function Phone({
  role,
  title,
  number,
  thread,
  factory,
  onSent,
}: {
  role: Role;
  title: string;
  number: string | null;
  thread: Msg[];
  factory: string;
  onSent: () => void;
}) {
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const send = async (body: Record<string, unknown>) => {
    setErr(null);
    try {
      await post("/api/wa/simulate", { role, factory, ...body });
      onSent();
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };
  const file = async (f: File | undefined, kind: "image" | "audio") => {
    if (!f) return;
    const b64 = await toB64(f);
    await send(
      kind === "image"
        ? { image_b64: b64, mime: f.type, text: text || null }
        : { audio_b64: b64, mime: f.type },
    );
    setText("");
  };
  return (
    <section className="wa-phone" aria-label={`${title} phone`}>
      <header className="wa-head">
        <b>{title}</b>
        <span className="mono tiny">{number ?? "no number"}</span>
      </header>
      <ol className="wa-thread">
        {thread.length === 0 && <li className="wa-empty">No messages yet.</li>}
        {thread.map((m, i) => (
          // on this phone, Dhadkan's messages arrive (left); the person's own replies go out (right)
          <li key={i} className={m.dir === "out" ? "wa-recv" : "wa-sent"}>
            {m.media && <span className="wa-media">{m.kind === "image" ? "📷 photo" : "🎤 recording"}</span>}
            {m.text && <span>{m.text}</span>}
            <small>
              {new Date(m.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              {m.dir === "out" && m.status ? ` · ${m.status.replace("_", " ")}` : ""}
            </small>
          </li>
        ))}
      </ol>
      <div className="wa-quick">
        {QUICK[role].map((q) => (
          <button key={q} onClick={() => void send({ text: q })} disabled={!number}>
            {q}
          </button>
        ))}
      </div>
      <form
        className="wa-input"
        onSubmit={(e) => {
          e.preventDefault();
          if (text.trim()) void send({ text: text.trim() }).then(() => setText(""));
        }}
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Message"
          disabled={!number}
        />
        <label className="wa-file" title="send a photo">
          📷
          <input
            type="file"
            accept="image/*"
            hidden
            onChange={(e) => void file(e.target.files?.[0], "image")}
          />
        </label>
        {role === "owner" && (
          <label className="wa-file" title="send a recording">
            🎤
            <input
              type="file"
              accept="audio/*"
              hidden
              onChange={(e) => void file(e.target.files?.[0], "audio")}
            />
          </label>
        )}
        <button type="submit" disabled={!number || !text.trim()}>
          Send
        </button>
      </form>
      {err && <p className="wa-err">{err}</p>}
    </section>
  );
}

export function WaPhones({ factories }: { factories: { id: string; name: string }[] }) {
  const [factory, setFactory] = useState(factories[0]?.id ?? "A");
  const [p, setP] = useState<Phones | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setP(await get<Phones>(`/api/wa/phones?factory=${encodeURIComponent(factory)}`));
      setErr(null);
    } catch (e) {
      setErr(String((e as Error).message));
    }
  }, [factory]);
  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), 2000);
    return () => clearInterval(id);
  }, [load]);
  return (
    <div className="wa-phones">
      <div className="row between wa-bar">
        <label>
          Factory{" "}
          <select value={factory} onChange={(e) => setFactory(e.target.value)}>
            {factories.map((f) => (
              <option key={f.id} value={f.id}>
                {f.id} {f.name}
              </option>
            ))}
          </select>
        </label>
        <span className="tiny muted">
          {p?.provider === "dry_run"
            ? "Simulated phones · dry run: nothing is sent · same code path as the WhatsApp webhook"
            : "A real WhatsApp provider is set: simulated phones are off (use DHADKAN_DRY_RUN=1)"}
        </span>
      </div>
      {err && <p className="wa-err">{err}</p>}
      <div className="wa-pair">
        <Phone
          role="owner"
          title="Owner"
          number={p?.owner ?? null}
          thread={p?.owner_thread ?? []}
          factory={factory}
          onSent={load}
        />
        <Phone
          role="mechanic"
          title={`Mechanic${p?.technician ? ` · ${p.technician}` : ""}`}
          number={p?.mechanic ?? null}
          thread={p?.mechanic_thread ?? []}
          factory={factory}
          onSent={load}
        />
      </div>
    </div>
  );
}
