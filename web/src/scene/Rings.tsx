// Floor rings (fill clockwise = how close to its own alarm level) and sound ripples
// (rise with the live loudness). Each is one instanced mesh with a small shader.
import { useLayoutEffect, useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { store, type Machine } from "../api";
import { fill, ringRGB } from "../theme";
import { pulses } from "./fx";
import { TOP } from "./layout";

const RING_VS = /* glsl */ `
attribute float aFill; attribute vec3 aColor; attribute float aPulse;
varying vec2 vUv; varying float vFill; varying vec3 vColor; varying float vPulse;
void main() {
  vUv = uv; vFill = aFill; vColor = aColor; vPulse = aPulse;
  gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
}`;
const RING_FS = /* glsl */ `
varying vec2 vUv; varying float vFill; varying vec3 vColor; varying float vPulse;
void main() {
  vec2 p = vUv * 2.0 - 1.0;
  float r = length(p);
  float ang = fract(atan(p.x, p.y) / 6.2831853 + 1.0);          // 0 at the far side, clockwise
  float band = smoothstep(0.50, 0.515, r) * (1.0 - smoothstep(0.60, 0.615, r));
  float on = step(ang, vFill) * step(0.001, vFill);
  vec3 col = mix(vec3(0.23, 0.24, 0.26), vColor, on);
  float a = band * mix(0.7, 1.0, on);
  if (vPulse >= 0.0) {                                           // one outward pulse on a registry match
    float pr = 0.62 + vPulse * 0.36;
    float w = smoothstep(pr - 0.05, pr, r) * (1.0 - smoothstep(pr, pr + 0.015, r));
    col = mix(col, vec3(1.0, 0.416, 0.075), w);
    a = max(a, w * (1.0 - vPulse));
  }
  if (a < 0.01) discard;
  gl_FragColor = vec4(col, a);
}`;

const RIPPLE_VS = /* glsl */ `
attribute float aAlpha; varying float vAlpha;
void main() { vAlpha = aAlpha; gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0); }`;
const RIPPLE_FS = /* glsl */ `
varying float vAlpha;
void main() { if (vAlpha < 0.01) discard; gl_FragColor = vec4(0.82, 0.83, 0.85, vAlpha); }`;

const PER = 3;          // ripples per machine
const PULSE_S = 1.4;

export function Rings({ machines, pos, reduced }: { machines: Machine[]; pos: Record<string, THREE.Vector3>; reduced: boolean }) {
  const ring = useRef<THREE.InstancedMesh>(null);
  const rip = useRef<THREE.InstancedMesh>(null);
  const n = machines.length;
  const shown = useRef<number[]>([]);

  const ringGeo = useMemo(() => {
    const g = new THREE.PlaneGeometry(2.6, 2.6);
    g.rotateX(-Math.PI / 2);
    g.setAttribute("aFill", new THREE.InstancedBufferAttribute(new Float32Array(n), 1));
    g.setAttribute("aColor", new THREE.InstancedBufferAttribute(new Float32Array(n * 3), 3));
    g.setAttribute("aPulse", new THREE.InstancedBufferAttribute(new Float32Array(n).fill(-1), 1));
    return g;
  }, [n]);
  const ripGeo = useMemo(() => {
    const g = new THREE.RingGeometry(0.93, 1, 48);
    g.rotateX(-Math.PI / 2);
    g.setAttribute("aAlpha", new THREE.InstancedBufferAttribute(new Float32Array(n * PER), 1));
    return g;
  }, [n]);
  const ringMat = useMemo(() => new THREE.ShaderMaterial({
    vertexShader: RING_VS, fragmentShader: RING_FS, transparent: true, depthWrite: false,
    polygonOffset: true, polygonOffsetFactor: -2,
  }), []);
  const ripMat = useMemo(() => new THREE.ShaderMaterial({
    vertexShader: RIPPLE_VS, fragmentShader: RIPPLE_FS, transparent: true, depthWrite: false, side: THREE.DoubleSide,
  }), []);

  useLayoutEffect(() => {
    const m = ring.current;
    if (!m) return;
    machines.forEach((mc, i) => {
      const p = pos[mc.id] ?? new THREE.Vector3();
      m.setMatrixAt(i, new THREE.Matrix4().makeTranslation(p.x, 0.125, p.z));
    });
    m.instanceMatrix.needsUpdate = true;
    m.computeBoundingSphere();
  }, [machines, pos]);

  const tmp = useMemo(() => new THREE.Matrix4(), []);
  useFrame(({ clock }, dt) => {
    const t = clock.elapsedTime;
    const now = performance.now() / 1000;
    const fa = ringGeo.getAttribute("aFill") as THREE.InstancedBufferAttribute;
    const ca = ringGeo.getAttribute("aColor") as THREE.InstancedBufferAttribute;
    const pa = ringGeo.getAttribute("aPulse") as THREE.InstancedBufferAttribute;
    const k = reduced ? 1 : 1 - Math.exp(-dt * 3);
    machines.forEach((m, i) => {
      const target = fill(m);
      shown.current[i] = (shown.current[i] ?? 0) + (target - (shown.current[i] ?? 0)) * k;
      fa.setX(i, shown.current[i]);
      const [r, g, b] = ringRGB(m);
      ca.setXYZ(i, r, g, b);
      const s = pulses.get(m.id);
      const ph = s == null ? -1 : (now - s) / PULSE_S;
      pa.setX(i, ph >= 0 && ph <= 1 && !reduced ? ph : -1);
      if (s != null && ph > 1) pulses.delete(m.id);
    });
    fa.needsUpdate = ca.needsUpdate = pa.needsUpdate = true;

    const rm = rip.current;
    if (!rm) return;
    rm.visible = !reduced;
    if (reduced) return;
    const al = ripGeo.getAttribute("aAlpha") as THREE.InstancedBufferAttribute;
    machines.forEach((m, i) => {
      const p = pos[m.id] ?? new THREE.Vector3();
      const e = store.frame[m.id]?.energy;
      // loudness of the current 0.5 s slice, -50..-25 dBFS -> 0..1
      const loud = e == null ? 0 : Math.min(1, Math.max(0, (e + 50) / 25));
      for (let j = 0; j < PER; j++) {
        const ph = (t * 0.55 + j / PER) % 1;
        const s = 0.14 + ph * (0.18 + 0.22 * loud);
        tmp.makeScale(s, 1, s).setPosition(p.x, p.y + TOP[m.shape] + ph * 0.55, p.z);
        rm.setMatrixAt(i * PER + j, tmp);
        al.setX(i * PER + j, (1 - ph) * ph * 4 * 0.38 * loud);
      }
    });
    rm.instanceMatrix.needsUpdate = true;
    al.needsUpdate = true;
  });

  return (
    <group>
      <instancedMesh ref={ring} args={[ringGeo, ringMat, n]} frustumCulled={false} renderOrder={2} />
      <instancedMesh ref={rip} args={[ripGeo, ripMat, n * PER]} frustumCulled={false} renderOrder={3} />
    </group>
  );
}
