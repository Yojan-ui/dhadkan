// Messages: every WhatsApp sent / received and every agent tool step for one machine. From /api/messages/{id}.
import { useEffect, useState } from "react";
import { get } from "../api";
import { Panel } from "./ui";

type Msg = {
  id: number;
  factory_ts: string | null;
  direction: "out" | "in" | "agent";
  role: string;
  text: string;
  status: string;
  kind: string;
  stage: string | null;
};

const ARROW = { out: "→", in: "←", agent: "⚙" } as const;

export function Messages({ id, version }: { id: string; version: unknown }) {
  const [rows, setRows] = useState<Msg[] | null>(null);
  useEffect(() => {
    let on = true;
    get<Msg[]>(`/api/messages/${id}`)
      .then((r) => on && setRows(r))
      .catch(() => on && setRows(null));
    return () => {
      on = false;
    };
  }, [id, version]);
  return (
    <Panel title="Messages (WhatsApp and agent steps)">
      {!rows?.length ? (
        <span className="muted small">No WhatsApp messages or agent steps for this machine yet.</span>
      ) : (
        <ol className="cc-msgs">
          {rows.slice(-40).map((r) => (
            <li key={r.id} className={`m-${r.direction} k-${r.kind}`}>
              <div className="muted small mono">
                {ARROW[r.direction] ?? "·"} {r.role}
                {r.stage ? ` · ${r.stage}` : ""}
                {r.factory_ts ? ` · ${r.factory_ts.replace("T", " ")}` : ""}
                {r.status ? ` · ${r.status}` : ""}
              </div>
              <p className="small">{r.text}</p>
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}
