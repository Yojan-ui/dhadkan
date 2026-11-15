// The technician's route from the agent's plan_technician_route call, drawn on the estate road:
// base -> each shed's gate in visit order -> back to base in the other lane, with numbered stops
// and arrival times from route.timeline. After the owner approves, a marker drives it.
import { useEffect, useMemo, useRef, useState } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { store, type Factory, type LiveEvent, type Machine, type Route as R } from "../api";
import { C, fmtClock, inr } from "../theme";
import { ease, useKeyed } from "./fx";
import { Label } from "./labels";
import { basePoint, roadPoint, shedCenter } from "./layout";

const LEG_S = 0.3; // seconds to draw one leg (legs draw one after another)
const OUT_LANE = -0.35,
  BACK_LANE = 0.4;

interface Stop {
  point: THREE.Vector3;
  label: string; // empty for the turning points
  num: number | null;
  hot: boolean;
}
export interface RoutePlan {
  stops: Stop[];
  path: THREE.CurvePath<THREE.Vector3>;
  at: number[]; // fraction of the path at each point
  born: number;
  drive: number | null;
}

export function useRoute(
  route: R | null,
  factories: Factory[],
  machines: Machine[],
  dayStart: string,
  reduced: boolean,
): RoutePlan | null {
  const key = `${route?.planned_at}|${route?.stops.join()}|${machines.map((m) => m.status + m.rupees_at_risk).join()}|${dayStart}`;
  const stops = useKeyed<Stop[]>(key, () => {
    if (!route || !route.legs.length) return [];
    const n = factories.length;
    const idx = Object.fromEntries(factories.map((f, i) => [f.id, i]));
    const base = basePoint(n);
    const out: Stop[] = [
      {
        point: base.clone().setZ(base.z + OUT_LANE),
        label: `${route.technician.split(" ")[0]} leaves base ${dayStart}`,
        num: null,
        hot: false,
      },
    ];
    let num = 0;
    route.legs.forEach((l) => {
      const m = machines.find((x) => x.id === l.machine);
      const gate = roadPoint(shedCenter(idx[l.factory] ?? 0, n).x, OUT_LANE);
      const risk = m && m.rupees_at_risk > 0 ? ` · ${inr(m.rupees_at_risk)}` : "";
      const text = `${m?.name ?? l.machine} · ${fmtClock(l.arrive)}${risk}`;
      const prev = out[out.length - 1];
      if (prev.num !== null && prev.point.distanceTo(gate) < 0.01) prev.label += `\n${text}`;
      else out.push({ point: gate, label: text, num: ++num, hot: m?.status === "early_match" });
    });
    const last = out[out.length - 1].point;
    out.push({ point: last.clone().setZ(base.z + BACK_LANE), label: "", num: null, hot: false });
    out.push({ point: base.clone().setZ(base.z + BACK_LANE), label: "", num: null, hot: false });
    return out;
  });

  const path = useMemo(() => {
    const cp = new THREE.CurvePath<THREE.Vector3>();
    for (let i = 1; i < stops.length; i++) cp.add(new THREE.LineCurve3(stops[i - 1].point, stops[i].point));
    return cp;
  }, [stops]);
  const at = useMemo(() => {
    if (stops.length < 2) return [];
    const L = path.getCurveLengths();
    const tot = L[L.length - 1] || 1;
    return [0, ...L.map((l) => l / tot)];
  }, [path, stops.length]);

  const id = `${route?.planned_at}|${route?.stops.join()}`;
  const [born, setBorn] = useState(() => performance.now() / 1000 - 99); // already drawn on first load
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setBorn(performance.now() / 1000 - (reduced ? 99 : 0));
  }, [id, reduced]);
  const [drive, setDrive] = useState<number | null>(null);
  useEffect(
    () =>
      store.onEvent((e: LiveEvent) => {
        if (e.kind === "reply" && e.approved === true) setDrive(performance.now() / 1000);
        if (e.kind === "reset") setDrive(null);
      }),
    [],
  );
  return stops.length < 2 ? null : { stops, path, at, born, drive };
}

