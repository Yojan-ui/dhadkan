// Messages timeline (side panel): every WhatsApp sent / received and every agent tool step for one machine.
import { useEffect, useState } from "react";
import { get } from "../api";

type Msg = {
  id: number;
  factory_ts: string | null;
  direction: "out" | "in" | "agent";
  role: string;
  text: string;
  status: string;
  kind: string;
  stage: string | null;
  phone: string | null;
};

export function Messages({ id, tick }: { id: string; tick: number }) {
  const [rows, setRows] = useState<Msg[] | null>(null);
  useEffect(() => {
    let on = true;
    get<Msg[]>(`/api/messages/${id}`)
      .then((r) => on && setRows(r))
      .catch(() => on && setRows(null));
    return () => {
      on = false;
    };
  }, [id, tick]);
  return (
    <section className="msgs" aria-label="WhatsApp messages">
      <h3>Messages</h3>
      {!rows?.length && (
        <p className="dim small">No WhatsApp messages or agent steps for this machine yet.</p>
      )}
      <ol>
        {(rows ?? []).slice(-40).map((r) => (
          <li key={r.id} className={`m-${r.direction} k-${r.kind}`}>
            <div className="meta small dim">
              {r.direction === "out" ? "→ " : r.direction === "in" ? "← " : "⚙ "}
              {r.role}
              {r.stage ? ` · ${r.stage}` : ""}
              {r.factory_ts ? ` · ${r.factory_ts.replace("T", " ")}` : ""}
              {r.status ? ` · ${r.status}` : ""}
            </div>
            <p>{r.text}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}
