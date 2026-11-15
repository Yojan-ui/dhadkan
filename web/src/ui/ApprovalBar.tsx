// The owner's yes/no, as a slim bar. Text is the template the agent drafted in the
// owner's language; the English line under it is the same template in English.
// Approving books the visit; nothing here can touch a machine.
import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
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
        <motion.div
          key={msg.id}
          className="approval"
          role="region"
          aria-label="owner approval"
          initial={reduced ? false : { y: 60, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={reduced ? undefined : { y: 60, opacity: 0 }}
          transition={{ duration: 0.35, ease: "easeOut" }}
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
        </motion.div>
      ) : last ? (
        <motion.div
          key={`done-${last.id}`}
          className="approval done"
          initial={reduced ? false : { opacity: 0 }}
          animate={{ opacity: 1 }}
        >
          <span className="small mono muted">
            {factories.find((x) => x.id === last.factory)?.owner} replied "{last.reply}" ·{" "}
            {last.approved ? `visit booked (${last.visit_day})` : "no visit"} · {last.machine}
          </span>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
