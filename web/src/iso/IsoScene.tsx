// The isometric Digital Twin: one dark floor slab, glowing cyan edge, machines on pallets with status
// beacons, overhead rails, robot arms, cabins, workers, lane markings and flow lines.
// Orthographic camera at 45 deg / ~35 deg; slow drift; zoom on scroll, pan on drag.
import { Line, MapControls, OrthographicCamera } from "@react-three/drei";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import type { Machine } from "../api";
import { ARMS, CABINS, FLOOR, LANES_X, RAIL_CROSS, RAIL_LOOP, RAIL_Y, machinePositions } from "./layout";
import { P, statusColour } from "./palette";

type V3 = [number, number, number];
const TILT = THREE.MathUtils.degToRad(35);
const YAW = THREE.MathUtils.degToRad(45);
const DIST = 60;

export function IsoScene({
  machines,
  selected,
  onPick,
  reduced,
}: {
  machines: Machine[];
  selected: string | null;
  onPick: (id: string | null) => void;
  reduced: boolean;
}) {
  const pos = useMemo(() => machinePositions(machines), [machines]);
  return (
    <Canvas dpr={[1, 2]} gl={{ antialias: true }} onPointerMissed={() => onPick(null)}>
      <OrthographicCamera makeDefault zoom={26} near={-200} far={400} position={camPos(0)} />
      <Rig target={selected ? pos[selected] : null} reduced={reduced} />
      <MapControls enableRotate={false} screenSpacePanning zoomSpeed={0.8} minZoom={14} maxZoom={90} />
      <ambientLight intensity={0.55} color="#9FB8D9" />
      <directionalLight position={[18, 30, 12]} intensity={1.15} color="#FFFFFF" />
      <directionalLight position={[-20, 14, -10]} intensity={0.35} color={P.cyan} />
      <Floor />
      <Lanes />
      <FlowLines reduced={reduced} />
      <Rails />
      {CABINS.map((c, i) => (
        <Cabin key={i} {...c} />
      ))}
      {ARMS.map(([x, z], i) => (
        <RobotArm key={i} at={[x, FLOOR.h / 2, z]} phase={i * 1.3} reduced={reduced} />
      ))}
      <Workers reduced={reduced} />
      {machines.map((m) =>
        pos[m.id] ? (
          <MachineUnit
            key={m.id}
            m={m}
            at={pos[m.id]}
            on={m.id === selected}
            onPick={() => onPick(m.id)}
            reduced={reduced}
          />
        ) : null,
      )}
      <EffectComposer>
        <Bloom intensity={0.9} luminanceThreshold={0.55} luminanceSmoothing={0.2} mipmapBlur />
      </EffectComposer>
    </Canvas>
  );
}

function camPos(drift: number): V3 {
  const yaw = YAW + drift;
  return [
    DIST * Math.cos(TILT) * Math.sin(yaw),
    DIST * Math.sin(TILT),
    DIST * Math.cos(TILT) * Math.cos(yaw),
  ];
}

/** Slow drift around the isometric angle; eases to a picked machine (target + zoom). */
function Rig({ target, reduced }: { target: V3 | null; reduced: boolean }) {
  const { camera, controls } = useThree() as unknown as {
    camera: THREE.OrthographicCamera;
    controls: { target: THREE.Vector3; update: () => void } | null;
  };
  const goal = useRef(new THREE.Vector3());
  useFrame((st, dt) => {
    const drift = reduced ? 0 : Math.sin(st.clock.elapsedTime * 0.05) * 0.06;
    goal.current.set(...(target ?? [0, 0, 0]));
    const ctl = controls;
    const base = ctl ? ctl.target : new THREE.Vector3();
    if (target) base.lerp(goal.current, Math.min(1, dt * 2.5));
    const off = camPos(drift);
    camera.position.set(base.x + off[0], base.y + off[1], base.z + off[2]);
    camera.lookAt(base);
    if (target) camera.zoom += (48 - camera.zoom) * Math.min(1, dt * 2);
    camera.updateProjectionMatrix();
    ctl?.update();
  });
  return null;
}

