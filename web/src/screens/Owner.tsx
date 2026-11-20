// Owner screen (phone, white): one card per machine in the owner's language. Status, one line of advice,
// rupees at risk, and the decision buttons (the same path as a WhatsApp reply). ?view=owner&f=A&lang=hi
import { useEffect, useState } from "react";
import { get, post, useLive, useMeta, type DiagnosisCard, type Machine } from "../api";

type Lang = "hi" | "hi_latn" | "mr" | "gu" | "en";
const L: Record<Lang, Record<string, string>> = {
  hi: {
    suno: "सुनो",
    novoice: "आवाज़ अभी नहीं बन सकी",
    title: "मेरी मशीनें",
    ok: "ठीक",
    learning: "सीख रही है",
    risk: "खतरे में (2 दिन)",
    none: "सब ठीक है।",
    demo: "डेमो डेटा",
    sent: "भेज दिया",
    noreq: "अभी कोई अनुरोध नहीं",
  },
  hi_latn: {
    suno: "Suno",
    novoice: "Awaaz abhi nahi ban saki",
    title: "Meri machines",
    ok: "Theek",
    learning: "Seekh rahi hai",
    risk: "khatre mein (2 din)",
    none: "Sab theek hai.",
    demo: "demo data",
    sent: "Bhej diya",
    noreq: "Abhi koi request nahi",
  },
  mr: {
    suno: "ऐका",
    novoice: "आवाज आत्ता तयार झाला नाही",
    title: "माझ्या मशीन",
    ok: "ठीक",
    learning: "शिकत आहे",
    risk: "धोक्यात (2 दिवस)",
    none: "सगळे ठीक आहे.",
    demo: "डेमो डेटा",
    sent: "पाठवले",
    noreq: "आत्ता विनंती नाही",
  },
  gu: {
    suno: "સાંભળો",
    novoice: "અવાજ હમણાં બની શક્યો નહીં",
    title: "મારા મશીન",
    ok: "ઠીક",
    learning: "શીખે છે",
    risk: "જોખમમાં (2 દિવસ)",
    none: "બધું ઠીક છે.",
    demo: "ડેમો ડેટા",
    sent: "મોકલ્યું",
    noreq: "હમણાં કોઈ વિનંતી નથી",
  },
  en: {
    suno: "Listen",
    novoice: "No voice right now",
    title: "My machines",
    ok: "Fine",
    learning: "Learning",
    risk: "at risk (2 days)",
    none: "Everything is fine.",
    demo: "demo data",
    sent: "Sent",
    noreq: "No open request",
  },
};
const BUTTONS: Record<Lang, [string, string, string]> = {
  hi: ["मिस्त्री बुलाओ", "बाद में", "पूरी जानकारी"],
  hi_latn: ["Mechanic bulao", "Baad mein", "Details"],
  mr: ["मिस्त्री बोलवा", "उद्या", "माहिती"],
  gu: ["મિસ્ત્રી બોલાવો", "કાલે", "માહિતી"],
  en: ["Send mechanic", "Later", "Details"],
};
const rs = (v: number) => "₹" + Math.round(v).toLocaleString("en-IN");

export function OwnerScreen({ factory, lang }: { factory: string; lang: Lang }) {
  const state = useLive();
  const meta = useMeta();
  const f = meta?.factories.find((x) => x.id === factory);
  const t = L[lang];
  if (!state || !meta) return <main className="lite">…</main>;
  const mine = state.machines
    .filter((m) => m.factory === factory)
    .sort((a, b) => b.rupees_at_risk - a.rupees_at_risk || (b.risk3 ?? 0) - (a.risk3 ?? 0));
  const flagged = mine.filter((m) => ["alarm", "early_match", "watch"].includes(m.status));
  return (
    <main className="lite" lang={lang === "hi_latn" ? "hi-Latn" : lang}>
      <header className="lite-head">
        <h1>{t.title}</h1>
        <p className="lite-sub">
          {f?.name} · <span className="lite-tag">{t.demo}</span>
        </p>
      </header>
      {flagged.length === 0 && <p className="lite-line">{t.none}</p>}
      {flagged.map((m) => (
        <OwnerCard key={m.id} m={m} lang={lang} tick={state.tick} />
      ))}
      <ul className="lite-quiet">
        {mine
          .filter((m) => !flagged.includes(m))
          .map((m) => (
            <li key={m.id}>
              <b>{lang === "hi" || lang === "mr" ? m.name_local : m.name}</b>{" "}
              {m.status === "learning" && m.learning
                ? `${t.learning} ${m.learning.day}/${m.learning.of}`
                : t.ok}
            </li>
          ))}
      </ul>
    </main>
  );
}

function OwnerCard({ m, lang, tick }: { m: Machine; lang: Lang; tick: number }) {
  const t = L[lang];
  const [card, setCard] = useState<DiagnosisCard | null>(null);
  const [out, setOut] = useState<string | null>(null);
  useEffect(() => {
    let on = true;
    get<{ card: DiagnosisCard | null }>(`/api/diagnosis/${m.id}?lang=${lang}`)
      .then((r) => on && setCard(r.card))
      .catch(() => on && setCard(null));
    return () => {
      on = false;
    };
  }, [m.id, lang, tick]);
  const decide = async (choice: "book" | "later" | "details") => {
    const r = await post<{ text?: string; booked?: boolean; why?: string }>("/api/owner/decide", {
      machine: m.id,
      choice,
      lang,
    });
    setOut(r.text ?? (r.why ? t.noreq : t.sent));
  };
  const [playing, setPlaying] = useState(false);
  // "suno": the alert read aloud, from the same template text (server/voice), in the owner's language
  const listen = async () => {
    setPlaying(true);
    try {
      const a = new Audio(`/api/voice/machine/${encodeURIComponent(m.id)}.ogg?lang=${lang}`);
      a.onended = () => setPlaying(false);
      a.onerror = () => {
        setPlaying(false);
        setOut(t.novoice);
      };
      await a.play();
    } catch {
      setPlaying(false);
      setOut(t.novoice);
    }
  };
  const what = card?.what?.[lang as "en"] ?? card?.unknown?.[lang as "en"];
  const sev = card?.severity.label[lang as "en"];
  return (
    <section className="lite-card">
      <div className="lite-row">
        <h2>{lang === "hi" || lang === "mr" ? m.name_local : m.name}</h2>
        {sev && <span className="lite-sev">{sev}</span>}
        <button className="lite-suno" onClick={() => void listen()} disabled={playing} aria-label={t.suno}>
          🔊 {t.suno}
        </button>
      </div>
      {what && <p className="lite-line">{what}</p>}
      {m.rupees_at_risk > 0 && (
        <p className="lite-risk">
          {rs(m.rupees_at_risk)} {t.risk}
        </p>
      )}
      <div className="lite-btns">
        {BUTTONS[lang].map((b, i) => (
          <button
            key={b}
            className={i === 0 ? "lite-go" : "lite-btn"}
            onClick={() => decide((["book", "later", "details"] as const)[i])}
          >
            {b}
          </button>
        ))}
      </div>
      {out && <p className="lite-out">{out}</p>}
    </section>
  );
}
