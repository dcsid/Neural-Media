# Brain assets

Files committed here:

```
fsaverage5.glb           cortical surface mesh, 20,484 vertices  (~720 KB)
fsaverage5.regions.bin   uint8 per-vertex region index, len=20,484
```

Both are loaded at runtime by `apps/web/components/brain/BrainMesh.tsx`.
The HEAD probe on `/brain/fsaverage5.glb` flips the renderer from the
low-poly placeholder to the real cortical surface.

## Render states

`BrainMesh` shows the **low-poly placeholder mesh** (an icosphere coloured
per-region, see `PlaceholderMesh.tsx`) instead of this surface in three
cases, two transient and one persistent:

- **Loading** — the GLB is still parsing. The placeholder renders in
  *wireframe* so the swap-in reads as "the brain is on its way".
- **Asset unavailable / render error** — the HEAD probe 404s or the surface
  throws; the *solid* placeholder stands in so the hero is never a blank
  rectangle.
- **Activation purged** (`activationPurged` prop) — the per-vertex `.npz`
  was deleted after region metrics were aggregated, so there is no
  per-vertex resolution to paint. The solid placeholder renders **plus an
  "Aggregated only" banner** along the bottom. Region-level readings (legend
  and hover) still work — they come from the aggregated per-region means,
  not the purged per-vertex buffer.

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
`services/inference/neural_media_inference/data/region_masks.json:vertex_ordering`).
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

The partitioning comes from **ml-inference's HCP-MMP1 / Glasser parcels**
at `services/inference/neural_media_inference/data/region_masks.json`
(Glasser et al. 2016, fetched via MNE-Python's
`fetch_hcp_mmp_parcellation`, doi.org/10.6084/m9.figshare.3498446).
brain-viz re-bakes that JSON into the byte-aligned `.bin` so the hover
tooltip and the metrics table agree on which region each vertex belongs
to. The masks are **disjoint** — the build script fails loud if any
vertex appears in two regions.

Coverage on fsaverage5 (3,806 / 20,484 vertices, ~18.6 %):

| region   | vertices | source parcels                                   |
|----------|----------|--------------------------------------------------|
| v1       | 523      | V1                                               |
| v2       | 383      | V2                                               |
| v3       | 417      | V3, V3A, V3B, V3CD                               |
| v4       | 320      | V4, V4t, LO1, LO2, LO3                           |
| auditory | 590      | A1, LBelt, MBelt, PBelt, RI, A4, A5              |
| language | 1027     | 44, 45, 55b, IFSa, IFSp, PSL, STSva/p, STSda/dp, STV |
| ffa      | 104      | FFC                                              |
| vwfa     | 442      | VVC, TF, PHA1, PHA2, PHA3                        |

Roughly four hovers in five land on unassigned cortex (255). The mesh
itself is drawn in full because the LUT also paints unassigned vertices
at activation = 0 (the dark end of cividis); only the tooltip suppresses.

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

The geometry and the region mask are now produced by two separate
stdlib-only scripts. They almost always run independently because the
underlying sources change at different rates.

Rebake the **region mask** (run this whenever ml-inference updates
`region_masks.json`):

```
python3 apps/web/components/brain/scripts/build_regions_bin.py
```

Rebuild the **GLB geometry** (rarely — only when the fsaverage5 surface
source itself changes). Network-fetches nilearn's pial GIFTI on first
run, then parses XML + base64 + zlib + float32 and bakes the GLB:

```
python3 apps/web/components/brain/scripts/build_fsaverage5_glb.py \
        apps/web/public/brain
```

## Sizing

Keep this whole directory under 5 MB. Larger surfaces or HD textures
belong in a release artifact pulled at install time, not committed.
Current total: ~740 KB.
