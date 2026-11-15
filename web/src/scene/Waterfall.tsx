// Live waterfall: mel frequency (x) x time (z, newest at the front) x level (height).
// Each row is one 0.5 s slice of the reading the model scored, normalised against the
// model's training normal. Bands whose reconstruction residual sits >3 std above this
// machine's own normal (band_z from the latest reading) are drawn in the accent colour.
import { useEffect, useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { HISTORY, store, type Machine } from "../api";
import { C } from "../theme";
import { Label } from "./labels";

export const W = 2.8,
  D = 2.2;
const HMAX = 0.5,
  COLS = 64;

export function Waterfall({
  machine,
  at,
  reduced,
}: {
  machine: Machine;
  at: THREE.Vector3;
  reduced: boolean;
}) {
  const group = useRef<THREE.Group>(null);
  const seen = useRef(0);
  const geo = useMemo(() => {
    const g = new THREE.PlaneGeometry(W, D, COLS - 1, HISTORY - 1);
    g.rotateX(-Math.PI / 2);
    g.setAttribute("color", new THREE.BufferAttribute(new Float32Array(COLS * HISTORY * 3), 3));
    return g;
  }, []);
  useEffect(() => () => geo.dispose(), [geo]);
  const mat = useMemo(() => new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide }), []);
  const hot = useMemo(() => (machine.band_z ?? []).map((z) => z > 3), [machine.band_z]);

  const lo = useMemo(() => new THREE.Color("#25272b"), []);
  const hi = useMemo(() => new THREE.Color("#a3a7ae"), []);
  const hotLo = useMemo(() => new THREE.Color("#4a2410"), []);
  const hotHi = useMemo(() => new THREE.Color(C.accent), []);
  const tmp = useMemo(() => new THREE.Color(), []);

  useFrame(() => {
    const g = group.current;
    if (!g) return;
    // smooth scroll between 2 Hz frames
    const since = (performance.now() - store.frameAt) / 500;
    g.position.z = at.z - (reduced ? 0 : Math.min(1, since) * (D / (HISTORY - 1))); // drift back one row, then the rows shift
    if (store.frameAt === seen.current) return;
    seen.current = store.frameAt;
    const rows = store.spec[machine.id] ?? [];
    const pos = geo.getAttribute("position") as THREE.BufferAttribute;
    const col = geo.getAttribute("color") as THREE.BufferAttribute;
    for (let r = 0; r < HISTORY; r++) {
      // plane row 0 is the far edge (oldest); the last row is now
      const src = rows[rows.length - HISTORY + r];
      for (let c = 0; c < COLS; c++) {
        const i = r * COLS + c;
        const v = src ? src[c] : 0;
        pos.setY(i, src ? Math.pow(v, 1.6) * HMAX : 0);
        tmp.copy(hot[c] ? hotLo : lo).lerp(hot[c] ? hotHi : hi, src ? Math.min(1, v * 1.15) : 0);
        col.setXYZ(i, tmp.r, tmp.g, tmp.b);
      }
    }
    pos.needsUpdate = col.needsUpdate = true;
  });

  return (
    <group ref={group} position={[at.x, at.y, at.z]}>
      <mesh geometry={geo} material={mat} />
    </group>
  );
}

/** Axis labels for the DOM overlay. */
export function WaterfallLabels({ at, hz }: { at: THREE.Vector3; hz: number[] }) {
  const tick = (f: number) => (f >= 1000 ? `${(f / 1000).toFixed(f >= 10000 ? 0 : 1)}k` : `${Math.round(f)}`);
  return (
    <>
      {[0, 21, 42, 63].map((c) => (
        <Label key={c} at={[at.x - W / 2 + (c / 63) * W, at.y, at.z + D / 2 + 0.18]}>
          <div className="axis mono">{tick(hz[c] ?? 0)} Hz</div>
        </Label>
      ))}
      <Label at={[at.x, at.y, at.z + D / 2 + 0.5]}>
        <div className="axis">pitch, low to high (mel scale) · front row = now · back = 30 s ago</div>
      </Label>
    </>
  );
}
