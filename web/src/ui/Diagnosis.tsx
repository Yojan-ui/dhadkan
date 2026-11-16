// The diagnosis card for one machine (server/faults.yaml via /api/diagnosis) and its WhatsApp alert status.
import { useEffect, useState } from "react";
import { get, type Alert, type DiagnosisCard } from "../api";
import { inr } from "../theme";

const SEV_CLASS: Record<string, string> = {
  watch: "sev-watch",
  repair_soon: "sev-soon",
  stop_now: "sev-stop",
  replace: "sev-stop",
};

export function DiagnosisBlock({ id, tick, alerts }: { id: string; tick: number; alerts: Alert[] }) {
  const [card, setCard] = useState<DiagnosisCard | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let on = true;
    get<{ card: DiagnosisCard | null }>(`/api/diagnosis/${id}?lang=en`)
      .then((r) => on && (setCard(r.card), setErr(null)))
      .catch((e: Error) => on && setErr(e.message));
    return () => {
      on = false;
    };
  }, [id, tick]);
  const alert = alerts.find((a) => a.machine === id && a.kind === "alert");
  if (err) return <p className="small muted">Couldn&rsquo;t build the diagnosis card: {err}.</p>;
  if (!card) return null;
  const range = (r: [number, number] | null | undefined) =>
    r ? `${inr(r[0])}–${inr(r[1])}` : "no published price";
  return (
    <section className="diag" aria-label="diagnosis">
      <div className="row between">
        <b className="small">Diagnosis</b>
        <span className={`sev ${SEV_CLASS[card.severity.level] ?? ""}`}>{card.severity.label.en}</span>
      </div>
      {card.fault === null ? (
        <p className="small">{card.unknown?.en}</p>
      ) : (
        <>
          <p className="diag-what">
            {card.what?.en}{" "}
            {card.named_by && (
              <span className="muted small">
                (named by{" "}
                {card.named_by === "classifier" ? "the CWRU-trained classifier" : "the owner's word"})
              </span>
            )}
          </p>
          <table className="kv small">
            <tbody>
              <tr>
                <th>repairable</th>
                <td>
                  {card.repairable?.answer ? "yes" : "no"} · {card.repairable?.why}
                </td>
              </tr>
              <tr>
                <th>fix</th>
                <td>
                  {card.fix?.part.en}: {range(card.fix?.price_inr)} ({card.fix?.price_unit}) +{" "}
                  {card.fix?.labour_hours.join("–")} h · {card.fix?.technician.en}
                </td>
              </tr>
              <tr>
                <th>fix now</th>
                <td className="mono">{range(card.money?.fix_now_inr)} incl. labour and planned downtime</td>
              </tr>
              <tr>
                <th>wait {card.money?.wait_days} days</th>
                <td className="mono">
                  {Math.round((card.money?.p_fault_within_wait ?? 0) * 100)}% chance of a breakdown ≈{" "}
                  {inr(card.money?.breakdown_inr ?? 0)}
                </td>
              </tr>
              <tr>
                <th>damaged by</th>
                <td>{card.misuse?.map((m) => m.en).join("; ")}</td>
              </tr>
              <tr>
                <th>warranty</th>
                <td>
                  {card.warranty?.period ?? card.warranty?.note}
                  {card.warranty?.voided_by.length
                    ? ` Void if: ${card.warranty.voided_by.map((v) => v.en).join("; ")}.`
                    : ""}
                </td>
              </tr>
            </tbody>
          </table>
        </>
      )}
      <p className="small muted">{card.disclaimer.en} Sources in server/faults.yaml.</p>
      {alert && (
        <p className="small mono wa-status">
          WhatsApp {alertWord(alert)}
          {alert.choice
            ? ` · owner replied ${alert.reply} (${alert.choice})`
            : alert.last_reply
              ? ` · asked: ${alert.last_reply}`
              : ""}
          {alert.escalated_alert ? " · escalated to supervisor" : ""}
        </p>
      )}
    </section>
  );
}

export function alertWord(a: Alert) {
  switch (a.status) {
    case "sent":
      return `sent (${a.channel === "sms_twilio" ? "by SMS" : "WhatsApp"})`;
    case "dry_run":
      return "not sent: no WhatsApp keys (dry run)";
    case "no_contact":
      return "not sent: no phone number for this factory";
    case "queued":
      return "waiting for network (queued)";
    default:
      return a.status;
  }
}
