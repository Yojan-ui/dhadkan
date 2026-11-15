// When a fault is confirmed, arcs from that machine to every same-type machine in the
// other sheds, labelled with the registry's actual cosine vs that machine's flag level.
// A machine whose pattern matches pulses once. Later early matches draw their own arc.
import { useEffect, useMemo, useState } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { store, type LiveEvent, type Machine } from "../api";
import { C } from "../theme";
import { ease, pulse, useKeyed } from "./fx";
import { Label } from "./labels";
import { TOP } from "./layout";

export interface ArcSpec {
  persistent?: boolean; // a standing registry match (drawn while it lasts)
  key: string;
  from: string;
  to: string;
  match: boolean;
  text: string;
  born: number;
}

export const DRAW_S = 0.3;
const HOLD_S = 7,
  FADE_S = 1.2;

/** Arc list driven by registry events (lives in the DOM tree so labels can render there). */
export function useArcs() {
  const [arcs, setArcs] = useState<ArcSpec[]>([]);
  useEffect(
    () =>
      store.onEvent((e: LiveEvent) => {
        const now = performance.now() / 1000;
        if (e.kind === "published") {
          const checks = e.checks as {
            machine: string;
            cosine: number;
            level: number;
            match: boolean;
            same_model: boolean;
          }[];
          const add = checks.map((c, i) => ({
            key: `${e.seq}-${c.machine}`,
            from: e.machine as string,
            to: c.machine,
            match: c.match,
            born: now + i * 0.15,
            text: `${c.match ? "matches" : "no match"} · cos ${c.cosine.toFixed(2)} ${c.match ? ">" : "<"} ${c.level.toFixed(2)}${c.same_model ? " · same model" : ""}`,
          }));
          setArcs((a) => [...a, ...add]);
          add.forEach((a) => a.match && setTimeout(() => pulse(a.to), (a.born - now + DRAW_S) * 1000));
        }
        if (e.kind === "early_match" && e.source) {
          const a = {
            key: `${e.seq}`,
            from: e.source as string,
            to: e.machine as string,
            match: true,
            born: now,
            text: `early match · cos ${(e.cosine as number).toFixed(2)} > ${(e.level as number).toFixed(2)} · ${e.fault}`,
          };
          setArcs((x) => [...x, a]);
          setTimeout(() => pulse(a.to), DRAW_S * 1000);
        }
        if (e.kind === "reset") setArcs([]);
      }),
    [],
  );
  useEffect(() => {
    if (!arcs.length) return;
    const id = setInterval(() => {
      const now = performance.now() / 1000;
      setArcs((a) => a.filter((x) => now - x.born < DRAW_S + HOLD_S + FADE_S));
    }, 1000);
    return () => clearInterval(id);
  }, [arcs.length]);
  return arcs;
}

const top = (m: Machine, p: THREE.Vector3) => p.clone().setY(TOP[m.shape]);

export function Contagion({
  arcs,
  machines,
  pos,
  reduced,
}: {
  arcs: ArcSpec[];
  machines: Machine[];
  pos: Record<string, THREE.Vector3>;
  reduced: boolean;
}) {
  const byId = useMemo(() => Object.fromEntries(machines.map((m) => [m.id, m])), [machines]);
  const standing: ArcSpec[] = machines
    .filter((m) => m.status === "early_match" && m.registry?.from_machine)
    .map((m) => ({
      key: `match-${m.registry?.from_machine}-${m.id}`,
      from: m.registry?.from_machine ?? "",
      to: m.id,
      match: true,
      text: "",
      born: 0,
      persistent: true,
    }));
  return (
    <group>
      {[...standing, ...arcs].map(
        (a) =>
          byId[a.from] &&
          byId[a.to] &&
          pos[a.from] &&
          pos[a.to] && (
            <Arc
              key={a.key}
              spec={a}
              a={top(byId[a.from], pos[a.from])}
              b={top(byId[a.to], pos[a.to])}
              reduced={reduced}
            />
          ),
      )}
    </group>
  );
}

/** Arc end labels, for the DOM overlay. They fade in once the arc has been drawn. */
export function ArcLabels({
  arcs,
  machines,
  pos,
  reduced,
}: {
  arcs: ArcSpec[];
  machines: Machine[];
  pos: Record<string, THREE.Vector3>;
  reduced: boolean;
}) {
  const now = performance.now() / 1000;
  return (
    <>
      {arcs.map((a, i) => {
        const m = machines.find((x) => x.id === a.to);
        if (!m || !pos[a.to]) return null;
        const delay = reduced ? 0 : Math.max(0, a.born + DRAW_S - now);
        // labels for machines in the same shed stack instead of overlapping
        const stack = arcs
          .slice(0, i)
          .filter((b) => machines.find((x) => x.id === b.to)?.factory === m.factory).length;
        return (
          <Label key={a.key} at={top(m, pos[a.to]).add(new THREE.Vector3(0, 0.55 + stack * 0.45, 0))}>
            <div
              className={`tag mono appear ${a.match ? "tag-hot" : ""}`}
              style={{ animationDelay: `${delay}s` }}
            >
              {a.text}
            </div>
          </Label>
        );
      })}
    </>
  );
}

const DASH_VS = /* glsl */ `
varying vec2 vUv;
void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const DASH_FS = /* glsl */ `
uniform vec3 uColor; uniform float uTime; uniform float uDraw; uniform float uOpacity;
varying vec2 vUv;
void main() {
  if (vUv.x > uDraw) discard;                                   // drawn from source to target
  if (fract(vUv.x * 28.0 - uTime * 1.2) > 0.55) discard;       // dashes marching toward the target
  gl_FragColor = vec4(uColor, uOpacity);
}`;

function Arc({
  spec,
  a,
  b,
  reduced,
}: {
  spec: ArcSpec;
  a: THREE.Vector3;
  b: THREE.Vector3;
  reduced: boolean;
}) {
  const geo = useKeyed(`${a.toArray()}|${b.toArray()}|${spec.match}`, () => {
    const mid = a
      .clone()
      .lerp(b, 0.5)
      .setY(Math.max(a.y, b.y) + a.distanceTo(b) * 0.3 + 0.6);
    return new THREE.TubeGeometry(
      new THREE.QuadraticBezierCurve3(a, mid, b),
      96,
      spec.match ? 0.05 : 0.03,
      6,
      false,
    );
  });
  useEffect(() => () => geo.dispose(), [geo]);
  const mat = useMemo(
    () =>
      new THREE.ShaderMaterial({
        vertexShader: DASH_VS,
        fragmentShader: DASH_FS,
        transparent: true,
        depthWrite: false,
        toneMapped: false,
        uniforms: {
          uColor: { value: new THREE.Color(spec.match ? C.accent : C.grey) },
          uTime: { value: 0 },
          uDraw: { value: 0 },
          uOpacity: { value: 1 },
        },
      }),
    [spec.match],
  );
  useFrame(({ clock }) => {
    const t = performance.now() / 1000 - spec.born;
    mat.uniforms.uTime.value = reduced ? 0 : clock.elapsedTime;
    mat.uniforms.uDraw.value = spec.persistent || reduced ? 1 : ease(t / DRAW_S);
    mat.uniforms.uOpacity.value = spec.persistent
      ? 0.9
      : t < 0 && !reduced
        ? 0
        : t > DRAW_S + HOLD_S
          ? Math.max(0, 1 - (t - DRAW_S - HOLD_S) / FADE_S)
          : 1;
  });
  return <mesh geometry={geo} material={mat} />;
}