export function RouteLabels({ plan, reduced }: { plan: RoutePlan; reduced: boolean }) {
  const legs = plan.stops.length - 1;
  const now = performance.now() / 1000;
  return (
    <>
      {plan.stops.map(
        (s, i) =>
          s.label && (
            <Label
              key={`${i}-${s.label}`}
              at={
                s.num === null
                  ? s.point
                      .clone()
                      .setY(0.05)
                      .setZ(s.point.z + 1.5)
                  : s.point.clone().setY(0.35)
              }
            >
              <div
                className={`stop appear ${s.hot ? "stop-hot" : ""}`}
                style={{
                  animationDelay: `${reduced ? 0 : Math.max(0, plan.born + plan.at[i] * LEG_S * legs - now)}s`,
                }}
              >
                {s.num !== null && <span className="stop-num mono">{s.num}</span>}
                <span className="mono" style={{ whiteSpace: "pre" }}>
                  {s.label}
                </span>
              </div>
            </Label>
          ),
      )}
    </>
  );
}

export function RouteMesh({ plan, reduced }: { plan: RoutePlan; reduced: boolean }) {
  const { stops, path, at, born, drive } = plan;
  const legs = stops.length - 1;
  const [core, glow] = useMemo(
    () => [
      new THREE.TubeGeometry(path as unknown as THREE.Curve<THREE.Vector3>, 60 * legs, 0.045, 6, false),
      new THREE.TubeGeometry(path as unknown as THREE.Curve<THREE.Vector3>, 60 * legs, 0.16, 8, false),
    ],
    [path, legs],
  );
  useEffect(
    () => () => {
      core.dispose();
      glow.dispose();
    },
    [core, glow],
  );
  const coreMat = useMemo(() => new THREE.MeshBasicMaterial({ color: C.accent, toneMapped: false }), []);
  const glowMat = useMemo(
    () =>
      new THREE.MeshBasicMaterial({
        color: C.accent,
        transparent: true,
        opacity: 0.2,
        depthWrite: false,
        toneMapped: false,
      }),
    [],
  );
  const marker = useRef<THREE.Group>(null);
  const numbered = stops.map((s, i) => (s.num !== null ? i : -1)).filter((i) => i >= 0);

  useFrame(() => {
    const now = performance.now() / 1000;
    const prog = reduced ? 1 : Math.min(1, (now - born) / (LEG_S * legs));
    for (const [g, radial] of [
      [core, 6],
      [glow, 8],
    ] as const) {
      const per = radial * 6;
      g.setDrawRange(0, Math.floor(prog * ((g.index?.count ?? 0) / per)) * per);
    }
    const mk = marker.current;
    if (!mk) return;
    // parked at base; after approval it drives to each stop in turn (pausing there) and stays at the last
    let u = 0;
    if (drive != null) {
      u = at[numbered[numbered.length - 1] ?? 0];
      if (!reduced) {
        let t = now - drive;
        let from = 0;
        for (const i of numbered) {
          const dur = 0.3,
            pause = 0.25;
          if (t < dur) {
            u = at[from] + (at[i] - at[from]) * ease(t / dur);
            break;
          }
          t -= dur;
          if (t < pause) {
            u = at[i];
            break;
          }
          t -= pause;
          from = i;
        }
      }
    }
    mk.position.copy(path.getPointAt(Math.min(1, Math.max(0, u))));
  });

  return (
    <group>
      <mesh geometry={glow} material={glowMat} renderOrder={1} />
      <mesh geometry={core} material={coreMat} />
      <instancedMesh
        args={[undefined, undefined, Math.max(1, numbered.length)]}
        ref={(mesh) => {
          if (!mesh) return;
          numbered.forEach((si, k) => {
            const pt = stops[si].point;
            mesh.setMatrixAt(
              k,
              new THREE.Matrix4().compose(
                new THREE.Vector3(pt.x, 0.06, pt.z),
                new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2),
                new THREE.Vector3(1, 1, 1),
              ),
            );
          });
          mesh.count = numbered.length;
          mesh.instanceMatrix.needsUpdate = true;
          mesh.computeBoundingSphere();
        }}
      >
        <circleGeometry args={[0.26, 24]} />
        <meshBasicMaterial color={C.accent} toneMapped={false} />
      </instancedMesh>
      <group ref={marker}>
        <mesh position={[0, 0.3, 0]} castShadow>
          <boxGeometry args={[0.5, 0.28, 0.3]} />
          <meshStandardMaterial color="#E9E7E2" roughness={0.5} />
        </mesh>
        <mesh position={[0, 0.5, 0]}>
          <boxGeometry args={[0.28, 0.14, 0.26]} />
          <meshStandardMaterial color={C.accent} roughness={0.5} />
        </mesh>
      </group>
    </group>
  );
}
