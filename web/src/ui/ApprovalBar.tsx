// The owner's yes/no: a card in the machine panel (desktop) or a slim bar (phone). Text is the template the agent drafted in the
// owner's language; the English line under it is the same template in English.
// Approving books the visit; nothing here can touch a machine.
import { useState } from "react";
import { AnimatePresence, m as anim } from "framer-motion";
import { post, type Factory, type Machine, type Message, type Route } from "../api";
import { fmtClock } from "../theme";

const WORDS: Record<string, [string, string, string, string]> = {
  hi: ["हाँ", "Haan", "ना", "Nahi"],
  mr: ["हो", "Ho", "नाही", "Nahi"],
  en: ["Yes", "", "No", ""],
};
const LANG = { hi: "Hindi", mr: "Marathi", en: "English" } as Record<string, string>;

export function ApprovalBar({
  messages,
  factories,
  machines,
  route,
  reduced,
}: {
  messages: Message[];
  factories: Factory[];
  machines: Machine[];
  route: Route | null;
  reduced: boolean;
}) {
  // registry matches first: that owner has no alarm yet and most to gain from an early visit
  const hot = (m: Message) => (machines.find((x) => x.id === m.machine)?.status === "early_match" ? 0 : 1);
  const open = messages.filter((m) => m.approved === null).sort((a, b) => hot(a) - hot(b) || a.id - b.id);
  const last = [...messages].reverse().find((m) => m.approved !== null);
  const [busy, setBusy] = useState(false);
  const [shown, setShown] = useState(0);
  const msg = open[Math.min(shown, open.length - 1)];

  const answer = async (word: string) => {
    if (!msg) return;
    setBusy(true);
    try {
      await post("/api/reply", { message_id: msg.id, reply: word });
    } finally {
      setBusy(false);
    }
  };

  const f = msg && factories.find((x) => x.id === msg.factory);
  const mc = msg && machines.find((x) => x.id === msg.machine);
  const leg = msg && route?.legs.find((l) => l.machine === msg.machine);
  const w = WORDS[msg?.language ?? "en"] ?? WORDS.en;

  return (
    <AnimatePresence>
      {msg ? (
        <anim.div
          key={msg.id}
          className="approval"
          role="region"
          aria-label="owner approval"
          initial={reduced ? false : { y: 60, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={reduced ? undefined : { y: 60, opacity: 0 }}
          transition={{ duration: 0.25, ease: "easeOut" }}
        >
          <div className="who small muted">
            <span className="mono">
              SMS · {f?.owner} · {LANG[msg.language] ?? msg.language}
            </span>
            {open.length > 1 && (
              <span className="mono">
                {" "}
                · {Math.min(shown, open.length - 1) + 1} of {open.length}
              </span>
            )}
            {leg && (
              <span className="mono">
                {" "}
                · {mc?.name} on route, ETA {fmtClock(leg.arrive)}
              </span>
            )}
          </div>
          <div className="msg" lang={msg.language}>
            {msg.body}
          </div>
          {msg.text_en && msg.language !== "en" && <div className="gloss small muted">{msg.text_en}</div>}
          <div className="answers">
            <button className="btn yes" disabled={busy} onClick={() => answer(w[0])}>
              {w[0]}
              {w[1] && <small>{w[1]}</small>}
            </button>
            <button className="btn ghost" disabled={busy} onClick={() => answer(w[2])}>
              {w[2]}
              {w[3] && <small>{w[3]}</small>}
            </button>
            {open.length > 1 && (
              <button className="btn ghost" onClick={() => setShown((shown + 1) % open.length)}>
                Next message
              </button>
            )}
          </div>
          {msg.reply && <div className="small muted">Reply "{msg.reply}" was unclear; asking again.</div>}
        </anim.div>
      ) : last ? (
        <anim.div
          key={`done-${last.id}`}
          className="approval done"
          initial={reduced ? false : { opacity: 0 }}
          animate={{ opacity: 1 }}
        >
          <span className="small mono muted">
            {factories.find((x) => x.id === last.factory)?.owner} replied "{last.reply}" ·{" "}
            {last.approved ? `visit booked (${last.visit_day})` : "no visit"} · {last.machine}
          </span>
        </anim.div>
      ) : null}
    </AnimatePresence>
  );
}

/** The owner's message about one machine, inside its detail panel: the open question, or the answer. */
export function ApprovalCard({
  machine,
  messages,
  factories,
  route,
}: {
  machine: Machine;
  messages: Message[];
  factories: Factory[];
  route: Route | null;
}) {
  const mine = messages.filter((x) => x.machine === machine.id);
  const msg = mine.find((x) => x.approved === null);
  const last = [...mine].reverse().find((x) => x.approved !== null);
  const [busy, setBusy] = useState(false);
  if (!msg && !last) return null;
  const f = factories.find((x) => x.id === machine.factory);
  const leg = route?.legs.find((l) => l.machine === machine.id);
  if (!msg && last)
    return (
      <p className="small mono muted ac-done">
        {f?.owner} replied &ldquo;{last.reply}&rdquo; ·{" "}
        {last.approved ? `visit booked${leg ? `, ${fmtClock(leg.arrive)}` : ""}` : "no visit"}
      </p>
    );
  if (!msg) return null;
  const w = WORDS[msg.language] ?? WORDS.en;
  const answer = async (word: string) => {
    setBusy(true);
    try {
      await post("/api/reply", { message_id: msg.id, reply: word });
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="ac" role="region" aria-label="owner approval">
      <div className="small muted mono">
        SMS to {f?.owner} · {LANG[msg.language] ?? msg.language}
        {leg && <> · ETA {fmtClock(leg.arrive)}</>}
      </div>
      <p className="ac-msg" lang={msg.language}>
        {msg.body}
      </p>
      {msg.text_en && msg.language !== "en" && <p className="small muted ac-gloss">{msg.text_en}</p>}
      <div className="row gap">
        <button className="btn yes" disabled={busy} onClick={() => answer(w[0])}>
          {w[0]}
          {w[1] && <small>{w[1]}</small>}
        </button>
        <button className="btn ghost" disabled={busy} onClick={() => answer(w[2])}>
          {w[2]}
          {w[3] && <small>{w[3]}</small>}
        </button>
      </div>
      {msg.reply && <p className="small muted">Reply &ldquo;{msg.reply}&rdquo; was unclear; asking again.</p>}
    </section>
  );
}
