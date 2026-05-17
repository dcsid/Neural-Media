# Brain assets

Files committed here:

```
fsaverage5.glb           cortical surface mesh, 20,484 vertices  (~720 KB)
fsaverage5.regions.bin   uint8 per-vertex region index, len=20,484
```

Both are loaded at runtime by `apps/web/components/brain/BrainMesh.tsx`.
The HEAD probe on `/brain/fsaverage5.glb` flips the renderer from the
low-poly placeholder to the real cortical surface.

## Provenance

### Surface mesh

`fsaverage5` is the FreeSurfer 5th-order subdivided cortical surface
(10,242 vertices per hemisphere → 20,484 total). The pial surface is
sourced from [nilearn's vendored dataset](https://github.com/nilearn/nilearn/tree/main/nilearn/datasets/data/fsaverage5),
which redistributes it under their BSD license. The underlying surface
geometry is covered by the FreeSurfer Software License (research /
non-commercial use), which is compatible with Neural Media's
CC-BY-NC-4.0.

Hemispheres are concatenated **left first, then right** — this is the
vertex order the inference pipeline writes to its activation Parquet
columns `v_0000`..`v_20483` (see CONTRACTS.md §4 and
`services/inference/neural_media_inference/aggregate.py:REGION_VERTEX_MASKS`).
Triangle indices for the right hemisphere are offset by 10,242 in the
combined buffer.

After concatenation the mesh is **recentered on the origin** and **scaled
to fit a unit-radius sphere** so the same camera (`position=[0,0,3.2]`,
fov=35°) frames it without per-instance tuning. Smooth per-vertex normals
are baked in at build time to keep first-paint cheap.

GLB structure: single mesh, single primitive, mode=TRIANGLES,
attributes={POSITION, NORMAL}, indices=uint16 (fits — max index 20,483
< 65,536). No textures, no skinning, no animations. Uncompressed —
Draco was unnecessary at 720 KB.

### Region mask

`fsaverage5.regions.bin` is a flat `Uint8Array` of length 20,484 mapping
each vertex to a region index in the order defined by
`shared/types.ts:REGION_IDS`:

```
0  v1
1  v2
2  v3
3  v4
4  auditory
5  language
6  ffa
7  vwfa
255  background (medial wall / unassigned)
```

The partitioning is **the same placeholder slab assignment that the
inference aggregator uses**
(`services/inference/neural_media_inference/aggregate.py::REGION_VERTEX_MASKS`):

| region   | range (vertex indices) | count  |
|----------|------------------------|--------|
| v1       | `[0, 3000)`            | 3000   |
| v2       | `[3000, 5500)`         | 2500   |
| v3       | `[5500, 7500)`         | 2000   |
| v4       | `[7500, 9100)`         | 1600   |
| auditory | `[9100, 11100)`        | 2000   |
| language | `[11100, 14300)`       | 3200   |
| ffa      | `[14300, 16800)`       | 2500   |
| vwfa     | `[16800, 20484)`       | 3684   |

These slabs are **not anatomically valid** — they are a vertical-slice
placeholder that ml-inference and brain-viz share so the hover readout
matches the metrics table. Replace both sides in lockstep when a real
parcellation lands.

### Why this lives in `/public/` and not behind `/api/v1/regions`

The current `/api/v1/regions` endpoint only returns `(region_id,
description)` pairs — no vertex masks (see
`services/api/neural_media_api/main.py` and `shared/schemas.py:RegionDef`).
Asking the API for the mask would require a coordinated change to the
shared schemas. Until that lands, brain-viz ships the mask alongside the
geometry so hover works offline. The frontend caches it module-globally
via `useRegionMask()` so the fetch happens at most once per session.

When `/api/v1/regions` does start returning masks, this file can be
deleted and the hook can swap to the endpoint without other changes.

## Rebuild

```
python3 apps/web/components/brain/scripts/build_fsaverage5_glb.py \
        apps/web/public/brain
```

The script is stdlib-only (no nibabel / no numpy). On first run it
downloads the two pial GIFTI files from nilearn's GitHub into
`/tmp/nm-surface` (or a path you pass as the second argument), parses
them by hand (XML + base64 + zlib + float32), and writes the GLB +
region mask into the directory you pass as the first argument.

## Sizing

Keep this whole directory under 5 MB. Larger surfaces or HD textures
belong in a release artifact pulled at install time, not committed.
Current total: ~740 KB.
