# Brain assets

The brain-viz worker reads two files from this directory at runtime:

```
fsaverage5.glb           cortical surface mesh, 20,484 vertices
fsaverage5.regions.bin   uint8 per-vertex region index (length 20,484)
```

Both are intentionally absent from git. Until they exist, `BrainMesh.tsx`
falls back to the low-poly placeholder mesh, which is the correct behaviour
for the first deliverable.

## Provenance

`fsaverage5` is the FreeSurfer 5th-order subdivided cortical surface
(10,242 vertices per hemisphere → 20,484 total). The exact mesh used here
MUST match what TRIBE v2 evaluates on; the brain-viz worker cannot pick this
unilaterally.

Open question for ml-inference:

> Confirm the cortical surface TRIBE v2 evaluates on. Is it fsaverage5
> (10,242 verts/hemi), `fs_LR_32k`, or a custom downsample? The brain-viz
> renderer expects 20,484 vertices per CONTRACTS.md §4, so the wire format
> already commits us to fsaverage5 — but the geometry to render needs to
> match what produced the activations.

## Format

`fsaverage5.glb` should be a single `Mesh` node, both hemispheres
concatenated in the order matching the `v_0000`..`v_20483` columns from
the activation Parquet. Vertex colours are not required — the renderer
writes them every frame.

`fsaverage5.regions.bin` is a flat `Uint8Array` mapping each vertex to a
region index in the order from `shared/types.ts:REGION_IDS`. Vertices that
fall outside the eight TRIBE regions (medial wall, sulcal floors, etc.)
should be encoded as `255`. The renderer treats `255` as "background" and
paints it at the LUT's low-end colour.

## Sizing

Keep this whole directory under 5 MB. Larger surfaces or HD textures belong
in a release artifact pulled at install time, not committed.
