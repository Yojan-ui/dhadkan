// ?view=phones: the owner's and the mechanic's WhatsApp side by side, for a demo without phones.
import { useMeta } from "../api";
import { WaPhones } from "../ui/WaPhones";

export function PhonesScreen() {
  const meta = useMeta();
  return (
    <main className="wa-screen">
      <h1>WhatsApp, both sides</h1>
      {meta ? <WaPhones factories={meta.factories} /> : <p className="muted">…</p>}
    </main>
  );
}
