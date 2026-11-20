// The machine's papers (warranty card, insurance policy, invoice, AMC, manual) and the coverage verdict
// for the fault on the diagnosis card: server/coverage.py via /api/docs. Upload a PDF, photo or text file.
import { useEffect, useRef, useState } from "react";
import { get } from "../api";
import { inr } from "../theme";

type Doc = {
  id: string;
  kind: string;
  filename: string;
  read_by: "claude" | "rules" | "none";
  sample: boolean;
  warnings: string[];
  fields: {
    provider?: string | null;
    number?: string | null;
    end?: string | null;
    deductible_inr?: number | null;
  };
};
type Coverage = {
  verdict: "claimable" | "check" | "not_covered" | "no_docs" | "unknown_fault" | "error";
  best?: {
    kind: string;
    provider?: string | null;
    why: string;
    end?: string | null;
    days_left?: number | null;
  };
  saves_inr?: [number, number];
  dont_open?: boolean;
};
type DocsResp = { documents: Doc[]; coverage: Coverage; text: string; lang: string };

const VERDICT: Record<string, [string, string]> = {
  claimable: ["Likely claimable", "sev-watch"],
  check: ["Ask the company", "sev-soon"],
  not_covered: ["Not covered", "sev-stop"],
  no_docs: ["No papers", ""],
  unknown_fault: ["Papers on file", ""],
  error: ["Couldn't check", ""],
};
const KINDS = ["warranty", "insurance", "amc", "invoice", "manual", "other"];
const READ_BY: Record<string, string> = { claude: "read by Claude", rules: "read offline", none: "not read" };

export function PapersBlock({ id, tick }: { id: string; tick: number }) {
  const [en, setEn] = useState<DocsResp | null>(null);
  const [owner, setOwner] = useState<DocsResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState("");
  const [n, setN] = useState(0);
  const file = useRef<HTMLInputElement>(null);
  useEffect(() => {
    let on = true;
    Promise.all([get<DocsResp>(`/api/docs/${id}?lang=en`), get<DocsResp>(`/api/coverage/${id}`)])
      .then(([a, b]) => on && (setEn(a), setOwner(b), setErr(null)))
      .catch((e: Error) => on && setErr(e.message));
    return () => {
      on = false;
    };
  }, [id, tick, n]);

  async function upload(f: File) {
    setBusy(true);
    setErr(null);
    const body = new FormData();
    body.append("file", f);
    if (kind) body.append("kind", kind);
    try {
      const r = await fetch(`/api/docs/${id}`, { method: "POST", body });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail ?? r.statusText);
      setN((x) => x + 1);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
      if (file.current) file.current.value = "";
    }
  }

  async function remove(doc: string) {
    const r = await fetch(`/api/docs/${id}/${doc}`, { method: "DELETE" });
    if (!r.ok) setErr("couldn't remove the paper");
    setN((x) => x + 1);
  }

  if (!en) return err ? <p className="small muted">Couldn&rsquo;t load the papers: {err}.</p> : null;
  const cov = en.coverage;
  const [label, cls] = VERDICT[cov.verdict] ?? ["", ""];
  return (
    <section className="diag papers" aria-label="warranty and insurance">
      <div className="row between">
        <b className="small">Warranty &amp; insurance</b>
        <span className={`sev ${cls}`}>{label}</span>
      </div>
      {en.text && <p className="small">{en.text}</p>}
      {owner && owner.lang !== "en" && owner.text && (
        <p className="small wa-words" lang={owner.lang.replace("_latn", "")}>
          <span className="muted">On WhatsApp ({owner.lang}):</span> {owner.text}
        </p>
      )}
      {cov.saves_inr && cov.saves_inr[1] > 0 && (
        <p className="small mono">
          claim saves ≈ {inr(cov.saves_inr[0])}–{inr(cov.saves_inr[1])} of the repair
        </p>
      )}
      {en.documents.length > 0 && (
        <table className="kv small">
          <tbody>
            {en.documents.map((d) => (
              <tr key={d.id}>
                <th>{d.kind}</th>
                <td>
                  <a href={`/api/docs/${id}/${d.id}/file`} target="_blank" rel="noreferrer">
                    {d.fields.provider ?? d.filename}
                  </a>
                  {d.fields.number ? ` · ${d.fields.number}` : ""}
                  {d.fields.end ? ` · till ${d.fields.end}` : ""}
                  {d.fields.deductible_inr ? ` · deductible ${inr(d.fields.deductible_inr)}` : ""}
                  <span className="muted">
                    {" "}
                    · {READ_BY[d.read_by]}
                    {d.sample ? " · sample" : ""}
                  </span>{" "}
                  <button className="link" onClick={() => remove(d.id)} aria-label={`remove ${d.filename}`}>
                    remove
                  </button>
                  {d.warnings.map((w) => (
                    <span key={w} className="err small block">
                      {w}
                    </span>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="row">
        <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="paper type">
          <option value="">type: auto</option>
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <label className="upload">
          {busy ? "Reading…" : "Upload paper (PDF, photo, text)"}
          <input
            ref={file}
            type="file"
            accept=".pdf,.jpg,.jpeg,.png,.webp,.txt,.md"
            disabled={busy}
            onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
          />
        </label>
      </div>
      {err && <p className="err small">{err}</p>}
      <p className="small muted">
        The verdict is worked out in code from the papers&rsquo; own words (server/coverage.py). Confirm with
        the company before a repair.
      </p>
    </section>
  );
}
