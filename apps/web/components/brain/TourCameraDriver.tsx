"use client";

import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useRef } from "react";
import * as THREE from "three";
import type { MutableRefObject } from "react";
import type { TourFrame } from "./hooks/useTour";

// Structural subset of three-stdlib's OrbitControls so we don't have to
// import the package directly (it isn't a hoisted dependency of apps/web).
// Only the surface area TourCameraDriver actually touches is included.
interface OrbitControlsLike {
  enabled: boolean;
  target: THREE.Vector3;
  update: () => void;
}

// Canvas-internal companion to useTour. Lives inside <Canvas> so it can
// useThree() to read the live camera + useFrame to drive it per RAF tick.
//
// Why a dedicated component instead of doing this inline in BrainMesh:
//   BrainMesh's body runs outside the Canvas's reconciler; useThree /
//   useFrame would throw there. Splitting this out keeps BrainMesh as a
//   plain React component and keeps the camera writes inside the R3F
//   render loop where invalidate() is implicit and there's no stutter.
//
// Coexistence with OrbitControls:
//   When `active` is true we set the controls' `enabled` flag to false
//   so the user can't rotate against us, and we write the camera
//   position from (azimuth, polar) around the controls' current target.
//   `controls.update()` re-syncs the controls' internal spherical state
//   so when the tour ends and we re-enable OrbitControls there's no
//   visual snap back to wherever the user had left off — the controls
//   pick up exactly where the tour left the camera.
//
// On tour end we ease ~300ms back to the canonical default before
// handing control back. The ease blends from the last tour camera
// position to (0, 0, RADIUS) using a tiny exponential smoothing so the
// transition reads as "swooshes home" rather than "jumps home".

const DEFAULT_RADIUS = 3.2;
const DEFAULT_POSITION = new THREE.Vector3(0, 0, DEFAULT_RADIUS);
const SETTLE_SECONDS = 0.3;
const SETTLE_RATE = 1 / SETTLE_SECONDS;

interface TourCameraDriverProps {
  active: boolean;
  frame: TourFrame;
  // Accepts any ref whose .current is null-or-OrbitControlsLike. The
  // refinement to `OrbitControlsLike | null` covers both ours and the
  // drei-generated React.ComponentRef<typeof OrbitControls> shape, which
  // is structurally compatible.
  controlsRef: MutableRefObject<OrbitControlsLike | null>;
  // When true, skip the camera write entirely. Used so prefers-reduced-
  // motion users can still trigger the tour (region highlights + timeline
  // scrub) without an automatic rotation that they've opted out of.
  skipCameraDrive: boolean;
}

export function TourCameraDriver({
  active,
  frame,
  controlsRef,
  skipCameraDrive,
}: TourCameraDriverProps) {
  const camera = useThree((s) => s.camera);
  const invalidate = useThree((s) => s.invalidate);

  // Tracks "we were active last frame" so we can run the settle phase
  // after `active` flips to false. Without this the camera would snap
  // back instantly on tour end.
  const wasActiveRef = useRef(false);
  const settleProgressRef = useRef(1);  // 1 = fully settled

  // Toggle OrbitControls enable state on tour start/end. We also stash
  // any previous setting in case the host passes a non-default in the
  // future — restoring is safer than hardcoding true.
  const prevEnabledRef = useRef<boolean | null>(null);
  useEffect(() => {
    const controls = controlsRef.current;
    if (!controls) return;
    if (active && !skipCameraDrive) {
      if (prevEnabledRef.current === null) {
        prevEnabledRef.current = controls.enabled;
      }
      controls.enabled = false;
    } else if (prevEnabledRef.current !== null) {
      controls.enabled = prevEnabledRef.current;
      prevEnabledRef.current = null;
    }
    invalidate();
  }, [active, skipCameraDrive, controlsRef, invalidate]);

  useFrame((_, delta) => {
    const controls = controlsRef.current;
    const target = controls ? controls.target : new THREE.Vector3(0, 0, 0);

    if (active && !skipCameraDrive) {
      // Spherical → cartesian around the controls' target. Three's
      // convention for spherical is (radius, phi-polar, theta-azimuth).
      const r = DEFAULT_RADIUS;
      const phi = frame.cameraPolar;
      const theta = frame.cameraAzimuth;
      const sinPhi = Math.sin(phi);
      const x = target.x + r * sinPhi * Math.sin(theta);
      const y = target.y + r * Math.cos(phi);
      const z = target.z + r * sinPhi * Math.cos(theta);
      camera.position.set(x, y, z);
      camera.lookAt(target);
      controls?.update();
      wasActiveRef.current = true;
      settleProgressRef.current = 0;
      invalidate();
      return;
    }

    // Settle phase. Runs immediately after `active` flips false (or if
    // we were ever active and need to ease back home). Skipped entirely
    // if the tour never camera-drove (skipCameraDrive=true) — there's
    // nothing to settle back from in that case.
    if (
      wasActiveRef.current &&
      !skipCameraDrive &&
      settleProgressRef.current < 1
    ) {
      settleProgressRef.current = Math.min(
        1,
        settleProgressRef.current + delta * SETTLE_RATE,
      );
      const k = settleProgressRef.current;
      // Exponential ease-out feel: blend current → default with k.
      camera.position.lerp(DEFAULT_POSITION, k);
      camera.lookAt(target);
      controls?.update();
      invalidate();
      if (settleProgressRef.current >= 1) {
        wasActiveRef.current = false;
      }
    }
  });

  return null;
}

export default TourCameraDriver;
