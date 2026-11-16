// Sensors as the data sees them. A small sensor node on each machine with one LED:
//   live      green, with a bright flash each time a NEW reading arrives (latest.tick changes)
//   stale     dim and steady: the sensor is there but nothing new came (the machine is stopped)
//   lost/none dark: no signal; the label says when the last reading was
// One gateway per bay (the shed's phone link to the cluster): green when the link is up, amber and
// blinking while it is down and readings wait in the outbox. Blower outlets show airflow while running.
// Nothing here invents a reading: every state comes from /api/state.
import { useEffect, useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import type { Factory, Machine, Shape } from "../api";
import { isRunning } from "../theme";
import { SHED, shedCenter } from "./layout";

/** Where the sensor sits on each shape (on the motor's terminal box or housing). */
const MOUNT: Record<Shape, [number, number, number]> = {
  pump: [-0.3, 0.7, 0.0],
  motor: [-0.38, 0.64, 0.0],
  loom: [-1.0, 0.42, 0.36],
  mill: [0.82, 0.39, -0.05],
};
const FLASH_S = 0.7;
const GREEN = new THREE.Color("#46C17E"),
  DIM = new THREE.Color("#1F5A3A"),
  OFF = new THREE.Color("#2A2C30"),
  AMBER = new THREE.Color("#E3A23B");
const AIR = 4; // airflow streaks per blower

export function Sensors({
  machines,
  factories,
  pos,
  linkUp,
  reduced,
}: {
  machines: Machine[];
  factories: Factory[];
  pos: Record<string, THREE.Vector3>;
  linkUp: boolean;
  reduced: boolean;
}) {
  const body = useRef<THREE.InstancedMesh>(null);
  const led = useRef<THREE.InstancedMesh>(null);
  const gw = useRef<THREE.InstancedMesh>(null);
  const gwLed = useRef<THREE.InstancedMesh>(null);
  const air = useRef<THREE.InstancedMesh>(null);
  const seen = useRef<Record<string, number>>({});
  const flash = useRef<Record<string, number>>({});
  const n = machines.length;
  const nf = factories.length;
  const blowers = machines.filter((m) => m.shape === "motor" && pos[m.id]);

  const box = useMemo(() => new THREE.BoxGeometry(1, 1, 1), []);
  const dot = useMemo(() => new THREE.SphereGeometry(0.5, 10, 8), []);
  const bodyMat = useMemo(
    () => new THREE.MeshStandardMaterial({ color: "#1B1D21", roughness: 0.4, metalness: 0.5 }),
    [],
  );
  const ledMat = useMemo(() => new THREE.MeshBasicMaterial({ toneMapped: false }), []);
  const airGeo = useMemo(() => {
    const g = new THREE.PlaneGeometry(1, 1);
    g.setAttribute(
      "aAlpha",
      new THREE.InstancedBufferAttribute(new Float32Array(Math.max(1, blowers.length * AIR)), 1),
    );
    return g;
  }, [blowers.length]);
  const airMat = useMemo(
    () =>
      new THREE.ShaderMaterial({
        vertexShader: `attribute float aAlpha; varying float vA; varying vec2 vUv;
          void main(){ vA = aAlpha; vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position,1.0); }`,
        fragmentShader: `varying float vA; varying vec2 vUv;
          void main(){ float edge = sin(vUv.x * 3.14159); float a = vA * edge * 0.35; if (a < 0.01) discard;
            gl_FragColor = vec4(0.82, 0.85, 0.88, a); }`,
        transparent: true,
        depthWrite: false,
        side: THREE.DoubleSide,
      }),
    [],
  );

  useEffect(
    () => () => [box, dot, bodyMat, ledMat, airGeo, airMat].forEach((o) => o.dispose()),
    [box, dot, bodyMat, ledMat, airGeo, airMat],
  );
  const tmp = useMemo(() => new THREE.Matrix4(), []);
  const q = useMemo(() => new THREE.Quaternion(), []);
  const v = useMemo(() => new THREE.Vector3(), []);
  const s = useMemo(() => new THREE.Vector3(), []);
  const c = useMemo(() => new THREE.Color(), []);

  useFrame(({ clock, camera }) => {
    const t = clock.elapsedTime;
    const now = performance.now() / 1000;
    const b = body.current,
      l = led.current;
    if (b && l) {
      machines.forEach((m, i) => {
        const p = pos[m.id];
        if (!p) return;
        const [mx, my, mz] = MOUNT[m.shape];
        b.setMatrixAt(
          i,
          tmp.compose(v.set(p.x + mx, p.y + my, p.z + mz), q.identity(), s.set(0.1, 0.05, 0.07)),
        );
        l.setMatrixAt(
          i,
          tmp.compose(v.set(p.x + mx, p.y + my + 0.035, p.z + mz + 0.02), q, s.set(0.035, 0.035, 0.035)),
        );
        const tick = m.latest?.tick ?? -1;
        if (seen.current[m.id] !== undefined && tick > seen.current[m.id]) flash.current[m.id] = now; // new data
        seen.current[m.id] = tick;
        const sensor = m.operating?.sensor ?? (m.latest ? "live" : "none");
        if (sensor === "live") {
          const f = flash.current[m.id];
          const k = f == null || reduced ? 0 : Math.max(0, 1 - (now - f) / FLASH_S);
          c.copy(GREEN).multiplyScalar(0.7 + 1.3 * k);
        } else if (sensor === "stale") c.copy(DIM);
        else c.copy(OFF);
        l.setColorAt(i, c);
      });
      b.count = l.count = n;
      b.instanceMatrix.needsUpdate = l.instanceMatrix.needsUpdate = true;
      if (l.instanceColor) l.instanceColor.needsUpdate = true;
    }
    const g = gw.current,
      gl = gwLed.current;
    if (g && gl) {
      factories.forEach((_, fi) => {
        const ctr = shedCenter(fi, nf);
        const x = ctr.x - SHED.w / 2 + 0.25,
          z = ctr.z + SHED.d / 2 - 0.12;
        g.setMatrixAt(fi, tmp.compose(v.set(x, 0.75, z), q.identity(), s.set(0.32, 0.42, 0.12)));
        gl.setMatrixAt(fi, tmp.compose(v.set(x + 0.08, 0.88, z + 0.07), q, s.set(0.045, 0.045, 0.045)));
        const blink = reduced ? 1 : 0.5 + 0.5 * Math.sign(Math.sin(t * 5));
        gl.setColorAt(fi, linkUp ? GREEN : c.copy(AMBER).multiplyScalar(0.25 + 0.75 * blink));
      });
      g.count = gl.count = nf;
      g.instanceMatrix.needsUpdate = gl.instanceMatrix.needsUpdate = true;
      if (gl.instanceColor) gl.instanceColor.needsUpdate = true;
    }
    const a = air.current;
    if (a) {
      a.visible = !reduced;
      const al = airGeo.getAttribute("aAlpha") as THREE.InstancedBufferAttribute;
      q.copy(camera.quaternion); // streaks face the camera
      blowers.forEach((m, bi) => {
        const p = pos[m.id];
        const on = isRunning(m) && m.operating?.sensor !== "lost" ? 1 : 0;
        for (let j = 0; j < AIR; j++) {
          const ph = (t * 0.9 + j / AIR + bi * 0.13) % 1;
          a.setMatrixAt(
            bi * AIR + j,
            tmp.compose(
              v.set(p.x + 0.3 + (j - 1.5) * 0.06, p.y + 1.42 + ph * 0.55, p.z - 0.05),
              q,
              s.set(0.05, 0.22, 1),
            ),
          );
          al.setX(bi * AIR + j, on * (1 - ph) * ph * 4);
        }
      });
      a.count = blowers.length * AIR;
      a.instanceMatrix.needsUpdate = true;
      al.needsUpdate = true;
    }
  });

  return (
    <group>
      <instancedMesh ref={body} args={[box, bodyMat, Math.max(1, n)]} castShadow frustumCulled={false} />
      <instancedMesh ref={led} args={[dot, ledMat, Math.max(1, n)]} frustumCulled={false} />
      <instancedMesh ref={gw} args={[box, bodyMat, Math.max(1, nf)]} castShadow frustumCulled={false} />
      <instancedMesh ref={gwLed} args={[dot, ledMat, Math.max(1, nf)]} frustumCulled={false} />
      <instancedMesh
        ref={air}
        args={[airGeo, airMat, Math.max(1, blowers.length * AIR)]}
        frustumCulled={false}
        renderOrder={4}
      />
    </group>
  );
}
