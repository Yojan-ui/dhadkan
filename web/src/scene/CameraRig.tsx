// Camera: 800 ms eased flights between views, then a slow orbit (a gentle sway around the estate,
// a full turn around a single machine). Any drag pauses it; it eases back after 8 s idle.
import { useEffect, useMemo, useRef } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import * as THREE from "three";
import type { OrbitControls as OC } from "three-stdlib";

const FLY_S = 0.8;
const IDLE_S = 8;
const SWAY = 0.22; // radians either side of the framed view
const SWAY_PERIOD = 60; // seconds
const ORBIT_SPEED = 0.12; // rad/s around a machine

export type View = { kind: "cluster" } | { kind: "shed"; factory: string } | { kind: "machine"; id: string };
export interface Goal {
  pos: THREE.Vector3;
  target: THREE.Vector3;
}

const easeInOut = (u: number) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2);

export function CameraRig({ goal, view, reduced }: { goal: Goal; view: View; reduced: boolean }) {
  const { camera, controls } = useThree() as unknown as {
    camera: THREE.PerspectiveCamera;
    controls: OC | null;
  };
  const fly = useRef<{ t0: number; p0: THREE.Vector3; q0: THREE.Vector3 } | null>(null);
  const user = useRef<{ until: number; active: boolean }>({ until: -1, active: false });
  const phase = useRef(0); // orbit time accumulated while not paused
  const key = useMemo(() => JSON.stringify(view), [view]);

  useEffect(() => {
    fly.current = null; // start a flight on the next frame, from wherever the camera is
    phase.current = 0;
    user.current = { until: -1, active: false };
  }, [key, goal]);
  useEffect(() => {
    if (!controls) return;
    controls.autoRotate = false;
    const start = () => {
      user.current = { until: Infinity, active: true };
    };
    const end = () => {
      user.current = { until: performance.now() / 1000 + IDLE_S, active: false };
    };
    controls.addEventListener("start", start);
    controls.addEventListener("end", end);
    return () => {
      controls.removeEventListener("start", start);
      controls.removeEventListener("end", end);
    };
  }, [controls]);

  useFrame((_, dt) => {
    if (!controls) return;
    const now = performance.now() / 1000;
    const u = user.current;
    if (u.active || now < u.until) return; // the user is looking around
    if (u.until > 0) {
      u.until = -1; // idle again: fly back to the framed view
      fly.current = null;
      phase.current = 0;
    }
    if (!reduced) phase.current += dt;
    const angle =
      view.kind === "machine"
        ? phase.current * ORBIT_SPEED
        : SWAY * Math.sin((phase.current / SWAY_PERIOD) * 2 * Math.PI);
    const off = goal.pos
      .clone()
      .sub(goal.target)
      .applyAxisAngle(new THREE.Vector3(0, 1, 0), reduced ? 0 : angle);
    const want = goal.target.clone().add(off);
    if (fly.current === null)
      fly.current = { t0: now, p0: camera.position.clone(), q0: controls.target.clone() };
    const f = fly.current;
    const e = reduced ? 1 : easeInOut(Math.min(1, (now - f.t0) / FLY_S));
    camera.position.lerpVectors(f.p0, want, e);
    controls.target.lerpVectors(f.q0, goal.target, e);
    controls.update();
  });
  return null;
}
