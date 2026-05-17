"""
Build apps/web/public/brain/fsaverage5.glb + fsaverage5.regions.bin.

Source: nilearn's vendored fsaverage5 pial GIFTI files (lh + rh), 10,242
vertices per hemisphere. Concatenated left-then-right to give 20,484
vertices — matching the column order in TRIBE's per-vertex output and
the placeholder vertex slabs in
services/inference/neural_media_inference/aggregate.py.

License of the source data: FreeSurfer's fsaverage subject, redistributed
by nilearn under their BSD license. The FreeSurfer Software License
(research / non-commercial) covers the underlying surface — compatible
with Neural Media's CC-BY-NC-4.0.

Run (from repo root):

    python3 apps/web/components/brain/scripts/build_fsaverage5_glb.py \\
            apps/web/public/brain

Stdlib-only — no nibabel, no numpy. GIFTI is XML+base64+zlib+float32, and
GLB is a small JSON+binary container. Both are encodable by hand.
"""
from __future__ import annotations

import base64
import gzip
import io
import zlib
import json
import math
import struct
import sys
import urllib.request
import xml.etree.ElementTree as ET
from array import array
from pathlib import Path

NILEARN_RAW = (
    "https://raw.githubusercontent.com/nilearn/nilearn/main/"
    "nilearn/datasets/data/fsaverage5"
)
SOURCES = {
    "pial_left.gii": f"{NILEARN_RAW}/pial_left.gii.gz",
    "pial_right.gii": f"{NILEARN_RAW}/pial_right.gii.gz",
}

# ---------------------------------------------------------------------------
# GIFTI parsing
# ---------------------------------------------------------------------------

GIFTI_DTYPE = {
    "NIFTI_TYPE_FLOAT32": ("f", 4),
    "NIFTI_TYPE_INT32": ("i", 4),
}


def parse_gifti(path: Path):
    """Return (positions[Float32, V*3], faces[Int32, F*3])."""
    root = ET.parse(path).getroot()
    pos = None
    tri = None
    for da in root.findall("DataArray"):
        intent = da.attrib["Intent"]
        dtype = da.attrib["DataType"]
        encoding = da.attrib["Encoding"]
        endian = da.attrib["Endian"]
        if endian != "LittleEndian":
            raise ValueError(f"Unsupported endian {endian!r} in {path}")
        dim0 = int(da.attrib["Dim0"])
        dim1 = int(da.attrib["Dim1"])
        if dtype not in GIFTI_DTYPE:
            raise ValueError(f"Unsupported DataType {dtype!r} in {path}")
        fmt, _ = GIFTI_DTYPE[dtype]

        data_el = da.find("Data")
        assert data_el is not None and data_el.text is not None
        b64 = data_el.text.strip()
        raw = base64.b64decode(b64)
        if encoding == "GZipBase64Binary":
            # GIFTI spec uses zlib here despite the name. Some writers emit
            # actual gzip-wrapped data, so try gzip first and fall back.
            try:
                raw = gzip.decompress(raw)
            except (OSError, gzip.BadGzipFile):
                raw = zlib.decompress(raw)
        elif encoding != "Base64Binary":
            raise ValueError(f"Unsupported Encoding {encoding!r}")

        arr = array(fmt)
        arr.frombytes(raw)
        if len(arr) != dim0 * dim1:
            raise ValueError(
                f"DataArray length {len(arr)} != {dim0}*{dim1} in {path}"
            )

        if intent == "NIFTI_INTENT_POINTSET":
            pos = arr
        elif intent == "NIFTI_INTENT_TRIANGLE":
            tri = arr

    if pos is None or tri is None:
        raise ValueError(f"GIFTI missing pointset or triangle in {path}")
    return pos, tri


# ---------------------------------------------------------------------------
# Mesh utilities
# ---------------------------------------------------------------------------

def concat_hemispheres(lh_pos, lh_tri, rh_pos, rh_tri):
    """Left first, then right. Right's triangle indices get offset."""
    nL = len(lh_pos) // 3
    positions = array("f")
    positions.extend(lh_pos)
    positions.extend(rh_pos)

    indices = array("I")  # uint32 staging; we'll downcast to uint16 below
    for i in lh_tri:
        indices.append(i)
    for i in rh_tri:
        indices.append(i + nL)
    return positions, indices, nL


def compute_normals(positions: array, indices: array) -> array:
    """Smooth per-vertex normals, area-weighted by triangle cross product."""
    nv = len(positions) // 3
    normals = [0.0] * (nv * 3)

    px = positions  # local alias for speed
    nf = len(indices) // 3
    for f in range(nf):
        a = indices[3 * f]
        b = indices[3 * f + 1]
        c = indices[3 * f + 2]
        ax, ay, az = px[3*a], px[3*a+1], px[3*a+2]
        bx, by, bz = px[3*b], px[3*b+1], px[3*b+2]
        cx, cy, cz = px[3*c], px[3*c+1], px[3*c+2]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        for idx in (a, b, c):
            normals[3*idx]     += nx
            normals[3*idx + 1] += ny
            normals[3*idx + 2] += nz

    out = array("f", [0.0] * (nv * 3))
    for i in range(nv):
        x, y, z = normals[3*i], normals[3*i+1], normals[3*i+2]
        L = math.sqrt(x*x + y*y + z*z) or 1.0
        out[3*i]     = x / L
        out[3*i + 1] = y / L
        out[3*i + 2] = z / L
    return out


def bbox(positions: array):
    mn = [positions[0], positions[1], positions[2]]
    mx = [positions[0], positions[1], positions[2]]
    for i in range(0, len(positions), 3):
        for j in range(3):
            v = positions[i + j]
            if v < mn[j]: mn[j] = v
            if v > mx[j]: mx[j] = v
    return mn, mx


