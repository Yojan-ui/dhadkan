// The technician's route from the agent's plan_technician_route call: base -> sheds in
// order -> base, drawn leg by leg with arrival times from route.timeline. After the
// owner approves, a marker drives it.
import { useEffect, useMemo, useRef, useState } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { store, type Factory, type LiveEvent, type Machine, type Route as R } from "../api";
import { C, fmtClock, inr } from "../theme";
import { ease, useKeyed } from "./fx";
import { Label } from "./labels";
import { SHED, world } from "./layout";

const LEG_S = 0.3; // seconds to draw one leg (legs draw one after another)
const Y = 0.2;

interface Stop {
  point: THREE.Vector3;
  label: string;
  hot: boolean;
}
export interface RoutePlan {
  stops: Stop[];
  path: THREE.CurvePath<THREE.Vector3>;
  at: number[];
  born: number;
  drive: number | null;
}

/** Route geometry and animation clock, shared by the 3D path and the DOM labels. */
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
    const base = world(route.base_xy).setY(Y);
    const out: Stop[] = [
      {
        point: base,
        label: `${route.technician.split(" ")[0]} · base · ${route.weekday} ${dayStart}`,
        hot: false,
      },
    ];
    const byF: Record<string, Factory> = Object.fromEntries(factories.map((f) => [f.id, f]));
    route.legs.forEach((l, i) => {
      const m = machines.find((x) => x.id === l.machine);
      const door = world(byF[l.factory].xy_km).add(new THREE.Vector3(0, Y, SHED.d / 2 + 0.45));
      const risk = m && m.rupees_at_risk > 0 ? ` · ${inr(m.rupees_at_risk)} at risk` : "";
      const text = `${i + 1} · ${m?.name ?? l.machine} (${l.machine}) · ${fmtClock(l.arrive)}–${fmtClock(l.leave, false)}${risk}`;
      const prev = out[out.length - 1];
      if (prev.point.distanceTo(door) < 0.01) prev.label += `\n${text}`;
      else out.push({ point: door, label: text, hot: m?.status === "early_match" });
    });
    out.push({ point: base.clone(), label: "", hot: false });
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
  const [born, setBorn] = useState(() => performance.now() / 1000 - 99); // already-drawn on first load
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
      {plan.stops.slice(0, -1).map(
        (s, i) =>
          s.label && (
            <Label key={`${i}-${s.label}`} at={s.point.clone().setY(s.point.y + 0.25)}>
              <div
                className={`tag mono appear ${s.hot ? "tag-hot" : ""}`}
                style={{
                  whiteSpace: "pre",
                  animationDelay: `${reduced ? 0 : Math.max(0, plan.born + plan.at[i] * LEG_S * legs - now)}s`,
                }}
              >
                {s.label}
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
      new THREE.TubeGeometry(path as unknown as THREE.Curve<THREE.Vector3>, 60 * legs, 0.05, 6, false),
      new THREE.TubeGeometry(path as unknown as THREE.Curve<THREE.Vector3>, 60 * legs, 0.17, 8, false),
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
  const coreMat = useMemo(() => new THREE.MeshBasicMaterial({ color: C.accent }), []);
  const glowMat = useMemo(
    () =>
      new THREE.MeshBasicMaterial({ color: C.accent, transparent: true, opacity: 0.16, depthWrite: false }),
    [],
  );
  const marker = useRef<THREE.Mesh>(null);

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
    // parked at base; after approval it drives the route once (pausing at stops) and stays at the last stop
    let u = 0;
    if (drive != null) {
      const lastStop = at[at.length - 2];
      u = lastStop;
      if (!reduced) {
        let t = now - drive;
        for (let i = 0; i < legs - 1; i++) {
          const dur = 0.3,
            pause = 0.25;
          if (t < dur) {
            u = at[i] + (at[i + 1] - at[i]) * ease(t / dur);
            break;
          }
          t -= dur;
          if (t < pause) {
            u = at[i + 1];
            break;
          }
          t -= pause;
        }
      }
    }
    mk.position.copy(path.getPointAt(Math.min(1, Math.max(0, u)))).setY(0.38);
  });

  return (
    <group>
      <mesh geometry={glow} material={glowMat} renderOrder={1} />
      <mesh geometry={core} material={coreMat} />
      <mesh ref={marker} castShadow>
        <coneGeometry args={[0.2, 0.5, 4]} />
        <meshStandardMaterial color={C.ink} roughness={0.5} />
      </mesh>
    </group>
  );
}
