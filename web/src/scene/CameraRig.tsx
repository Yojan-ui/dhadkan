// Smooth camera moves between cluster, shed and machine views. The user can drag at
// any time; dragging cancels the move. Machine view orbits slowly (not with reduced motion).
import { useEffect, useMemo, useRef } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import * as THREE from "three";
import type { OrbitControls as OC } from "three-stdlib";

export type View = { kind: "cluster" } | { kind: "shed"; factory: string } | { kind: "machine"; id: string };

export function CameraRig({ goal, view, reduced }: { goal: { pos: THREE.Vector3; target: THREE.Vector3 }; view: View; reduced: boolean }) {
  const { camera, controls } = useThree() as unknown as { camera: THREE.PerspectiveCamera; controls: OC | null };
  const moving = useRef(true);
  const key = useMemo(() => JSON.stringify(view), [view]);

  useEffect(() => { moving.current = true; }, [key]);
  useEffect(() => {
    if (!controls) return;
    const stop = () => { moving.current = false; };
    controls.addEventListener("start", stop);
    return () => controls.removeEventListener("start", stop);
  }, [controls]);
  useEffect(() => {
    if (!controls) return;
    controls.autoRotate = view.kind === "machine" && !reduced;
    controls.autoRotateSpeed = 0.5;
  }, [controls, view.kind, reduced]);

  useFrame((_, dt) => {
    if (!controls || !moving.current) return;
    const k = reduced ? 1 : 1 - Math.exp(-dt * 2.6);
    camera.position.lerp(goal.pos, k);
    controls.target.lerp(goal.target, k);
    controls.update();
    if (camera.position.distanceTo(goal.pos) < 0.02 && controls.target.distanceTo(goal.target) < 0.02) moving.current = false;
  });
  return null;
}
