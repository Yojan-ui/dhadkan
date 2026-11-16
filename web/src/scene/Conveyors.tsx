// One conveyor per bay along its back wall: steel frame and legs, end rollers, a belt whose surface moves,
// and goods riding on it (flour sacks in a flour mill, crates elsewhere). It is scenery that obeys the data:
// a bay's conveyor runs only while at least one of its machines is reported running, and coasts to a stop
// otherwise. One instanced mesh per kind of part; positions are written in useFrame (no React state).
import { useEffect, useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import type { Factory, Machine } from "../api";
import { isRunning } from "../theme";
import { SHED, shedCenter } from "./layout";

const Z = -SHED.d / 2 + 0.42; // just inside the back wall
const X0 = -SHED.w / 2 + 0.55,
  X1 = SHED.w / 2 - 0.55;
const H = 0.42; // belt height
const W = 0.34; // belt width
const ITEMS = 6;
const SPEED = 0.32; // m/s on screen

const BELT_VS = /* glsl */ `
attribute float aOffset;
varying vec2 vUv; varying float vOffset;
void main() {
  vUv = uv; vOffset = aOffset;
  gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
}`;
const BELT_FS = /* glsl */ `
varying vec2 vUv; varying float vOffset;
void main() {
  float slat = step(0.86, fract(vUv.x * 28.0 - vOffset * 28.0));
  gl_FragColor = vec4(mix(vec3(0.085, 0.09, 0.095), vec3(0.2, 0.21, 0.22), slat), 1.0);
}`;

export function Conveyors({
  factories,
  machines,
  reduced,
}: {
  factories: Factory[];
  machines: Machine[];
  reduced: boolean;
}) {
  const n = factories.length;
  const frame = useRef<THREE.InstancedMesh>(null);
  const rollers = useRef<THREE.InstancedMesh>(null);
  const belt = useRef<THREE.InstancedMesh>(null);
  const goods = useRef<THREE.InstancedMesh>(null);
  const speed = useRef<number[]>([]);
  const travel = useRef<number[]>([]);
  const spin = useRef<number[]>([]);

  const kind = useMemo(
    () =>
      factories.map((f) =>
        machines.some((m) => m.factory === f.id && m.shape === "mill")
          ? ("sack" as const)
          : ("crate" as const),
      ),
    [factories, machines],
  );

  const frameParts = useMemo(() => {
    const out: THREE.Matrix4[] = [];
    const len = X1 - X0;
    factories.forEach((_, fi) => {
      const c = shedCenter(fi, n);
      const box = (x: number, y: number, z: number, sx: number, sy: number, sz: number) =>
        out.push(
          new THREE.Matrix4().compose(
            new THREE.Vector3(c.x + x, y, c.z + z),
            new THREE.Quaternion(),
            new THREE.Vector3(sx, sy, sz),
          ),
        );
      for (const dz of [-W / 2 - 0.02, W / 2 + 0.02])
        box((X0 + X1) / 2, H - 0.03, Z + dz, len + 0.1, 0.09, 0.04); // side rails
      for (let x = X0 + 0.1; x <= X1 - 0.05; x += len / 4)
        for (const dz of [-W / 2, W / 2]) box(x, (H - 0.08) / 2 + 0.12, Z + dz, 0.05, H - 0.08, 0.05); // legs
      box(X1 + 0.18, 0.38, Z + W / 2 + 0.16, 0.24, 0.5, 0.12); // drive gearbox
    });
    return out;
  }, [factories, n]);

  const beltMat = useMemo(
    () =>
      new THREE.ShaderMaterial({
        vertexShader: BELT_VS,
        fragmentShader: BELT_FS,
      }),
    [],
  );
  const frameMat = useMemo(
    () => new THREE.MeshStandardMaterial({ color: "#4A4F56", roughness: 0.45, metalness: 0.65 }),
    [],
  );
  const rollerMat = useMemo(
    () => new THREE.MeshStandardMaterial({ color: "#9EA3A9", roughness: 0.3, metalness: 0.8 }),
    [],
  );
  const goodsMat = useMemo(() => new THREE.MeshStandardMaterial({ roughness: 0.85, metalness: 0 }), []);
  const rollerGeo = useMemo(
    () => new THREE.CylinderGeometry(0.05, 0.05, W + 0.02, 14).rotateX(Math.PI / 2),
    [],
  );
  const box = useMemo(() => new THREE.BoxGeometry(1, 1, 1), []);
  const plane = useMemo(() => {
    const g = new THREE.PlaneGeometry(1, 1).rotateX(-Math.PI / 2);
    g.setAttribute("aOffset", new THREE.InstancedBufferAttribute(new Float32Array(Math.max(1, n)), 1));
    return g;
  }, [n]);

  useEffect(
    () => () => [beltMat, frameMat, rollerMat, goodsMat, rollerGeo, box, plane].forEach((o) => o.dispose()),
    [beltMat, frameMat, rollerMat, goodsMat, rollerGeo, box, plane],
  );
  const tmp = useMemo(() => new THREE.Matrix4(), []);
  const q = useMemo(() => new THREE.Quaternion(), []);
  const v = useMemo(() => new THREE.Vector3(), []);
  const sc = useMemo(() => new THREE.Vector3(), []);
  const colors = useMemo(() => ({ sack: new THREE.Color("#D9CDB2"), crate: new THREE.Color("#7B6347") }), []);
  const painted = useRef(false);

  useFrame((_, dt) => {
    const coast = 1 - Math.exp(-dt * 1.2);
    const len = X1 - X0;
    const fr = frame.current,
      ro = rollers.current,
      be = belt.current,
      go = goods.current;
    if (!fr || !ro || !be || !go) return;
    if (!painted.current) {
      frameParts.forEach((m, i) => fr.setMatrixAt(i, m));
      fr.count = frameParts.length;
      fr.instanceMatrix.needsUpdate = true;
      fr.computeBoundingSphere();
    }
    factories.forEach((f, fi) => {
      const c = shedCenter(fi, n);
      const on = machines.some((m) => m.factory === f.id && isRunning(m)) ? 1 : 0;
      speed.current[fi] = (speed.current[fi] ?? on) + (on - (speed.current[fi] ?? on)) * coast;
      const s = reduced ? 0 : speed.current[fi] * SPEED;
      travel.current[fi] = ((travel.current[fi] ?? 0) + s * dt) % len;
      spin.current[fi] = (spin.current[fi] ?? 0) + (s / 0.05) * dt;
      (plane.getAttribute("aOffset") as THREE.InstancedBufferAttribute).setX(fi, travel.current[fi] / len);
      be.setMatrixAt(
        fi,
        tmp.compose(v.set(c.x + (X0 + X1) / 2, H + 0.026, c.z + Z), q.identity(), sc.set(len, 1, W)),
      );
      [X0, X1].forEach((x, j) => {
        q.setFromAxisAngle(v.set(0, 0, 1), -spin.current[fi]);
        ro.setMatrixAt(fi * 2 + j, tmp.compose(v.set(c.x + x, H - 0.03, c.z + Z), q, sc.set(1, 1, 1)));
      });
      q.identity();
      for (let k = 0; k < ITEMS; k++) {
        const x = X0 + ((travel.current[fi] + (k * len) / ITEMS) % len);
        const sack = kind[fi] === "sack";
        const size: [number, number, number] = sack ? [0.26, 0.13, 0.2] : [0.22, 0.18, 0.22];
        go.setMatrixAt(
          fi * ITEMS + k,
          tmp.compose(v.set(c.x + x, H + 0.03 + size[1] / 2, c.z + Z), q, sc.set(...size)),
        );
        if (!painted.current) go.setColorAt(fi * ITEMS + k, colors[kind[fi]]);
      }
    });
    be.count = n;
    ro.count = n * 2;
    go.count = n * ITEMS;
    be.instanceMatrix.needsUpdate = ro.instanceMatrix.needsUpdate = go.instanceMatrix.needsUpdate = true;
    plane.getAttribute("aOffset").needsUpdate = true;
    if (!painted.current && go.instanceColor) go.instanceColor.needsUpdate = true;
    painted.current = true;
  });

  return (
    <group>
      <instancedMesh
        ref={frame}
        args={[box, frameMat, Math.max(1, frameParts.length)]}
        castShadow
        receiveShadow
        frustumCulled={false}
      />
      <instancedMesh
        ref={rollers}
        args={[rollerGeo, rollerMat, Math.max(1, n * 2)]}
        castShadow
        frustumCulled={false}
      />
      <instancedMesh ref={belt} args={[plane, beltMat, Math.max(1, n)]} frustumCulled={false} />
      <instancedMesh
        ref={goods}
        args={[box, goodsMat, Math.max(1, n * ITEMS)]}
        castShadow
        receiveShadow
        frustumCulled={false}
      />
    </group>
  );
}