function Floor() {
  const { w, d, h } = FLOOR;
  const edge: V3[] = [
    [-w / 2, h / 2 + 0.01, -d / 2],
    [w / 2, h / 2 + 0.01, -d / 2],
    [w / 2, h / 2 + 0.01, d / 2],
    [-w / 2, h / 2 + 0.01, d / 2],
    [-w / 2, h / 2 + 0.01, -d / 2],
  ];
  const outer: V3[] = edge.map(([x, , z]) => [x * 1.035, -h / 2, z * 1.05]);
  const grid = useMemo(() => {
    const out: V3[][] = [];
    for (let x = -w / 2 + 2; x < w / 2; x += 2)
      out.push([
        [x, h / 2 + 0.005, -d / 2],
        [x, h / 2 + 0.005, d / 2],
      ]);
    for (let z = -d / 2 + 2; z < d / 2; z += 2)
      out.push([
        [-w / 2, h / 2 + 0.005, z],
        [w / 2, h / 2 + 0.005, z],
      ]);
    return out;
  }, [w, d, h]);
  return (
    <group>
      <mesh receiveShadow>
        <boxGeometry args={[w, h, d]} />
        <meshStandardMaterial color={P.floor} roughness={0.9} metalness={0.1} />
      </mesh>
      {grid.map((pts, i) => (
        <Line key={i} points={pts} color={P.grid} lineWidth={1} transparent opacity={0.55} />
      ))}
      <Line
        points={edge}
        color={new THREE.Color(P.floorEdge).multiplyScalar(2.2)}
        lineWidth={2.2}
        toneMapped={false}
      />
      <Line points={outer} color={P.frame} lineWidth={1} transparent opacity={0.8} />
    </group>
  );
}

function Lanes() {
  const y = FLOOR.h / 2 + 0.02;
  const z = FLOOR.d / 2 - 1;
  return (
    <group>
      {LANES_X.map((x) =>
        [-0.45, 0.45].map((o) => (
          <Line
            key={`${x}${o}`}
            points={[
              [x + o, y, -z],
              [x + o, y, z],
            ]}
            color={P.yellow}
            lineWidth={2}
            dashed
            dashSize={0.9}
            gapSize={0.45}
          />
        )),
      )}
      <Line
        points={[
          [-FLOOR.w / 2 + 1, y, -z],
          [FLOOR.w / 2 - 1, y, -z],
        ]}
        color={P.yellow}
        lineWidth={2}
      />
      <Line
        points={[
          [-FLOOR.w / 2 + 1, y, z],
          [FLOOR.w / 2 - 1, y, z],
        ]}
        color={P.yellow}
        lineWidth={2}
      />
    </group>
  );
}

/** Teal flow lines along the floor under the rails, dashes moving with the material flow. */
function FlowLines({ reduced }: { reduced: boolean }) {
  const y = FLOOR.h / 2 + 0.03;
  const ref = useRef<THREE.Group>(null);
  useFrame((_, dt) => {
    if (reduced || !ref.current) return;
    ref.current.children.forEach((c) => {
      const mat = (c as unknown as { material?: { dashOffset: number } }).material;
      if (mat) mat.dashOffset -= dt * 1.2;
    });
  });
  const loop: V3[] = [...RAIL_LOOP, RAIL_LOOP[0]].map(([x, z]) => [x, y, z]);
  return (
    <group ref={ref}>
      <Line
        points={loop}
        color={new THREE.Color(P.teal).multiplyScalar(1.6)}
        lineWidth={1.6}
        dashed
        dashSize={0.6}
        gapSize={0.6}
        toneMapped={false}
      />
      {RAIL_CROSS.map((seg, i) => (
        <Line
          key={i}
          points={seg.map(([x, z]) => [x, y, z] as V3)}
          color={P.teal}
          lineWidth={1.2}
          dashed
          dashSize={0.5}
          gapSize={0.7}
          transparent
          opacity={0.8}
        />
      ))}
    </group>
  );
}

