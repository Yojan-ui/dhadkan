// DOM labels pinned to 3D points. Labels live in the normal React tree (an overlay on
// top of the canvas); one projector inside the canvas moves them each frame by writing
// a transform straight to the element, so nothing re-renders at 60 fps.
import { useLayoutEffect, useRef, type CSSProperties, type ReactNode } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import * as THREE from "three";

const anchors = new Map<object, { p: THREE.Vector3; el: HTMLElement }>();

export function Label({
  at,
  children,
  className = "",
  style,
}: {
  at: THREE.Vector3 | [number, number, number];
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [x, y, z] = Array.isArray(at) ? at : at.toArray();
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const key = {};
    anchors.set(key, { p: new THREE.Vector3(x, y, z), el });
    return () => {
      anchors.delete(key);
    };
  }, [x, y, z]);
  return (
    <div ref={ref} className={`pin ${className}`} style={{ ...style, visibility: "hidden" }}>
      {children}
    </div>
  );
}

export function LabelProjector() {
  const { camera, size } = useThree();
  const v = new THREE.Vector3();
  useFrame(() => {
    for (const { p, el } of anchors.values()) {
      v.copy(p).project(camera);
      const off = v.z > 1 || Math.abs(v.x) > 1.2 || Math.abs(v.y) > 1.2;
      el.style.visibility = off ? "hidden" : "visible";
      if (!off)
        el.style.transform = `translate(${((v.x + 1) / 2) * size.width}px, ${((1 - v.y) / 2) * size.height}px) translate(-50%, -50%)`;
    }
  });
  return null;
}