def recenter_unit(positions: array):
    """Center on origin and scale to fit roughly in a unit-radius sphere."""
    mn, mx = bbox(positions)
    cx = (mn[0] + mx[0]) / 2
    cy = (mn[1] + mx[1]) / 2
    cz = (mn[2] + mx[2]) / 2
    extent = max(mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]) / 2 or 1.0
    s = 1.0 / extent
    for i in range(0, len(positions), 3):
        positions[i]     = (positions[i]     - cx) * s
        positions[i + 1] = (positions[i + 1] - cy) * s
        positions[i + 2] = (positions[i + 2] - cz) * s


# ---------------------------------------------------------------------------
# GLB writing
# ---------------------------------------------------------------------------

def write_glb(out_path: Path, positions: array, normals: array, indices: array):
    nv = len(positions) // 3
    nidx = len(indices)
    if max(indices) >= 65536:
        raise ValueError("Index overflow — would need uint32 indices")

    # Downcast indices to uint16.
    idx16 = array("H", indices)

    # Bin layout: positions, normals, indices. Each section padded to 4 bytes.
    def pad4(n): return (4 - (n % 4)) % 4

    pos_bytes = positions.tobytes()
    nrm_bytes = normals.tobytes()
    idx_bytes = idx16.tobytes()

    bin_chunks = []
    offsets = []
    cur = 0
    for b in (pos_bytes, nrm_bytes, idx_bytes):
        offsets.append((cur, len(b)))
        bin_chunks.append(b)
        cur += len(b)
        pad = pad4(cur)
        if pad:
            bin_chunks.append(b"\x00" * pad)
            cur += pad
    bin_blob = b"".join(bin_chunks)

    mn, mx = bbox(positions)

    gltf = {
        "asset": {
            "version": "2.0",
            "generator": "neural-media brain-viz / build_glb.py",
            "copyright": "fsaverage5 pial surface, FreeSurfer (research license)",
        },
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{
            "primitives": [{
                "attributes": {"POSITION": 0, "NORMAL": 1},
                "indices": 2,
                "mode": 4,  # TRIANGLES
            }],
        }],
        "buffers": [{"byteLength": len(bin_blob)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": offsets[0][0],
             "byteLength": offsets[0][1], "target": 34962},  # ARRAY_BUFFER
            {"buffer": 0, "byteOffset": offsets[1][0],
             "byteLength": offsets[1][1], "target": 34962},
            {"buffer": 0, "byteOffset": offsets[2][0],
             "byteLength": offsets[2][1], "target": 34963},  # ELEMENT_ARRAY_BUFFER
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126,
             "count": nv, "type": "VEC3", "min": mn, "max": mx},
            {"bufferView": 1, "componentType": 5126,
             "count": nv, "type": "VEC3"},
            {"bufferView": 2, "componentType": 5123,
             "count": nidx, "type": "SCALAR"},
        ],
    }

    json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_pad = pad4(len(json_bytes))
    if json_pad:
        json_bytes += b" " * json_pad

    bin_pad = pad4(len(bin_blob))
    if bin_pad:
        bin_blob += b"\x00" * bin_pad

    total = 12 + 8 + len(json_bytes) + 8 + len(bin_blob)

    out = io.BytesIO()
    out.write(struct.pack("<III", 0x46546C67, 2, total))  # "glTF", v2, total
    out.write(struct.pack("<II", len(json_bytes), 0x4E4F534A))  # "JSON"
    out.write(json_bytes)
    out.write(struct.pack("<II", len(bin_blob), 0x004E4942))  # "BIN\0"
    out.write(bin_blob)

    out_path.write_bytes(out.getvalue())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
#
# The region mask (fsaverage5.regions.bin) is built by a sibling script,
# build_regions_bin.py, which consumes ml-inference's region_masks.json.
# Keeping them separate lets you rebake the regions without re-fetching
# the GIFTI sources — and the regions file changes more often than the
# geometry does.

def fetch_sources(cache_dir: Path) -> Path:
    """Download nilearn's fsaverage5 pial GIFTI files into a cache dir."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        dest = cache_dir / name
        if dest.exists():
            continue
        gz_dest = cache_dir / f"{name}.gz"
        print(f"fetch {url} -> {gz_dest}")
        urllib.request.urlretrieve(url, gz_dest)
        with gzip.open(gz_dest, "rb") as src, open(dest, "wb") as dst:
            dst.write(src.read())
    return cache_dir


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/nm-surface")

    src_dir = fetch_sources(cache_dir)
    lh_pos, lh_tri = parse_gifti(src_dir / "pial_left.gii")
    rh_pos, rh_tri = parse_gifti(src_dir / "pial_right.gii")
    print(f"lh: {len(lh_pos)//3} verts, {len(lh_tri)//3} tris")
    print(f"rh: {len(rh_pos)//3} verts, {len(rh_tri)//3} tris")

    positions, indices, nL = concat_hemispheres(lh_pos, lh_tri, rh_pos, rh_tri)
    nv = len(positions) // 3
    assert nv == 20484, f"expected 20484 vertices, got {nv}"
    print(f"combined: {nv} verts, {len(indices)//3} tris")

    recenter_unit(positions)
    normals = compute_normals(positions, indices)
    write_glb(out_dir / "fsaverage5.glb", positions, normals, indices)

    glb_size = (out_dir / "fsaverage5.glb").stat().st_size
    print(f"wrote fsaverage5.glb ({glb_size/1024:.1f} KB)")
    print("(region mask: run build_regions_bin.py separately)")

if __name__ == "__main__":
    main()
