// The 3D shop-floor map. One directional light (with soft ground shadows) plus low ambient fill.
// Draw calls stay small: 3 instanced part meshes, 1 ring mesh, 1 ripple mesh, ground,
// route (2 + marker), a few arcs and, in machine view, the waterfall.
import { useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import * as THREE from "three";
import type { Factory, Machine, Meta, State } from "../api";
import { C } from "../theme";
import { CameraRig, type View } from "./CameraRig";
import { ArcLabels, Contagion, useArcs } from "./Contagion";
import { Label, LabelProjector } from "./labels";
import { clusterCenter, machinePositions, SHED, world } from "./layout";
import { useKeyed } from "./fx";
import { Machines } from "./Machines";
import { Rings } from "./Rings";
import { RouteLabels, RouteMesh, useRoute } from "./Route";
import { Waterfall, WaterfallLabels } from "./Waterfall";

interface Props {
  state: State;
  meta: Meta;
  view: View;
  reduced: boolean;
  hover: string | null;
  onHover: (id: string | null) => void;
  onView: (v: View) => void;
}

const ISO = new THREE.Vector3(1, 1.15, 1).normalize();

/** Camera distance and ground target that frame every shed corner from the ISO direction. */
function fitCluster(factories: Factory[], aspect: number) {
  const d = ISO.clone();
  const right = new THREE.Vector3(0, 1, 0).cross(d).normalize();
  const up = d.clone().cross(right).normalize();
  const fwd = new THREE.Vector3(-d.x, 0, -d.z).normalize();
  let r0 = Infinity,
    r1 = -Infinity,
    u0 = Infinity,
    u1 = -Infinity;
  for (const f of factories) {
    const c = world(f.xy_km);
    for (const [dx, dz] of [
      [-1, -1],
      [1, -1],
      [-1, 1],
      [1, 1],
    ]) {
      for (const y of [0, 1.6]) {
        const p = new THREE.Vector3(c.x + (dx * SHED.w) / 2, y, c.z + (dz * SHED.d) / 2);
        const r = p.dot(right),
          u = p.dot(up);
        r0 = Math.min(r0, r);
        r1 = Math.max(r1, r);
        u0 = Math.min(u0, u);
        u1 = Math.max(u1, u);
      }
    }
  }
  // a little below centre, so the bottom approval bar does not cover the nearest shed
  const rc = (r0 + r1) / 2,
    uc = (u0 + u1) / 2 - (u1 - u0) * 0.07;
  const target = right
    .clone()
    .multiplyScalar(rc)
    .addScaledVector(fwd, uc / fwd.dot(up));
  const tv = Math.tan(THREE.MathUtils.degToRad(15));
  const dist = Math.max((u1 - u0) / 2 / tv, (r1 - r0) / 2 / (tv * aspect)) * 1.3;
  return { target, pos: target.clone().addScaledVector(ISO, dist) };
}

function goalFor(view: View, factories: Factory[], pos: Record<string, THREE.Vector3>, aspect: number) {
  if (view.kind === "machine" && pos[view.id]) {
    const p = pos[view.id];
    const target = p.clone().add(new THREE.Vector3(1.2, 0.95, 0));
    return { target, pos: target.clone().add(new THREE.Vector3(0.6, 3.2, 7.2)) };
  }
  if (view.kind === "shed") {
    const f = factories.find((x) => x.id === view.factory);
    if (f) {
      const target = world(f.xy_km).setY(0.4);
      return { target, pos: target.clone().addScaledVector(ISO, 15) };
    }
  }
  return fitCluster(factories, aspect);
}

/** Shift the picture left by half the side panel, so the subject sits in the visible part. */
function ViewOffset({ px }: { px: number }) {
  const { camera, size } = useThree() as unknown as {
    camera: THREE.PerspectiveCamera;
    size: { width: number; height: number };
  };
  useEffect(() => {
    if (px) camera.setViewOffset(size.width, size.height, px, 0, size.width, size.height);
    else camera.clearViewOffset();
    camera.updateProjectionMatrix();
  }, [camera, size.width, size.height, px]);
  return null;
}

/** ?debug: draw calls and frame rate, written into a DOM node outside the canvas. */
function Stats({ out }: { out: React.RefObject<HTMLDivElement | null> }) {
  const gl = useThree((st) => st.gl);
  const acc = useRef({ frames: 0, last: performance.now() });
  useFrame(() => {
    const a = acc.current;
    a.frames++;
    const now = performance.now();
    if (now - a.last > 1000 && out.current) {
      out.current.textContent = `${Math.round((a.frames * 1000) / (now - a.last))} fps · ${gl.info.render.calls} draw calls · ${gl.info.render.triangles} tris`;
      a.frames = 0;
      a.last = now;
    }
  });
  return null;
}

export default function Scene({ state, meta, view, reduced, hover, onHover, onView }: Props) {
  const [aspect, setAspect] = useState(() => window.innerWidth / Math.max(1, window.innerHeight - 52));
  useEffect(() => {
    const f = () => setAspect(window.innerWidth / Math.max(1, window.innerHeight - 52));
    window.addEventListener("resize", f);
    return () => window.removeEventListener("resize", f);
  }, []);
  const factories = meta.factories;
  const machines = state.machines;
  const pos = useKeyed(machines.map((m) => m.id + m.factory).join(), () =>
    machinePositions(factories, machines),
  );
  const goal = useMemo(() => goalFor(view, factories, pos, aspect), [view, factories, pos, aspect]);
  const center = useMemo(() => clusterCenter(factories), [factories]);
  const selected = view.kind === "machine" ? view.id : null;
  const sel = machines.find((m) => m.id === selected);
  const debug = new URLSearchParams(location.search).has("debug");
  const statsEl = useRef<HTMLDivElement>(null);
  const arcs = useArcs();
  const plan = useRoute(state.route, factories, machines, meta.technician.day_start, reduced);
  const wfAt = sel && pos[sel.id] ? pos[sel.id].clone().add(new THREE.Vector3(2.3, 1.75, -0.3)) : null;
  const inMachine = view.kind === "machine";
  const shedOf = view.kind === "shed" ? view.factory : sel?.factory;

  const pick = (id: string) => {
    const m = machines.find((x) => x.id === id) as Machine;
    if (view.kind === "shed" && view.factory === m.factory) onView({ kind: "machine", id });
    else if (view.kind === "machine") onView({ kind: "machine", id });
    else onView({ kind: "shed", factory: m.factory });
  };

  return (
    <div className="scene">
      <Canvas
        shadows="percentage"
        dpr={[1, 1.75]}
        gl={{ antialias: true, powerPreference: "high-performance" }}
        camera={{ fov: 30, near: 0.5, far: 300, position: goal.pos.toArray() }}
        onPointerMissed={() => onHover(null)}
        style={{ background: C.bg }}
      >
        <color attach="background" args={[C.bg]} />
        <ambientLight intensity={0.55} />
        <directionalLight
          position={[center.x + 10, 22, center.z + 14]}
          intensity={2.1}
          castShadow
          shadow-mapSize={[2048, 2048]}
          shadow-bias={-0.0004}
          shadow-radius={6}
          shadow-camera-left={-24}
          shadow-camera-right={24}
          shadow-camera-top={24}
          shadow-camera-bottom={-24}
          shadow-camera-near={1}
          shadow-camera-far={80}
        >
          <object3D attach="target" position={center.toArray()} />
        </directionalLight>

        <mesh rotation-x={-Math.PI / 2} position={[center.x, 0, center.z]} receiveShadow>
          <planeGeometry args={[140, 140]} />
          <meshStandardMaterial color={C.ground} roughness={1} />
        </mesh>

        <Machines
          machines={machines}
          factories={factories}
          pos={pos}
          hover={hover}
          selected={selected}
          reduced={reduced}
          onHover={onHover}
          onPick={pick}
        />
        <Rings machines={machines} pos={pos} reduced={reduced} />
        <group visible={!inMachine}>
          <Contagion arcs={arcs} machines={machines} pos={pos} reduced={reduced} />
          {plan && <RouteMesh plan={plan} reduced={reduced} />}
        </group>
        {sel && wfAt && <Waterfall machine={sel} at={wfAt} reduced={reduced} />}

        <OrbitControls
          makeDefault
          enableDamping
          dampingFactor={0.08}
          maxPolarAngle={1.32}
          minDistance={3}
          maxDistance={70}
        />
        <CameraRig goal={goal} view={view} reduced={reduced} />
        <ViewOffset px={inMachine ? 190 : 0} />
        <LabelProjector />
        {debug && <Stats out={statsEl} />}
      </Canvas>
      <div className="labels">
        {!inMachine &&
          factories.map((f) => {
            const c = world(f.xy_km);
            return (
              <Label key={f.id} at={[c.x - SHED.w / 2, 1.5, c.z - SHED.d / 2]} className="pin-click">
                <button
                  className={`shed-label ${shedOf === f.id ? "on" : ""}`}
                  onClick={() => onView({ kind: "shed", factory: f.id })}
                >
                  <span className="mono">{f.id}</span> {f.name}
                  <small>{f.area}</small>
                </button>
              </Label>
            );
          })}
        {view.kind !== "cluster" &&
          machines
            .filter((m) => m.factory === shedOf && pos[m.id])
            .map((m) => (
              <Label key={m.id} at={[pos[m.id].x, 0.15, pos[m.id].z + 0.95]}>
                <div className={`mlabel ${m.id === selected ? "on" : ""}`}>{m.name}</div>
              </Label>
            ))}
        {!inMachine && <ArcLabels arcs={arcs} machines={machines} pos={pos} reduced={reduced} />}
        {!inMachine && plan && <RouteLabels plan={plan} reduced={reduced} />}
        {wfAt && <WaterfallLabels at={wfAt} hz={meta.mel_hz} />}
      </div>
      {debug && <div ref={statsEl} className="stats mono" data-testid="stats" />}
    </div>
  );
}
