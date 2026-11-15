// The estate ground: one plane with a very faint grid, and the road in front of the sheds.
import { useMemo } from "react";
import { Grid } from "@react-three/drei";
import * as THREE from "three";
import { ROAD, estateExtent } from "./layout";

export function Ground({ n }: { n: number }) {
  const ext = estateExtent(n);
  const len = ext.maxX - ext.minX + 14;
  const cx = (ext.maxX + ext.minX) / 2;
  // road, kerbs and centre dashes as one instanced mesh
  const pieces = useMemo(() => {
    const out: { p: THREE.Vector3; s: THREE.Vector3; c: string }[] = [
      { p: new THREE.Vector3(cx, 0.012, ROAD.z), s: new THREE.Vector3(len, 0.02, ROAD.w), c: "#1E2024" },
      {
        p: new THREE.Vector3(cx, 0.03, ROAD.z - ROAD.w / 2),
        s: new THREE.Vector3(len, 0.05, 0.12),
        c: "#30333A",
      },
      {
        p: new THREE.Vector3(cx, 0.03, ROAD.z + ROAD.w / 2),
        s: new THREE.Vector3(len, 0.05, 0.12),
        c: "#30333A",
      },
    ];
    for (let x = cx - len / 2 + 1; x < cx + len / 2 - 1; x += 1.6)
      out.push({
        p: new THREE.Vector3(x, 0.025, ROAD.z),
        s: new THREE.Vector3(0.7, 0.01, 0.07),
        c: "#6B6A5F",
      });
    return out;
  }, [cx, len]);
  return (
    <group>
      <mesh rotation-x={-Math.PI / 2} position={[cx, 0, 0]} receiveShadow>
        <planeGeometry args={[220, 220]} />
        <meshStandardMaterial color="#202227" roughness={0.95} metalness={0} />
      </mesh>
      <Grid
        position={[cx, 0.002, 0]}
        args={[220, 220]}
        cellSize={1}
        cellThickness={0.5}
        cellColor="#272a2f"
        sectionSize={5}
        sectionThickness={0.8}
        sectionColor="#30343a"
        fadeDistance={70}
        fadeStrength={1.5}
        infiniteGrid
      />
      <instancedMesh
        args={[undefined, undefined, pieces.length]}
        receiveShadow
        ref={(m) => {
          if (!m) return;
          pieces.forEach((d, i) => {
            m.setMatrixAt(i, new THREE.Matrix4().compose(d.p, new THREE.Quaternion(), d.s));
            m.setColorAt(i, new THREE.Color(d.c));
          });
          m.instanceMatrix.needsUpdate = true;
          if (m.instanceColor) m.instanceColor.needsUpdate = true;
          m.computeBoundingSphere();
        }}
      >
        <boxGeometry />
        <meshStandardMaterial roughness={0.95} />
      </instancedMesh>
    </group>
  );
}
