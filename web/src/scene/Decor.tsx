// Estate surroundings, for depth only (no data): a back row of warehouses with a few lit windows,
// a compound wall in front of the road, and street lamps.
import { useMemo } from "react";
import * as THREE from "three";
import { ROAD, SHED, estateExtent } from "./layout";

interface B {
  p: [number, number, number];
  s: [number, number, number];
  c: string;
}

export function Decor({ n }: { n: number }) {
  const ext = estateExtent(n);
  const { solid, glow } = useMemo(() => {
    const solid: B[] = [];
    const glow: B[] = [];
    const backZ = -SHED.d / 2 - 8;
    const widths = [7.5, 5.5, 8.5, 6, 7, 6.5];
    let x = ext.minX - 6;
    widths.forEach((w, i) => {
      const h = 1.8 + ((i * 37) % 5) * 0.22;
      solid.push({
        p: [x + w / 2, h / 2, backZ - (i % 2) * 1.4],
        s: [w, h, 3.6],
        c: i % 2 ? "#1C1F23" : "#1F2226",
      });
      solid.push({ p: [x + w / 2, h + 0.08, backZ - (i % 2) * 1.4], s: [w + 0.3, 0.16, 4.5], c: "#2A2D32" });
      for (let k = 0; k < Math.floor(w / 1.6); k++)
        if ((i + k) % 4 === 0)
          glow.push({
            p: [x + 0.9 + k * 1.6, h * 0.6, backZ - (i % 2) * 1.4 + 2.11],
            s: [0.9, 0.26, 0.02],
            c: "#A88250",
          });
      x += w + 1.2;
    });
    const wallZ = ROAD.z + ROAD.w / 2 + 1.3;
    for (let wx = ext.minX - 4; wx < ext.maxX + 4; wx += 4.2)
      solid.push({ p: [wx + 1.9, 0.32, wallZ], s: [3.8, 0.64, 0.22], c: "#2B2E32" });
    for (let lx = ext.minX + 1; lx < ext.maxX; lx += 7) {
      solid.push({ p: [lx, 1.6, ROAD.z - ROAD.w / 2 - 0.35], s: [0.08, 3.2, 0.08], c: "#3A3E44" });
      solid.push({ p: [lx, 3.18, ROAD.z - ROAD.w / 2 - 0.05], s: [0.1, 0.06, 0.7], c: "#3A3E44" });
      glow.push({ p: [lx, 3.12, ROAD.z - ROAD.w / 2 + 0.25], s: [0.22, 0.05, 0.3], c: "#FFD9A0" });
    }
    return { solid, glow };
  }, [ext.minX, ext.maxX]);

  const fill = (list: B[]) => (m: THREE.InstancedMesh | null) => {
    if (!m) return;
    list.forEach((b, i) => {
      m.setMatrixAt(
        i,
        new THREE.Matrix4().compose(
          new THREE.Vector3(...b.p),
          new THREE.Quaternion(),
          new THREE.Vector3(...b.s),
        ),
      );
      m.setColorAt(i, new THREE.Color(b.c));
    });
    m.instanceMatrix.needsUpdate = true;
    if (m.instanceColor) m.instanceColor.needsUpdate = true;
    m.computeBoundingSphere();
  };
  return (
    <group>
      <instancedMesh args={[undefined, undefined, solid.length]} ref={fill(solid)} castShadow receiveShadow>
        <boxGeometry />
        <meshStandardMaterial roughness={0.9} metalness={0.05} />
      </instancedMesh>
      <instancedMesh args={[undefined, undefined, glow.length]} ref={fill(glow)}>
        <boxGeometry />
        <meshBasicMaterial toneMapped={false} />
      </instancedMesh>
    </group>
  );
}
