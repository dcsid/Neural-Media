"use client";

import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useRef } from "react";
import * as THREE from "three";
import type { MutableRefObject } from "react";

// Canonical neuroscience-poster camera angles, snapped via a small set
// of preset buttons on the BrainMesh viewport overlay. ~300ms easing
// between presets.
//
// Caveat on Medial L / Medial R: a true medial view shows the cut-through
// face of a single hemisphere — that requires either splitting the mesh
// into separate L/R chunks or clipping with a plane, neither of which the
// brain-viz worker has scope to do here. The camera positions below land
// in the intra-hemisphere gap pointing outward, which is the best we can
// do with a unified mesh; the user gets the angle, not a textbook cutaway.
// Document this honestly in the button tooltips.

export type CameraPreset =
  | "lateral-l"
  | "lateral-r"
  | "medial-l"
  | "medial-r"
  | "dorsal"
  | "ventral"
  | "default";

interface PresetPose {
  position: THREE.Vector3;
  target: THREE.Vector3;
  up: THREE.Vector3;
}

const DEFAULT_RADIUS = 3.2;

// All positions / targets are in world units. Up vectors are chosen so
// the brain reads right-side-up in each view (top of head pointing up
// in lateral views; nose-ish pointing up in dorsal; reversed in ventral).
const PRESETS: Record<CameraPreset, PresetPose> = {
  "lateral-l": {
    position: new THREE.Vector3(-DEFAULT_RADIUS, 0.12, 0),
    target: new THREE.Vector3(0, 0, 0),
    up: new THREE.Vector3(0, 1, 0),
  },
  "lateral-r": {
    position: new THREE.Vector3(DEFAULT_RADIUS, 0.12, 0),
    target: new THREE.Vector3(0, 0, 0),
    up: new THREE.Vector3(0, 1, 0),
  },
  // Medial views: camera sits just outside the contralateral side and
  // looks toward the medial face of the named hemisphere. The unified
  // mesh means the contralateral hemisphere occludes the cut plane;
  // this view favours angle over occlusion correctness.
  "medial-l": {
    position: new THREE.Vector3(DEFAULT_RADIUS * 0.95, 0.12, 0),
    target: new THREE.Vector3(-0.6, 0, 0),
    up: new THREE.Vector3(0, 1, 0),
  },
  "medial-r": {
    position: new THREE.Vector3(-DEFAULT_RADIUS * 0.95, 0.12, 0),
    target: new THREE.Vector3(0.6, 0, 0),
    up: new THREE.Vector3(0, 1, 0),
  },
  dorsal: {
    position: new THREE.Vector3(0, DEFAULT_RADIUS, 0),
    target: new THREE.Vector3(0, 0, 0),
    // Up faces "front of head" (+Z by the fsaverage build's
    // convention — apps/web/public/brain/README.md). Setting up=-Z
    // makes the dorsal view read with anterior at the top.
    up: new THREE.Vector3(0, 0, -1),
  },
  ventral: {
    position: new THREE.Vector3(0, -DEFAULT_RADIUS, 0),
    target: new THREE.Vector3(0, 0, 0),
    up: new THREE.Vector3(0, 0, 1),
  },
  default: {
    position: new THREE.Vector3(0, 0, DEFAULT_RADIUS),
    target: new THREE.Vector3(0, 0, 0),
    up: new THREE.Vector3(0, 1, 0),
  },
};

export function isCameraPreset(value: unknown): value is CameraPreset {
  return (
    typeof value === "string" &&
    Object.prototype.hasOwnProperty.call(PRESETS, value)
  );
}

// Structural subset of three-stdlib's OrbitControls — matches the same
// shape used by TourCameraDriver so we don't need a direct three-stdlib
// import. Only the surface area CameraPresetDriver actually touches.
interface OrbitControlsLike {
  enabled: boolean;
  target: THREE.Vector3;
  update: () => void;
}

