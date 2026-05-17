"""
Build apps/web/public/brain/fsaverage5.regions.bin from ml-inference's
HCP-MMP1 / Glasser parcellation.

Source: services/inference/neural_media_inference/data/region_masks.json
        (canonical atlas for the inference aggregator — same masks the
        API serves /api/v1/videos/{id}/metrics through).

The committed .bin is byte-aligned with that JSON so the hover tooltip
in BrainMesh agrees with the metrics table for the same vertex. When
ml-inference updates region_masks.json, re-run this script to keep
brain-viz in lockstep.

Run (from repo root):

    python3 apps/web/components/brain/scripts/build_regions_bin.py \\
            apps/web/public/brain/fsaverage5.regions.bin

Stdlib-only. The fsaverage5 *geometry* lives in fsaverage5.glb and is
rebuilt by build_fsaverage5_glb.py — that is a separate, slower
operation because it network-fetches the GIFTI sources.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Mirrors shared/types.ts REGION_IDS. Order is load-bearing: the byte
# value in the .bin indexes into this list when CorticalSurface paints
# vertex colours.
REGION_ORDER = ("v1", "v2", "v3", "v4", "auditory", "language", "ffa", "vwfa")

NUM_VERTICES = 20_484
UNASSIGNED = 255  # CorticalSurface treats this as "no region" — hover suppressed.

# Default paths assume repo-root cwd.
DEFAULT_MASKS_PATH = Path(
    "services/inference/neural_media_inference/data/region_masks.json"
)
DEFAULT_OUT_PATH = Path("apps/web/public/brain/fsaverage5.regions.bin")


def load_masks(path: Path) -> dict:
    """Load and lightly validate the atlas JSON."""
    data = json.loads(path.read_text())

    nv = data.get("num_vertices")
    if nv != NUM_VERTICES:
        raise ValueError(
            f"{path} declares num_vertices={nv}, expected {NUM_VERTICES}"
        )

    ordering = data.get("vertex_ordering", "")
    if "lh" not in ordering or "rh" not in ordering:
        raise ValueError(
            f"{path} vertex_ordering={ordering!r}; expected lh-then-rh "
            "to match fsaverage5.glb concatenation"
        )

    masks = data.get("masks")
    if not isinstance(masks, dict):
        raise ValueError(f"{path} missing 'masks' object")

    if set(masks.keys()) != set(REGION_ORDER):
        raise ValueError(
            f"masks keys {sorted(masks.keys())} != REGION_ORDER {list(REGION_ORDER)}"
        )

    return masks


def bake(masks: dict) -> bytes:
    """Build the uint8 region buffer. 0..7 = REGION_ORDER, 255 = unassigned.

    The HCP-MMP1 masks the inference aggregator ships only cover ~18.6%
    of cortical vertices (the eight canonical TRIBE regions); the rest
    of cortex stays at 255. Overlap between regions is treated as a hard
    error rather than silently picking a winner — if it ever happens it
    means ml-inference shipped a malformed atlas and we want to fail
    loud.
    """
    buf = bytearray([UNASSIGNED] * NUM_VERTICES)

    for region_idx, region in enumerate(REGION_ORDER):
        verts = masks[region]
        if not isinstance(verts, list):
            raise ValueError(f"masks[{region!r}] is not a list")
        for v in verts:
            if not isinstance(v, int) or v < 0 or v >= NUM_VERTICES:
                raise ValueError(
                    f"masks[{region!r}] contains out-of-range vertex {v}"
                )
            if buf[v] != UNASSIGNED:
                prev = REGION_ORDER[buf[v]]
                raise ValueError(
                    f"vertex {v} appears in both {prev!r} and {region!r}; "
                    "regions must be disjoint"
                )
            buf[v] = region_idx

    return bytes(buf)


def report_coverage(buf: bytes) -> None:
    counts = [0] * 8
    unassigned = 0
    for b in buf:
        if b == UNASSIGNED:
            unassigned += 1
        else:
            counts[b] += 1
    print(f"  {'region':10s}  verts   %")
    for i, region in enumerate(REGION_ORDER):
        pct = 100 * counts[i] / NUM_VERTICES
        print(f"  {region:10s} {counts[i]:6d}  {pct:5.2f}")
    pct = 100 * unassigned / NUM_VERTICES
    print(f"  {'(unassigned)':12s} {unassigned:6d}  {pct:5.2f}")


def main():
    masks_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MASKS_PATH
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT_PATH

    if not masks_path.exists():
        raise SystemExit(f"region_masks.json not found at {masks_path}")

    masks = load_masks(masks_path)
    buf = bake(masks)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(buf)

    print(f"wrote {out_path} ({len(buf)} bytes)")
    report_coverage(buf)


if __name__ == "__main__":
    main()
