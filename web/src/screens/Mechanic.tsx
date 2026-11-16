// Mechanic screen (phone, white): the next visits with what to carry, and the job history. ?view=mechanic&f=A
import { useEffect, useState } from "react";
import { get, useLive, useMeta, type DiagnosisCard } from "../api";

interface RepairRow {
  id: number;
  record: {
    machine: { id: string; name: string };
    repaired_at: string;
    part: string | null;
    verification: { outcome: string; words: Record<string, string> };
  };
}

export function MechanicScreen({ factory }: { factory: string | null }) {
  const state = useLive();
  const meta = useMeta();
  const [cards, setCards] = useState<Record<string, DiagnosisCard | null>>({});
  const [history, setHistory] = useState<RepairRow[]>([]);
  const legs = (state?.route?.legs ?? []).filter((l) => !factory || l.factory === factory);
  const jobs = (state?.alerts ?? []).filter((a) => a.kind === "job" && (!factory || a.factory === factory));
  const ids = [...new Set([...legs.map((l) => l.machine), ...jobs.map((j) => j.machine)])].join(",");
  useEffect(() => {
    let on = true;
    for (const id of ids.split(",").filter(Boolean)) {
      get<{ card: DiagnosisCard | null }>(`/api/diagnosis/${id}?lang=en`)
        .then((r) => on && setCards((c) => ({ ...c, [id]: r.card })))
        .catch(() => undefined);
    }
    get<RepairRow[]>("/api/records")
      .then((r) => on && setHistory(r))
      .catch(() => undefined);
    return () => {
      on = false;
    };
  }, [ids, state?.tick]);
  if (!state || !meta) return <main className="lite">…</main>;
  const name = (id: string) => state.machines.find((m) => m.id === id)?.name ?? id;
  const fac = (id: string) => meta.factories.find((f) => f.id === id)?.name ?? id;
  return (
    <main className="lite" lang="en">
      <header className="lite-head">
        <h1>Visits</h1>
        <p className="lite-sub">
          {state.route
            ? `${state.route.weekday} ${state.route.date} · ${state.route.drive_km} km`
            : "No route planned"}{" "}
          · <span className="lite-tag">demo data</span>
        </p>
      </header>
      {legs.length === 0 && jobs.length === 0 && <p className="lite-line">No visits booked.</p>}
      {legs.map((l) => (
        <section key={l.machine} className="lite-card">
          <div className="lite-row">
            <h2>{name(l.machine)}</h2>
            <span className="lite-sev">{l.arrive.slice(11, 16)}</span>
          </div>
          <p className="lite-line">
            {fac(l.factory)} · {cards[l.machine]?.name?.en ?? "check on site"}
          </p>
          <p className="lite-risk">Carry: {cards[l.machine]?.fix?.part.en ?? "tools for inspection"}</p>
        </section>
      ))}
      {jobs.map((j) => (
        <section key={j.id} className="lite-card">
          <p className="lite-line">{j.body}</p>
        </section>
      ))}
      <h2 className="lite-h2">Job history</h2>
      <ul className="lite-quiet">
        {history.length === 0 && <li>No verified repairs yet.</li>}
        {history.map((h) => (
          <li key={h.id}>
            <b>{h.record.machine.name}</b> {h.record.repaired_at.slice(0, 10)} · {h.record.part ?? "-"} ·{" "}
            {h.record.verification.words.en}
          </li>
        ))}
      </ul>
    </main>
  );
}
