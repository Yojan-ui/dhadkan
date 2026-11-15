// All machines and shed structure as three instanced meshes (box, cylinder, cone),
// plus an invisible instanced hit box per machine. Each machine shakes in proportion
// to the live anomaly score of the 1 s window under the play cursor.
import { useLayoutEffect, useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { store, type Factory, type Machine } from "../api";
import { C } from "../theme";
import { useKeyed } from "./fx";
import { SHAPES, SHED, partMatrix, world, type Geo, type Part } from "./layout";

interface Inst {
  mi: number;
  part: Part;
  base: THREE.Vector3;
  color: THREE.Color;
}

const GEOS: Record<Geo, THREE.BufferGeometry> = {
  box: new THREE.BoxGeometry(1, 1, 1),
  cyl: new THREE.CylinderGeometry(0.5, 0.5, 1, 18),
  cone: new THREE.CylinderGeometry(0.5, 0.18, 1, 14),
};
const PART_COLORS = [C.partA, C.partB, C.partC].map((c) => new THREE.Color(c));

function shedParts(f: Factory): Inst[] {
  const c = world(f.xy_km);
  const { w, d, wall } = SHED;
  const mk = (p: [number, number, number], s: [number, number, number], col: string): Inst => ({
    mi: -1,
    part: { g: "box", p, s, c: 0 },
    base: c,
    color: new THREE.Color(col),
  });
  const post = (x: number, z: number) => mk([x, 0.7, z], [0.12, 1.4, 0.12], C.wall);
  return [
    mk([0, 0.06, 0], [w, 0.12, d], C.slab),
    mk([0, 0.12 + wall / 2, -d / 2], [w, wall, 0.1], C.wall),
    mk([-w / 2, 0.12 + wall / 2, 0], [0.1, wall, d], C.wall),
    post(-w / 2, -d / 2),
    post(w / 2, -d / 2),
    post(-w / 2, d / 2),
    post(w / 2, d / 2),
  ];
}

interface Props {
  machines: Machine[];
  factories: Factory[];
  pos: Record<string, THREE.Vector3>;
  hover: string | null;
  selected: string | null;
  reduced: boolean;
  onHover: (id: string | null) => void;
  onPick: (id: string) => void;
}

export function Machines({ machines, factories, pos, hover, selected, reduced, onHover, onPick }: Props) {
  const box = useRef<THREE.InstancedMesh>(null);
  const cyl = useRef<THREE.InstancedMesh>(null);
  const cone = useRef<THREE.InstancedMesh>(null);
  const refs = useMemo(() => ({ box, cyl, cone }), []);
  const hit = useRef<THREE.InstancedMesh>(null);
  const live = useRef(machines.map(() => 0));

  const shapeKey = machines.map((m) => m.id + m.shape).join() + factories.map((f) => f.id).join();
  const insts = useKeyed(shapeKey, () => {
    const by: Record<Geo, Inst[]> = { box: [], cyl: [], cone: [] };
    factories.forEach((f) => shedParts(f).forEach((p) => by.box.push(p)));
    machines.forEach((m, mi) => {
      const base = pos[m.id];
      if (!base) return;
      SHAPES[m.shape].forEach((part) =>
        by[part.g].push({ mi, part, base, color: PART_COLORS[part.c].clone() }),
      );
    });
    return by;
  });

  // colours: hovered / selected machine slightly lighter
  useLayoutEffect(() => {
    const lift = new THREE.Color("#9aa0a8");
    (Object.keys(insts) as Geo[]).forEach((g) => {
      const mesh = refs[g].current;
      if (!mesh) return;
      insts[g].forEach((it, i) => {
        const id = it.mi >= 0 ? machines[it.mi].id : null;
        const c = it.color.clone();
        if (id && (id === hover || id === selected)) c.lerp(lift, 0.28);
        mesh.setColorAt(i, c);
        mesh.setMatrixAt(i, partMatrix(it.base, it.part));
      });
      mesh.count = insts[g].length;
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
      mesh.computeBoundingSphere();
    });
    const h = hit.current;
    if (h) {
      machines.forEach((m, i) => {
        const p = pos[m.id] ?? new THREE.Vector3();
        h.setMatrixAt(
          i,
          new THREE.Matrix4().compose(
            new THREE.Vector3(p.x, p.y + 0.6, p.z),
            new THREE.Quaternion(),
            new THREE.Vector3(1.6, 1.3, 1.2),
          ),
        );
      });
      h.count = machines.length;
      h.instanceMatrix.needsUpdate = true;
      h.computeBoundingSphere();
    }
  }, [insts, hover, selected, machines, pos, refs]);

  const off = useMemo(() => new THREE.Vector3(), []);
  useFrame(({ clock }, dt) => {
    if (reduced) return;
    const t = clock.elapsedTime;
    const k = 1 - Math.exp(-dt * 10);
    const amp = machines.map((m, i) => {
      const target = store.frame[m.id]?.risk ?? 0;
      live.current[i] += (target - live.current[i]) * k;
      // tiny jitter when healthy, a visible shake as the 1 s score passes ~0.8 of its alarm level
      return 0.004 + 0.06 * Math.min(1.2, Math.max(0, live.current[i] - 0.75));
    });
    (Object.keys(insts) as Geo[]).forEach((g) => {
      const mesh = refs[g].current;
      if (!mesh) return;
      insts[g].forEach((it, i) => {
        if (it.mi < 0) return;
        const a = amp[it.mi];
        const s = it.mi * 7.13;
        off.set(
          a * Math.sin(t * 47 + s) * Math.sin(t * 3.1 + s),
          a * 0.4 * Math.sin(t * 59 + s * 2),
          a * Math.sin(t * 53 + s * 3),
        );
        mesh.setMatrixAt(i, partMatrix(it.base, it.part, off));
      });
      mesh.instanceMatrix.needsUpdate = true;
    });
  });

  const mat = useMemo(() => new THREE.MeshStandardMaterial({ roughness: 0.82, metalness: 0.08 }), []);
  return (
    <group>
      {(Object.keys(GEOS) as Geo[]).map((g) => (
        <instancedMesh
          key={g}
          ref={refs[g]}
          args={[GEOS[g], mat, Math.max(1, insts[g].length)]}
          castShadow
          receiveShadow
          frustumCulled={false}
        />
      ))}
      <instancedMesh
        ref={hit}
        args={[GEOS.box, undefined, Math.max(1, machines.length)]}
        onPointerMove={(e) => {
          e.stopPropagation();
          if (e.instanceId != null) onHover(machines[e.instanceId].id);
        }}
        onPointerOut={() => onHover(null)}
        onClick={(e) => {
          e.stopPropagation();
          if (e.instanceId != null) onPick(machines[e.instanceId].id);
        }}
      >
        <meshBasicMaterial visible={false} />
      </instancedMesh>
    </group>
  );
}