function Rails() {
  const posts = useMemo(() => {
    const out: [number, number][] = [];
    for (let i = 0; i < RAIL_LOOP.length; i++) {
      const [a, b] = [RAIL_LOOP[i], RAIL_LOOP[(i + 1) % RAIL_LOOP.length]];
      const n = Math.max(2, Math.round(Math.hypot(b[0] - a[0], b[1] - a[1]) / 6));
      for (let k = 0; k < n; k++) out.push([a[0] + ((b[0] - a[0]) * k) / n, a[1] + ((b[1] - a[1]) * k) / n]);
    }
    return out;
  }, []);
  const tube = (pts: [number, number][], r: number) => {
    const curve = new THREE.CatmullRomCurve3(
      pts.map(([x, z]) => new THREE.Vector3(x, RAIL_Y, z)),
      false,
      "catmullrom",
      0,
    );
    return new THREE.TubeGeometry(curve, pts.length * 24, r, 8, false);
  };
  const loop = useMemo(() => tube([...RAIL_LOOP, RAIL_LOOP[0]], 0.16), []);
  const cross = useMemo(() => RAIL_CROSS.map((s) => tube(s, 0.12)), []);
  return (
    <group>
      <mesh geometry={loop}>
        <meshStandardMaterial color={P.silver} metalness={0.7} roughness={0.3} />
      </mesh>
      {cross.map((g, i) => (
        <mesh key={i} geometry={g}>
          <meshStandardMaterial color={P.silver} metalness={0.7} roughness={0.35} />
        </mesh>
      ))}
      {posts.map(([x, z], i) => (
        <mesh key={i} position={[x, (RAIL_Y + FLOOR.h / 2) / 2, z]}>
          <boxGeometry args={[0.18, RAIL_Y - FLOOR.h / 2, 0.18]} />
          <meshStandardMaterial color={P.steel} metalness={0.5} roughness={0.5} />
        </mesh>
      ))}
    </group>
  );
}

function Cabin({ x, z, w, d, h }: { x: number; z: number; w: number; d: number; h: number }) {
  const y0 = FLOOR.h / 2;
  return (
    <group position={[x, y0, z]}>
      <mesh position={[0, h / 2, 0]}>
        <boxGeometry args={[w, h, d]} />
        <meshStandardMaterial color={P.cabin} roughness={0.6} />
      </mesh>
      <mesh position={[0, h + 0.06, 0]}>
        <boxGeometry args={[w + 0.2, 0.12, d + 0.2]} />
        <meshStandardMaterial color="#C9D2DE" />
      </mesh>
      {[-w / 3, 0, w / 3].map((wx) => (
        <mesh key={wx} position={[wx, h * 0.6, d / 2 + 0.01]}>
          <planeGeometry args={[w / 4.5, h / 3]} />
          <meshStandardMaterial color="#5B7AA3" emissive="#2A4A73" emissiveIntensity={0.6} />
        </mesh>
      ))}
    </group>
  );
}

function RobotArm({ at, phase, reduced }: { at: V3; phase: number; reduced: boolean }) {
  const upper = useRef<THREE.Group>(null);
  const fore = useRef<THREE.Group>(null);
  useFrame((st) => {
    if (reduced) return;
    const t = st.clock.elapsedTime * 0.5 + phase;
    if (upper.current) upper.current.rotation.y = Math.sin(t) * 0.9;
    if (fore.current) fore.current.rotation.z = -0.6 + Math.sin(t * 1.3) * 0.35;
  });
  return (
    <group position={at}>
      <mesh position={[0, 0.25, 0]}>
        <cylinderGeometry args={[0.55, 0.65, 0.5, 16]} />
        <meshStandardMaterial color={P.dark} metalness={0.4} roughness={0.5} />
      </mesh>
      <group ref={upper} position={[0, 0.5, 0]}>
        <mesh position={[0, 0.3, 0]}>
          <cylinderGeometry args={[0.32, 0.38, 0.6, 12]} />
          <meshStandardMaterial color={P.orange} roughness={0.45} />
        </mesh>
        <group position={[0, 0.6, 0]} rotation={[0, 0, 0.5]}>
          <mesh position={[0, 0.8, 0]}>
            <boxGeometry args={[0.32, 1.6, 0.32]} />
            <meshStandardMaterial color={P.orange} roughness={0.45} />
          </mesh>
          <group ref={fore} position={[0, 1.6, 0]}>
            <mesh position={[0.7, 0, 0]}>
              <boxGeometry args={[1.4, 0.26, 0.26]} />
              <meshStandardMaterial color={P.orange} roughness={0.45} />
            </mesh>
            <mesh position={[1.45, -0.12, 0]}>
              <boxGeometry args={[0.2, 0.35, 0.36]} />
              <meshStandardMaterial color={P.steel} metalness={0.6} />
            </mesh>
          </group>
        </group>
      </group>
    </group>
  );
}

