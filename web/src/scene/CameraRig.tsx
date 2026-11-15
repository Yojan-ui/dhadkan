// Smooth camera moves between cluster, shed and machine views. The user can drag at
// any time; dragging cancels the move. Machine view orbits slowly (not with reduced motion).
import { useEffect, useMemo, useRef } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import * as THREE from "three";
import type { OrbitControls as OC } from "three-stdlib";

const TWEEN_S = 0.3;

export type View = { kind: "cluster" } | { kind: "shed"; factory: string } | { kind: "machine"; id: string };

export function CameraRig({
  goal,
  view,
  reduced,
}: {
  goal: { pos: THREE.Vector3; target: THREE.Vector3 };
  view: View;
  reduced: boolean;
}) {
  const { camera, controls } = useThree() as unknown as {
    camera: THREE.PerspectiveCamera;
    controls: OC | null;
  };
  // a 300 ms ease-out tween from wherever the camera is to the goal
  const tween = useRef<{ t0: number; p0: THREE.Vector3; q0: THREE.Vector3 } | null>(null);
  const key = useMemo(() => JSON.stringify(view), [view]);

  useEffect(() => {
    tween.current = null; // start on the next frame, from the current pose
  }, [key, goal]);
  useEffect(() => {
    if (!controls) return;
    const stop = () => {
      tween.current = { t0: -1, p0: camera.position.clone(), q0: controls.target.clone() };
    };
    controls.addEventListener("start", stop);
    return () => controls.removeEventListener("start", stop);
  }, [controls, camera]);
  useEffect(() => {
    if (!controls) return;
    controls.autoRotate = view.kind === "machine" && !reduced;
    controls.autoRotateSpeed = 0.5;
  }, [controls, view.kind, reduced]);

  useFrame(({ clock }) => {
    if (!controls) return;
    const now = clock.elapsedTime;
    if (tween.current === null)
      tween.current = { t0: now, p0: camera.position.clone(), q0: controls.target.clone() };
    const tw = tween.current;
    if (tw.t0 < 0) return; // the user took over
    const u = reduced ? 1 : Math.min(1, (now - tw.t0) / TWEEN_S);
    const e = 1 - Math.pow(1 - u, 3);
    if (u >= 1 && camera.position.distanceTo(goal.pos) < 1e-3) return;
    camera.position.lerpVectors(tw.p0, goal.pos, e);
    controls.target.lerpVectors(tw.q0, goal.target, e);
    controls.update();
    if (u >= 1) tw.t0 = -1;
  });
  return null;
}
