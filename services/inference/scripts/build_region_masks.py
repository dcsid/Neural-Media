"""Build neural_media_inference/data/region_masks.json from HCP-MMP1.

One-time tool. Atlas = HCP-MMP1 (Glasser et al. 2016, A multi-modal
parcellation of human cerebral cortex, Nature 536:171-178). Projection to
fsaverage = Mills 2016 (doi.org/10.6084/m9.figshare.3498446), redistributed
by MNE (`mne.datasets.fetch_hcp_mmp_parcellation`).

fsaverage5 = the first 10,242 vertices of fsaverage per hemisphere, a
property of FreeSurfer's hierarchical icosahedral subdivision. So we slice
the fsaverage annot to that prefix — no resampling.

Run when masks need refreshing. The committed `region_masks.json` is the
contract; end users never re-run this:

    pip install '.[atlas-build]'  # installs mne + nibabel
    python services/inference/scripts/build_region_masks.py
    git add services/inference/neural_media_inference/data/region_masks.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running this script directly from a clone without `pip install -e`.
_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import mne  # noqa: E402
import nibabel as nib  # noqa: E402

from neural_media_inference._shared import NUM_VERTICES, REGION_IDS  # noqa: E402

FSAVERAGE5_VERTS_PER_HEMI = 10_242
assert FSAVERAGE5_VERTS_PER_HEMI * 2 == NUM_VERTICES

# Curated HCP-MMP1 parcel → our 8 region ids.
#
# Each HCP-MMP1 parcel is bilateral (`L_<name>_ROI` and `R_<name>_ROI`). We
# assign by parcel name; the script handles both hemispheres automatically.
#
# Sources for the mapping:
#   - Glasser 2016 (Nature) — parcel naming and anatomical definitions.
#   - Margalit, Ward, Mitchell, et al. 2020 (J Neurosci) for FFC = FFA.
#   - Klein, Vinckier, Cohen 2015 (Neuropsychologia) for VWFA placement
#     in posterior VVC / lateral PHA / TF.
#   - Tremblay & Dick 2016 (Brain & Language) on lateral language parcels.
#
# Parcels NOT in this table stay unassigned. That includes the medial wall,
# all of motor / somatosensory / posterior parietal / dorsolateral PFC,
# and the bulk of inferior parietal lobule. This is intentional — our 8
# regions are a curated subset, not a full parcellation. (Confirmed
# with terminal 1 as a contract relaxation.)
HCPMMP_TO_REGION: dict[str, str] = {
    # ---- v1 ---------------------------------------------------------------
    "V1": "v1",
    # ---- v2 ---------------------------------------------------------------
    "V2": "v2",
    # ---- v3 ---------------------------------------------------------------
    "V3": "v3",
    "V3A": "v3",
    "V3B": "v3",
    "V3CD": "v3",
    # ---- v4 + lateral occipital ------------------------------------------
    "V4": "v4",
    "V4t": "v4",
    "LO1": "v4",
    "LO2": "v4",
    "LO3": "v4",
    # ---- auditory: primary + belt + early association --------------------
    "A1": "auditory",
    "LBelt": "auditory",
    "MBelt": "auditory",
    "PBelt": "auditory",
    "RI": "auditory",
    "A4": "auditory",
    "A5": "auditory",
    # ---- lateral language network (bilateral; mostly L-dominant) ---------
    "44": "language",
    "45": "language",
    "55b": "language",
    "IFSa": "language",
    "IFSp": "language",
    "PSL": "language",
    "STSva": "language",
    "STSvp": "language",
    "STSda": "language",
    "STSdp": "language",
    "STV": "language",
    # ---- ffa: fusiform face complex --------------------------------------
    "FFC": "ffa",
    # ---- vwfa-neighborhood: ventral visual + posterior PHA + TF ----------
    "VVC": "vwfa",
    "TF": "vwfa",
    "PHA1": "vwfa",
    "PHA2": "vwfa",
    "PHA3": "vwfa",
}


def _strip_parcel_name(raw: str) -> str:
    """`L_V1_ROI` / `R_FFC_ROI` → `V1` / `FFC`."""
    s = raw
    for prefix in ("L_", "R_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if s.endswith("_ROI"):
        s = s[: -len("_ROI")]
    return s


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subjects-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "_fsaverage_cache",
        help="Where MNE caches fsaverage. Build-only; never read at runtime.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=(
            Path(__file__).resolve().parents[1]
            / "neural_media_inference" / "data" / "region_masks.json"
        ),
    )
    args = parser.parse_args(argv)

    args.subjects_dir.mkdir(parents=True, exist_ok=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    print(f"fetching fsaverage → {args.subjects_dir}")
    mne.datasets.fetch_fsaverage(subjects_dir=str(args.subjects_dir), verbose=False)
    print("fetching HCP-MMP1 annot files (license: CC-BY-NC; Glasser 2016)")
    mne.datasets.fetch_hcp_mmp_parcellation(
        subjects_dir=str(args.subjects_dir), accept=True, verbose=False
    )

    masks_by_region: dict[str, list[int]] = {r: [] for r in REGION_IDS}
    parcel_counts: dict[str, int] = {}

    for hemi_idx, hemi in enumerate(["lh", "rh"]):
        annot_path = args.subjects_dir / "fsaverage" / "label" / f"{hemi}.HCPMMP1.annot"
        labels, _ctab, names = nib.freesurfer.read_annot(str(annot_path))
        names_str = [n.decode() if isinstance(n, bytes) else n for n in names]
        labels_fs5 = labels[:FSAVERAGE5_VERTS_PER_HEMI]
        for vert_in_hemi, label_idx in enumerate(labels_fs5):
            if label_idx <= 0:
                continue  # background / medial wall
            parcel = _strip_parcel_name(names_str[label_idx])
            parcel_counts[parcel] = parcel_counts.get(parcel, 0) + 1
            region = HCPMMP_TO_REGION.get(parcel)
            if region is None:
                continue
            global_vertex = hemi_idx * FSAVERAGE5_VERTS_PER_HEMI + vert_in_hemi
            masks_by_region[region].append(global_vertex)

    missing = sorted(set(HCPMMP_TO_REGION) - set(parcel_counts))
    if missing:
        raise RuntimeError(
            f"Parcels listed in HCPMMP_TO_REGION not found in fsaverage annot: {missing}"
        )
    empty = [r for r in REGION_IDS if not masks_by_region[r]]
    if empty:
        raise RuntimeError(f"Regions with no vertices assigned: {empty}")

    total = sum(len(v) for v in masks_by_region.values())
    print(f"coverage: {total} / {NUM_VERTICES} vertices "
          f"({100 * total / NUM_VERTICES:.1f}%) belong to one of the 8 regions")
    for r in REGION_IDS:
        print(f"  {r:9s} {len(masks_by_region[r]):5d} verts")

    payload = {
        "atlas": "HCP-MMP1 (Glasser et al. 2016)",
        "surface": "fsaverage5 (first 10,242 vertices per hemisphere of fsaverage)",
        "source": (
            "MNE fetch_hcp_mmp_parcellation "
            "(Mills 2016, doi.org/10.6084/m9.figshare.3498446)"
        ),
        "num_vertices": NUM_VERTICES,
        "vertex_ordering": "lh[0:10242] then rh[10242:20484]",
        "parcel_to_region": HCPMMP_TO_REGION,
        "masks": {r: sorted(masks_by_region[r]) for r in REGION_IDS},
    }
    args.out.write_text(json.dumps(payload, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
