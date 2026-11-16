// The estate in 3D. Warm hemisphere light + one key light with soft shadows, faint fog over a
// charcoal ground, ambient occlusion (N8AO) and a pulsing outline on alerting machines.
// Fills the centre column; labels are DOM, clipped to this column, and fade when hidden behind a shed.
import { useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import { EffectComposer, N8AO, Outline, Selection, ToneMapping } from "@react-three/postprocessing";
import { ToneMappingMode } from "postprocessing";
import * as THREE from "three";
import type { Machine, Meta, State } from "../api";
import { C, inr } from "../theme";
import { CameraRig, type Goal, type View } from "./CameraRig";
import { ArcLabels, Contagion, useArcs } from "./Contagion";
import { useKeyed } from "./fx";
import { Decor } from "./Decor";
import { Ground } from "./Ground";
import { Label, LabelProjector } from "./labels";
import { SHED, TOP, estateExtent, machinePositions, shedCenter } from "./layout";
import { Machines } from "./Machines";
import { Rings } from "./Rings";
import { RouteLabels, RouteMesh, useRoute } from "./Route";

interface Props {
  state: State;
  machines: Machine[]; // state.machines, with the time-machine reading swapped in for the selected one
  meta: Meta;
  view: View;
  reduced: boolean;
  hover: string | null;
  onHover: (id: string | null) => void;
  onView: (v: View) => void;
}

const BG = "#121417";
const DIR = new THREE.Vector3(0, 0.95, 1).normalize(); // square on from the road side, ~43 deg down; the sway adds the angle
const FOV = 28;
const FILL = 0.8; // the estate fills about 80% of the view

/** Camera distance and target that frame a box (x/z extent, 0..height) at FILL of the view. */
function frame(minX: number, maxX: number, minZ: number, maxZ: number, height: number, aspect: number): Goal {
  const right = new THREE.Vector3(0, 1, 0).cross(DIR).normalize();
  const up = DIR.clone().cross(right).normalize();
  let r0 = Infinity,
    r1 = -Infinity,
    u0 = Infinity,
    u1 = -Infinity;
  for (const x of [minX, maxX])
    for (const z of [minZ, maxZ])
      for (const y of [0, height]) {
        const p = new THREE.Vector3(x, y, z);
        r0 = Math.min(r0, p.dot(right));
        r1 = Math.max(r1, p.dot(right));
        u0 = Math.min(u0, p.dot(up));
        u1 = Math.max(u1, p.dot(up));
      }
  const fwd = new THREE.Vector3(-DIR.x, 0, -DIR.z).normalize();
  const target = right
    .clone()
    .multiplyScalar((r0 + r1) / 2)
    .addScaledVector(fwd, (u0 + u1) / 2 / fwd.dot(up))
    .setY(0.6)
    .add(new THREE.Vector3(0, 0, 0.9)); // sit the sheds at the visual centre, not low
  const tv = Math.tan(THREE.MathUtils.degToRad(FOV / 2));
  const dist = Math.max((u1 - u0) / 2 / tv, (r1 - r0) / 2 / (tv * aspect)) / FILL;
  return { target, pos: target.clone().addScaledVector(DIR, dist) };
}

function goalFor(view: View, meta: Meta, pos: Record<string, THREE.Vector3>, aspect: number): Goal {
  const n = meta.factories.length;
  if (view.kind === "machine" && pos[view.id]) {
    const p = pos[view.id];
    const target = p.clone().add(new THREE.Vector3(1.1, 0.85, 0));
    return { target, pos: target.clone().addScaledVector(DIR, 9.5) };
  }
  if (view.kind === "shed") {
    const i = meta.factories.findIndex((f) => f.id === view.factory);
    if (i >= 0) {
      const c = shedCenter(i, n);
      return frame(
        c.x - SHED.w / 2 - 0.4,
        c.x + SHED.w / 2 + 0.4,
        -SHED.d / 2 - 0.3,
        SHED.d / 2 + 0.6,
        SHED.roof + 0.7,
        aspect,
      );
    }
  }
  const e = estateExtent(n);
  return frame(e.minX, e.maxX, e.minZ, e.maxZ, SHED.roof + 0.7, aspect);
}

/** ?debug: draw calls and frame rate, written into a DOM node outside the canvas. */
function Stats({ out }: { out: React.RefObject<HTMLDivElement | null> }) {
  // the composer renders several passes per frame, so count a whole frame, not the last pass
  const gl = useThree((st) => st.gl);
  const acc = useRef({ frames: 0, last: performance.now() });
  useEffect(() => {
    gl.info.autoReset = false;
    return () => {
      gl.info.autoReset = true;
    };
  }, [gl]);
  useFrame(() => {
    const a = acc.current;
    a.frames++;
    const now = performance.now();
    if (now - a.last > 1000 && out.current) {
      out.current.textContent = `${Math.round((a.frames * 1000) / (now - a.last))} fps · ${gl.info.render.calls} draw calls · ${gl.info.render.triangles} tris per frame`;
      a.frames = 0;
      a.last = now;
    }
    gl.info.reset();
  }, -1);
  return null;
}

/** A soft orange light over each alerting machine (a fixed pool of 3, so shaders never recompile). */
function AlertLights({
  machines,
  pos,
  reduced,
}: {
  machines: Machine[];
  pos: Record<string, THREE.Vector3>;
  reduced: boolean;
}) {
  const lights = useRef<(THREE.PointLight | null)[]>([]);
  const alerting = machines
    .filter((m) => (m.status === "alarm" || m.status === "early_match") && pos[m.id])
    .slice(0, 3);
  useFrame(({ clock }) => {
    lights.current.forEach((l, i) => {
      if (!l) return;
      const m = alerting[i];
      if (!m) {
        l.intensity = 0;
        return;
      }
      const p = pos[m.id];
      l.position.set(p.x, p.y + TOP[m.shape] + 0.3, p.z + 0.2);
      l.intensity = 9 * (reduced ? 1 : 0.8 + 0.2 * Math.sin(clock.elapsedTime * 2.4 + i));
    });
  });
  return (
    <>
      {[0, 1, 2].map((i) => (
        <pointLight
          key={i}
          ref={(l) => {
            lights.current[i] = l;
          }}
          color={C.accent}
          intensity={0}
          distance={4.5}
          decay={2}
        />
      ))}
    </>
  );
}

function daysWords(m: Machine) {
  const f = m.forecast;
  if (!f) return "";
  if (f.status === "estimate") return f.high == null ? `> ${f.low} days` : `${f.low}–${f.high} days`;
  if (f.status === "at_fault_level") return "sounds faulty now";
  return "no countdown";
}

export default function Scene({ state, machines, meta, view, reduced, hover, onHover, onView }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const [aspect, setAspect] = useState(1.4);
  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setAspect(el.clientWidth / Math.max(1, el.clientHeight)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const factories = meta.factories;
  const n = factories.length;
  const pos = useKeyed(machines.map((m) => m.id + m.factory).join(), () =>
    machinePositions(factories, machines),
  );
  const goal = useMemo(() => goalFor(view, meta, pos, aspect), [view, meta, pos, aspect]);
  const selected = view.kind === "machine" ? view.id : null;
  const debug = new URLSearchParams(location.search).has("debug");
  const noPost = new URLSearchParams(location.search).has("nopost"); // debug: measure the scene alone
  const statsEl = useRef<HTMLDivElement>(null);
  const arcs = useArcs();
  const plan = useRoute(state.route, factories, machines, meta.technician.day_start, reduced);
  const inMachine = view.kind === "machine";
  const ext = estateExtent(n);
  const atRisk: Record<string, number> = {};
  for (const m of machines) atRisk[m.factory] = (atRisk[m.factory] ?? 0) + m.rupees_at_risk;
  const alerting = machines.filter((m) => (m.status === "alarm" || m.status === "early_match") && pos[m.id]);

  const pick = (id: string) => onView({ kind: "machine", id });

  return (
    <div className="scene" ref={wrap}>
      <Canvas
        shadows="percentage"
        flat
        dpr={[1, 1.75]}
        gl={{ antialias: false, powerPreference: "high-performance" }}
        camera={{ fov: FOV, near: 0.5, far: 200, position: goal.pos.toArray() }}
        onPointerMissed={() => onHover(null)}
        style={{ background: BG }}
      >
        <color attach="background" args={[BG]} />
        <fog attach="fog" args={[BG, 32, 85]} />
        <hemisphereLight args={["#ffe4c8", "#332d27", 1.6]} />
        <ambientLight intensity={0.2} />
        <directionalLight
          position={[-9, 18, 12]}
          intensity={3.8}
          color="#fff3e2"
          castShadow
          shadow-mapSize={[2048, 2048]}
          shadow-bias={-0.0004}
          shadow-normalBias={0.02}
          shadow-radius={6}
          shadow-camera-left={-(ext.maxX - ext.minX) / 2 - 3}
          shadow-camera-right={(ext.maxX - ext.minX) / 2 + 3}
          shadow-camera-top={10}
          shadow-camera-bottom={-10}
          shadow-camera-near={1}
          shadow-camera-far={60}
        >
          <object3D attach="target" position={[(ext.maxX + ext.minX) / 2, 0, 0]} />
        </directionalLight>

        <Ground n={n} />
        <Decor n={n} />
        <Selection>
          <Machines
            machines={machines}
            factories={factories}
            pos={pos}
            rpm={meta.nominal_rpm ?? {}}
            hover={hover}
            selected={selected}
            reduced={reduced}
            roofs={view.kind === "cluster"}
            onHover={onHover}
            onPick={pick}
          />
          {!noPost && (
            <EffectComposer multisampling={4} autoClear={false}>
              <N8AO halfRes aoRadius={1} intensity={1.6} distanceFalloff={0.7} color="black" />
              <Outline
                blur
                edgeStrength={6}
                pulseSpeed={reduced ? 0 : 0.45}
                visibleEdgeColor={0xff6a13}
                hiddenEdgeColor={0x7a2e08}
                xRay={false}
              />
              <ToneMapping mode={ToneMappingMode.ACES_FILMIC} />
            </EffectComposer>
          )}
        </Selection>
        <Rings machines={machines} pos={pos} reduced={reduced} />
        <AlertLights machines={machines} pos={pos} reduced={reduced} />
        <group visible={!inMachine}>
          <Contagion arcs={arcs} machines={machines} pos={pos} reduced={reduced} />
          {plan && <RouteMesh plan={plan} reduced={reduced} />}
        </group>

        <OrbitControls
          makeDefault
          enableDamping={false}
          maxPolarAngle={1.3}
          minDistance={3}
          maxDistance={90}
        />
        <CameraRig goal={goal} view={view} reduced={reduced} />
        <LabelProjector />
        {debug && <Stats out={statsEl} />}
      </Canvas>
      <div className="labels">
        {!inMachine &&
          factories.map((f, i) => {
            const c = shedCenter(i, n);
            return (
              <Label key={f.id} at={[c.x, SHED.roof + 1.05, 0]} className="pin-click" occlude>
                <button className="shed-label" onClick={() => onView({ kind: "shed", factory: f.id })}>
                  <span className="sl-name">
                    <span className="mono accent-soft">{f.id}</span> {f.name}
                  </span>
                  <small>
                    {f.area}
                    {atRisk[f.id] > 0 && <span className="accent mono"> · {inr(atRisk[f.id])} at risk</span>}
                  </small>
                </button>
              </Label>
            );
          })}
        {alerting.map((m) => (
          <Label key={`alert-${m.id}`} at={pos[m.id].clone().setY(pos[m.id].y + TOP[m.shape] + 0.55)} occlude>
            <div className="alert-tag">
              {m.name} ·{" "}
              {m.fault_name?.en ??
                (m.known_fault ?? m.registry?.fault ?? "fault not named yet").replace("_", " ")}{" "}
              · {daysWords(m)}
            </div>
          </Label>
        ))}
        {!inMachine && <ArcLabels arcs={arcs} machines={machines} pos={pos} reduced={reduced} />}
        {!inMachine && plan && <RouteLabels plan={plan} reduced={reduced} />}
      </div>
      {debug && <div ref={statsEl} className="stats mono" data-testid="stats" />}
    </div>
  );
}
