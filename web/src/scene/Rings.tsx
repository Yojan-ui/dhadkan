// Status rings under every machine (green normal, amber watch, orange alert, dashed grey offline, blue-grey
// learning; the filled arc = how close to its own alarm level) and sound ripples, which rise with the
// LIVE loudness only (no live reading, no ripple). Each is one instanced mesh with a small shader.
import { useLayoutEffect, useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { store, type Machine } from "../api";
import { fill, twinRGB, twinState, type Twin } from "../theme";
import { pulses } from "./fx";
import { TOP } from "./layout";

const RING_VS = /* glsl */ `
attribute float aFill; attribute vec3 aColor; attribute float aPulse; attribute float aState; attribute float aSeed;
varying vec2 vUv; varying float vFill; varying vec3 vColor; varying float vPulse; varying float vState; varying float vSeed;
void main() {
  vUv = uv; vFill = aFill; vColor = aColor; vPulse = aPulse; vState = aState; vSeed = aSeed;
  gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
}`;
// aState: 0 normal, 1 watch, 2 alert, 3 offline, 4 learning
const RING_FS = /* glsl */ `
uniform float uTime; uniform float uMotion;
varying vec2 vUv; varying float vFill; varying vec3 vColor; varying float vPulse; varying float vState; varying float vSeed;
void main() {
  vec2 p = vUv * 2.0 - 1.0;
  float r = length(p);
  float ang = fract(atan(p.x, p.y) / 6.2831853 + 1.0);           // 0 at the far side, clockwise
  float t = uTime * uMotion + vSeed;
  bool watch = vState > 0.5 && vState < 1.5, alert = vState > 1.5 && vState < 2.5;
  bool offline = vState > 2.5 && vState < 3.5;
  float band = smoothstep(0.50, 0.515, r) * (1.0 - smoothstep(0.585, 0.60, r));
  float base = offline ? 0.32 : vState > 3.5 ? 0.30 : watch ? 0.55 : alert ? 0.8 : 0.34;
  if (offline) band *= step(0.45, fract(ang * 28.0));               // dashed: no signal or stopped
  float breathe = alert ? 0.75 + 0.25 * sin(t * 3.4) : watch ? 0.82 + 0.18 * sin(t * 1.6) : 0.94 + 0.06 * sin(t * 0.8);
  if (offline) breathe = 1.0;
  vec3 col = vColor;
  float a = band * base * breathe;
  // the part of the ring filled by risk (watch / alert only) is brighter
  float on = (watch || alert) ? step(ang, vFill) * step(0.001, vFill) : 0.0;
  col = mix(col, vColor * 1.35, on);
  a = max(a, band * on);
  // soft inner glow, kept low (no bloom pass)
  float glow = exp(-pow((r - 0.55) / 0.09, 2.0)) * (alert ? 0.30 : watch ? 0.16 : offline ? 0.0 : 0.07) * breathe;
  a = max(a, glow * (1.0 - band));
  // alert: a wave travels outward every 1.6 s; watch: a slow, faint one
  if ((alert || watch) && uMotion > 0.0) {
    float period = alert ? 1.6 : 3.2;
    float ph = fract(t / period);
    float pr = 0.6 + ph * 0.36;
    float w = smoothstep(pr - 0.04, pr, r) * (1.0 - smoothstep(pr, pr + 0.012, r));
    a = max(a, w * (1.0 - ph) * (alert ? 0.7 : 0.35));
  }
  if (vPulse >= 0.0) {                                            // one outward pulse on a registry match
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

const PER = 3; // ripples per machine
const STATE_CODE: Record<Twin, number> = { normal: 0, watch: 1, alert: 2, offline: 3, learning: 4 };
const PULSE_S = 0.3;

export function Rings({
  machines,
  pos,
  reduced,
}: {
  machines: Machine[];
  pos: Record<string, THREE.Vector3>;
  reduced: boolean;
}) {
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
    g.setAttribute("aState", new THREE.InstancedBufferAttribute(new Float32Array(n), 1));
    g.setAttribute(
      "aSeed",
      new THREE.InstancedBufferAttribute(
        new Float32Array(n).map((_, i) => i * 0.77),
        1,
      ),
    );
    return g;
  }, [n]);
  const ripGeo = useMemo(() => {
    const g = new THREE.RingGeometry(0.93, 1, 48);
    g.rotateX(-Math.PI / 2);
    g.setAttribute("aAlpha", new THREE.InstancedBufferAttribute(new Float32Array(n * PER), 1));
    return g;
  }, [n]);
  const ringMat = useMemo(
    () =>
      new THREE.ShaderMaterial({
        vertexShader: RING_VS,
        fragmentShader: RING_FS,
        uniforms: { uTime: { value: 0 }, uMotion: { value: 1 } },
        transparent: true,
        depthWrite: false,
        polygonOffset: true,
        polygonOffsetFactor: -2,
      }),
    [],
  );
  const ripMat = useMemo(
    () =>
      new THREE.ShaderMaterial({
        vertexShader: RIPPLE_VS,
        fragmentShader: RIPPLE_FS,
        transparent: true,
        depthWrite: false,
        side: THREE.DoubleSide,
      }),
    [],
  );

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
    const k = reduced ? 1 : 1 - Math.exp(-dt * 10); // ~95% of the way in 300 ms
    const sa = ringGeo.getAttribute("aState") as THREE.InstancedBufferAttribute;
    ringMat.uniforms.uTime.value = t;
    ringMat.uniforms.uMotion.value = reduced ? 0 : 1;
    machines.forEach((m, i) => {
      // every machine has a ring in its state's colour; the filled arc (risk) shows on watch and alert
      const st = twinState(m);
      const target = st === "watch" || st === "alert" ? fill(m) : -1;
      if (target < 0 || (shown.current[i] ?? -1) < 0) shown.current[i] = target;
      shown.current[i] = (shown.current[i] ?? 0) + (target - (shown.current[i] ?? 0)) * k;
      fa.setX(i, shown.current[i]);
      const [r, g, b] = twinRGB(m);
      ca.setXYZ(i, r, g, b);
      sa.setX(i, STATE_CODE[st]);
      const s = pulses.get(m.id);
      const ph = s == null ? -1 : (now - s) / PULSE_S;
      pa.setX(i, ph >= 0 && ph <= 1 && !reduced ? ph : -1);
      if (s != null && ph > 1) pulses.delete(m.id);
    });
    sa.needsUpdate = true;
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