const WALKS: V3[][] = [
  [
    [-6.5, 0, -11],
    [-6.5, 0, 11],
  ],
  [
    [6.5, 0, 11],
    [6.5, 0, -11],
  ],
  [
    [-17, 0, 11.6],
    [17, 0, 11.6],
  ],
];
function Workers({ reduced }: { reduced: boolean }) {
  const refs = useRef<(THREE.Group | null)[]>([]);
  useFrame((st) => {
    WALKS.forEach(([a, b], i) => {
      const g = refs.current[i];
      if (!g) return;
      const t = reduced ? 0.4 : (Math.sin(st.clock.elapsedTime * 0.08 + i * 2) + 1) / 2;
      g.position.set(a[0] + (b[0] - a[0]) * t, FLOOR.h / 2, a[2] + (b[2] - a[2]) * t);
    });
  });
  return (
    <group>
      {WALKS.map((_, i) => (
        <group key={i} ref={(g) => void (refs.current[i] = g)}>
          <mesh position={[0, 0.55, 0]}>
            <capsuleGeometry args={[0.2, 0.6, 4, 8]} />
            <meshStandardMaterial color={i % 2 ? "#3D6FB6" : "#2E5A96"} />
          </mesh>
          <mesh position={[0, 1.15, 0]}>
            <sphereGeometry args={[0.17, 12, 12]} />
            <meshStandardMaterial color="#C99A6B" />
          </mesh>
          <mesh position={[0, 1.27, 0]}>
            <sphereGeometry args={[0.18, 12, 12, 0, Math.PI * 2, 0, Math.PI / 2]} />
            <meshStandardMaterial color={P.yellow} />
          </mesh>
        </group>
      ))}
    </group>
  );
}

// ---------------------------------------------------------------- machines
function MachineUnit({
  m,
  at,
  on,
  onPick,
  reduced,
}: {
  m: Machine;
  at: V3;
  on: boolean;
  onPick: () => void;
  reduced: boolean;
}) {
  const alert = m.status === "alarm" || m.status === "early_match";
  const col = statusColour(m.status);
  const beacon = useRef<THREE.Mesh>(null);
  const body = useRef<THREE.Group>(null);
  const light = useRef<THREE.PointLight>(null);
  useFrame((st) => {
    const t = st.clock.elapsedTime;
    const pulse = alert && !reduced ? 0.5 + 0.5 * Math.sin(t * 4) : 1;
    const mat = beacon.current?.material as THREE.MeshStandardMaterial | undefined;
    if (mat) mat.emissiveIntensity = alert ? 1.4 + pulse * 2.6 : 0.85;
    if (light.current) light.current.intensity = alert ? 2 + pulse * 6 : 0;
    if (body.current && !reduced)
      body.current.position.y = alert ? Math.sin(t * 40) * 0.015 * Math.min(3, m.risk3 ?? 1) : 0;
  });
  const Model = MODELS[m.shape] ?? Motor;
  return (
    <group position={at} onClick={(e) => (e.stopPropagation(), onPick())}>
      <mesh position={[0, 0.1, 0]}>
        <boxGeometry args={[4, 0.2, 3.2]} />
        <meshStandardMaterial color={P.pallet} roughness={0.9} />
      </mesh>
      {on && (
        <Line
          points={[
            [-2.15, 0.22, -1.75],
            [2.15, 0.22, -1.75],
            [2.15, 0.22, 1.75],
            [-2.15, 0.22, 1.75],
            [-2.15, 0.22, -1.75],
          ]}
          color={new THREE.Color(P.cyan).multiplyScalar(2)}
          lineWidth={2}
          toneMapped={false}
        />
      )}
      <group ref={body} position={[0, 0.2, 0]}>
        <Model hot={alert} />
      </group>
      <mesh position={[0, 3.4, 0]}>
        <cylinderGeometry args={[0.02, 0.02, 0.9, 6]} />
        <meshStandardMaterial color={P.steel} />
      </mesh>
      <mesh ref={beacon} position={[0, 3.95, 0]}>
        <sphereGeometry args={[0.2, 16, 16]} />
        <meshStandardMaterial color={col} emissive={col} emissiveIntensity={2} toneMapped={false} />
      </mesh>
      <pointLight ref={light} position={[0, 2.2, 0]} color={P.alert} distance={7} decay={2} intensity={0} />
    </group>
  );
}

