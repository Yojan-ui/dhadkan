// DOM labels pinned to 3D points. Labels live in the normal React tree (an overlay clipped to the
// scene column, so they never cover the side panels); one projector inside the canvas moves them
// each frame by writing a transform straight to the element. Labels marked `occlude` fade out when
// part of the estate stands between the camera and their anchor.
import { useLayoutEffect, useRef, type CSSProperties, type ReactNode } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import * as THREE from "three";

interface Anchor {
  p: THREE.Vector3;
  el: HTMLElement;
  occlude: boolean;
  hidden: boolean;
}
const anchors = new Map<object, Anchor>();
/** Meshes that can hide labels (registered by the scene). */
export const occluders = new Set<THREE.Object3D>();

export function Label({
  at,
  children,
  className = "",
  style,
  occlude = false,
}: {
  at: THREE.Vector3 | [number, number, number];
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
  occlude?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [x, y, z] = Array.isArray(at) ? at : at.toArray();
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const key = {};
    anchors.set(key, { p: new THREE.Vector3(x, y, z), el, occlude, hidden: false });
    return () => {
      anchors.delete(key);
    };
  }, [x, y, z, occlude]);
  return (
    <div ref={ref} className={`pin ${className}`} style={{ ...style, visibility: "hidden" }}>
      {children}
    </div>
  );
}

export function LabelProjector() {
  const { camera, size } = useThree();
  const v = useRef(new THREE.Vector3()).current;
  const ray = useRef(new THREE.Raycaster()).current;
  const frame = useRef(0);
  useFrame(() => {
    const check = frame.current++ % 4 === 0; // occlusion every 4th frame is plenty
    for (const a of anchors.values()) {
      v.copy(a.p).project(camera);
      const off = v.z > 1 || Math.abs(v.x) > 1.15 || Math.abs(v.y) > 1.15;
      a.el.style.visibility = off ? "hidden" : "visible";
      if (off) continue;
      a.el.style.transform = `translate(${((v.x + 1) / 2) * size.width}px, ${((1 - v.y) / 2) * size.height}px) translate(-50%, -100%)`;
      if (a.occlude && check && occluders.size) {
        const dir = a.p.clone().sub(camera.position);
        const dist = dir.length();
        ray.set(camera.position, dir.normalize());
        ray.far = dist - 0.3;
        a.hidden = ray.intersectObjects([...occluders], false).length > 0;
      }
      a.el.style.opacity = a.hidden ? "0" : "1";
    }
  });
  return null;
}
