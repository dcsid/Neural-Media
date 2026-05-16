import { REGION_IDS, type RegionId } from "@shared/types";

// Placeholder region partitioning for the low-poly fallback mesh.
//
// Real region masks come from the fsaverage atlas once
// apps/web/public/brain/fsaverage5.glb is dropped in and the surface loader
// path is exercised. Until then the placeholder partitions an icosahedron
// by surface-position bins so the eight TRIBE regions are at least visually
// distinguishable on the hero mesh.

export const PLACEHOLDER_REGION_ORDER: ReadonlyArray<RegionId> = REGION_IDS;

// Maps a unit-sphere direction (x, y, z) on the placeholder icosphere to one
// of the 8 canonical regions. The partitioning is geometric — back-of-head =
// visual, sides = auditory/language, ventral = face/word — so the placeholder
// at least gestures at the real anatomy. It is NOT anatomically valid; the
// real fsaverage mesh replaces this entirely.
export function placeholderRegionForDir(
  x: number,
  y: number,
  z: number,
): RegionId {
  const len = Math.hypot(x, y, z) || 1;
  const nx = x / len;
  const ny = y / len;
  const nz = z / len;

  if (nz < -0.35) {
    if (ny < -0.2) return "v1";
    if (Math.abs(nx) > 0.55) return "v2";
    return "v3";
  }
  if (nz > 0.55) return "v4";
  if (ny < -0.5) {
    return Math.abs(nx) > 0.45 ? "ffa" : "vwfa";
  }
  if (Math.abs(nx) > 0.65) return "auditory";
  return "language";
}