const metal = (c: string, hot = false) => (
  <meshStandardMaterial
    color={c}
    metalness={0.45}
    roughness={0.45}
    emissive={hot ? P.alert : "#000000"}
    emissiveIntensity={hot ? 0.35 : 0}
  />
);

function Loom({ hot }: { hot: boolean }) {
  return (
    <group>
      {[-1.4, 1.4].map((x) => (
        <mesh key={x} position={[x, 0.8, 0]}>
          <boxGeometry args={[0.18, 1.6, 2.2]} />
          {metal("#5B6B82", hot)}
        </mesh>
      ))}
      {[
        [0, 1.45, -0.7],
        [0, 0.95, 0.75],
        [0, 0.45, -0.2],
      ].map(([x, y, z], i) => (
        <mesh key={i} position={[x, y, z]} rotation={[0, 0, Math.PI / 2]}>
          <cylinderGeometry args={[0.18, 0.18, 2.7, 16]} />
          {metal(i === 0 ? "#D9DEE6" : "#8A97AA", hot)}
        </mesh>
      ))}
      <mesh position={[0, 1.15, 0.1]}>
        <boxGeometry args={[2.6, 0.06, 1.2]} />
        <meshStandardMaterial color="#E9E2CF" roughness={0.9} />
      </mesh>
      <mesh position={[1.75, 0.45, 0.6]}>
        <boxGeometry args={[0.5, 0.6, 0.6]} />
        {metal(P.orange, hot)}
      </mesh>
    </group>
  );
}

function Mill({ hot }: { hot: boolean }) {
  return (
    <group>
      <mesh position={[0, 0.55, 0]}>
        <cylinderGeometry args={[0.9, 0.95, 1.1, 20]} />
        {metal("#6C7A8F", hot)}
      </mesh>
      <mesh position={[0, 1.55, 0]}>
        <coneGeometry args={[0.85, 0.9, 16, 1, true]} />
        {metal("#AAB4C2", hot)}
      </mesh>
      <mesh position={[1.4, 0.45, 0]}>
        <cylinderGeometry args={[0.35, 0.35, 0.9, 16]} />
        {metal(P.orange, hot)}
      </mesh>
      <mesh position={[0.7, 0.7, 0]} rotation={[0, 0, Math.PI / 2]}>
        <torusGeometry args={[0.32, 0.05, 6, 20]} />
        {metal("#2B2F36", hot)}
      </mesh>
    </group>
  );
}

function Pump({ hot }: { hot: boolean }) {
  return (
    <group>
      <mesh position={[-0.6, 0.45, 0]} rotation={[0, 0, Math.PI / 2]}>
        <cylinderGeometry args={[0.42, 0.42, 1.4, 18]} />
        {metal(P.orange, hot)}
      </mesh>
      <mesh position={[0.6, 0.48, 0]}>
        <cylinderGeometry args={[0.55, 0.55, 0.5, 18]} />
        {metal("#7D8A9C", hot)}
      </mesh>
      <mesh position={[0.6, 0.48, -0.9]} rotation={[Math.PI / 2, 0, 0]}>
        <cylinderGeometry args={[0.16, 0.16, 1.4, 12]} />
        {metal("#B8C2CF", hot)}
      </mesh>
      <mesh position={[0.6, 1.1, 0]}>
        <cylinderGeometry args={[0.14, 0.14, 0.9, 12]} />
        {metal("#B8C2CF", hot)}
      </mesh>
    </group>
  );
}

function Motor({ hot }: { hot: boolean }) {
  return (
    <group>
      <mesh position={[0, 0.5, 0]} rotation={[0, 0, Math.PI / 2]}>
        <cylinderGeometry args={[0.5, 0.5, 1.6, 20]} />
        {metal("#5C6C84", hot)}
      </mesh>
      <mesh position={[1.1, 0.5, 0]}>
        <boxGeometry args={[0.6, 0.9, 1.2]} />
        {metal(P.orange, hot)}
      </mesh>
      <mesh position={[-1.05, 0.5, 0]} rotation={[0, 0, Math.PI / 2]}>
        <cylinderGeometry args={[0.38, 0.38, 0.18, 20]} />
        {metal("#2B2F36", hot)}
      </mesh>
    </group>
  );
}

const MODELS: Record<string, (p: { hot: boolean }) => React.JSX.Element> = {
  loom: Loom,
  mill: Mill,
  pump: Pump,
  motor: Motor,
};
