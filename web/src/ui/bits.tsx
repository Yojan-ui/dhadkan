// Small 2D pieces shared by the desktop overlay and phone mode.
import { useEffect, useRef } from "react";
import { HISTORY, store, type Machine } from "../api";
import { C, fill, ringCSS } from "../theme";

/** Same ring as on the 3D floor: fills clockwise toward this machine's alarm level. */
export function HealthRing({ m, size = 34 }: { m: Machine; size?: number }) {
  const r = size / 2 - 3,
    c = 2 * Math.PI * r,
    f = fill(m);
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      aria-label={`${Math.round(f * 100)}% of alarm level`}
    >
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={C.track} strokeWidth={3} />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={ringCSS(m)}
        strokeWidth={3}
        strokeDasharray={`${f * c} ${c}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        strokeLinecap="butt"
      />
    </svg>
  );
}

export function Spectro2D({
  m,
  height = 110,
  hotBands = null,
}: {
  m: Machine;
  height?: number;
  hotBands?: [number, number] | null;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(
    () =>
      store.subscribe(() => {
        const cv = ref.current;
        if (!cv) return;
        const g = cv.getContext("2d");
        if (!g) return;
        const rows = store.spec[m.id] ?? [];
        const W = HISTORY,
          H = 64;
        const img = g.createImageData(W, H);
        const hot = hotBands
          ? Array.from({ length: H }, (_, i) => i >= hotBands[0] && i <= hotBands[1])
          : (m.band_z ?? []).map((z) => z > 3);
        for (let t = 0; t < W; t++) {
          const col = rows[rows.length - W + t];
          for (let f = 0; f < H; f++) {
            const v = col ? col[f] : 0;
            const i = ((H - 1 - f) * W + t) * 4;
            const [r, gg, b] = hot[f]
              ? [37 + v * 218, 36 + v * 70, 35]
              : [37 + v * 160, 39 + v * 158, 43 + v * 160];
            img.data[i] = r;
            img.data[i + 1] = gg;
            img.data[i + 2] = b;
            img.data[i + 3] = 255;
          }
        }
        g.putImageData(img, 0, 0);
      }),
    [m.id, m.band_z, hotBands],
  );
  return (
    <canvas
      ref={ref}
      width={HISTORY}
      height={64}
      className="spectro"
      style={{ height }}
      aria-label="live spectrogram"
    />
  );
}