interface CameraPresetDriverProps {
  // Caller signals a preset request by bumping `requestId`. We keep an
  // identifier rather than an enum value so re-clicking the same button
  // re-snaps the camera (handy if the user dragged away and wants to
  // pop back). `preset` resolves the actual pose; `requestId` triggers
  // the transition.
  preset: CameraPreset | null;
  requestId: number;
  controlsRef: MutableRefObject<OrbitControlsLike | null>;
  // When the tour is running it owns the camera; we go dormant so the
  // two drivers don't argue over per-frame writes.
  suspended?: boolean;
  // When true (prefers-reduced-motion), snap immediately instead of
  // easing. Setting the camera state in one frame is the most
  // accessible option — no motion to filter.
  snap?: boolean;
  // Fires when an in-progress transition reaches its destination.
  // Lets the parent clear its "currently tweening" indicator.
  onSettle?: (preset: CameraPreset | null) => void;
}

// Ease constants. ~300ms feels snappy without being a jump-cut. We
// model the transition as an exponential ease: `progress` runs 0→1
// at TRANSITION_RATE, and we lerp camera position / target / up by
// `progress`. Snapping (reduced-motion) sets progress=1 in one frame.
const TRANSITION_SECONDS = 0.3;
const TRANSITION_RATE = 1 / TRANSITION_SECONDS;

export function CameraPresetDriver({
  preset,
  requestId,
  controlsRef,
  suspended = false,
  snap = false,
  onSettle,
}: CameraPresetDriverProps) {
  const camera = useThree((s) => s.camera);
  const invalidate = useThree((s) => s.invalidate);

  // Snapshot of where we're transitioning FROM and TO. Both filled when
  // a new requestId comes in; cleared after settle.
  const fromPosRef = useRef(new THREE.Vector3());
  const fromTargetRef = useRef(new THREE.Vector3());
  const fromUpRef = useRef(new THREE.Vector3());
  const toPoseRef = useRef<PresetPose | null>(null);
  const progressRef = useRef(1); // 1 = settled / idle
  // Track the last `requestId` we acted on. We don't use the value
  // itself for anything except the inequality check.
  const lastRequestRef = useRef<number | null>(null);

  useEffect(() => {
    if (suspended) return;
    if (lastRequestRef.current === requestId) return;
    if (!preset) return;
    lastRequestRef.current = requestId;

    const controls = controlsRef.current;
    fromPosRef.current.copy(camera.position);
    if (controls) fromTargetRef.current.copy(controls.target);
    else fromTargetRef.current.set(0, 0, 0);
    fromUpRef.current.copy(camera.up);

    toPoseRef.current = PRESETS[preset];

    if (snap) {
      // One-shot: write the destination immediately.
      camera.position.copy(toPoseRef.current.position);
      camera.up.copy(toPoseRef.current.up);
      if (controls) {
        controls.target.copy(toPoseRef.current.target);
        controls.update();
      }
      camera.lookAt(toPoseRef.current.target);
      progressRef.current = 1;
      onSettle?.(preset);
      invalidate();
      return;
    }

    progressRef.current = 0;
    invalidate();
  }, [preset, requestId, suspended, snap, camera, controlsRef, invalidate, onSettle]);

  useFrame((_, delta) => {
    if (suspended) return;
    if (progressRef.current >= 1) return;
    const to = toPoseRef.current;
    if (!to) return;

    progressRef.current = Math.min(
      1,
      progressRef.current + delta * TRANSITION_RATE,
    );
    // Smoothstep-ish ease: cubic in-out so the snap feels deliberate
    // at both ends. 3t²−2t³ has a tidy zero-derivative at the boundaries.
    const t = progressRef.current;
    const k = t * t * (3 - 2 * t);

    camera.position.lerpVectors(fromPosRef.current, to.position, k);
    camera.up.copy(fromUpRef.current).lerp(to.up, k).normalize();
    const controls = controlsRef.current;
    if (controls) {
      controls.target.lerpVectors(fromTargetRef.current, to.target, k);
      controls.update();
    }
    camera.lookAt(controls ? controls.target : to.target);
    invalidate();

    if (progressRef.current >= 1) {
      onSettle?.(preset);
    }
  });

  return null;
}

export default CameraPresetDriver;
