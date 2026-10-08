"""
The City As Sponge — data preparation.

Reads data/Original and writes the grid the page needs into data/Processed:
  - grid.json         compact column arrays, embedded in index.html
  - grid_cells.gpkg   the same cells with polygons, for checking in QGIS / a notebook

Run with:  .venv/bin/python prepare.py
Nothing here depends on a slider; the page computes the run.
All distances and elevations are in feet (NY State Plane Long Island, EPSG:2263).
"""

import json
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from rasterio import features
from rasterio.transform import from_origin
from scipy.interpolate import LinearNDInterpolator
from scipy.spatial import cKDTree
from shapely.geometry import box

ROOT = Path(__file__).parent
ORIG = ROOT / "data" / "Original"
PROC = ROOT / "data" / "Processed"
PROC.mkdir(parents=True, exist_ok=True)

CELL_FT = 500          # grid cell size (prompt: started at 50 ft; agreed 500 ft for citywide)
SAMPLE_FT = 50         # elevation is averaged from samples every 50 ft inside a cell
RASTER_FT = 10         # land-cover polygons are burned into a 10 ft raster to measure fractions
LAND_MIN = 0.5         # a cell is land if at least half of it is mapped, non-open-water land cover

# Confidence of the averaged elevation (agreed thresholds, adapted to 500 ft cells)
HIGH_MIN_POINTS = 3    # at least 3 spot elevations inside the cell ...
HIGH_NEAREST_FT = 150  # ... and one within 150 ft of the cell center

# Share of rainfall each land-cover class turns into runoff-producing surface (0 = pervious, 1 = impervious)
# Semi-pervious values are rational-method runoff coefficients:
#   gravel 0.45 (compacted gravel, 5-yr storm, Iowa DOT Design Manual 4A-5)
#   bare soil 0.25 (upper end for loose soil, rational-method tables)
#   synthetic turf 0.95 (Ohio EPA, Rainwater & Land Development 8.3 Synthetic Turf, draft)
# Open water (ponds inside land cells) does not infiltrate, so it counts as 1.0.
CLASS_VALUE = {
    "asphalt": 1.0, "brick paver": 1.0, "concrete": 1.0, "metal": 1.0, "other": 1.0,
    "pool": 1.0, "rock": 1.0, "roof": 1.0, "solar panel": 1.0, "water": 1.0, "wood": 1.0,
    "bush": 0.0, "grass": 0.0, "sand": 0.0, "tree": 0.0,
    "gravel": 0.45, "bare soil": 0.25, "synthetic turf": 0.95,
    "open water": 1.0,
}
CLASSES = list(CLASS_VALUE)                       # raster code = index + 1, 0 = no data
OPEN_WATER_CODE = CLASSES.index("open water") + 1

BOROUGHS = [("Manhattan", "MN"), ("Bronx", "BX"), ("Brooklyn", "BK"),
            ("Queens", "QN"), ("Staten_Island", "SI")]

t0 = time.time()
def log(msg):
    print(f"[{time.time() - t0:6.0f}s] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Step 1. Read the elevation points and keep only spot elevations (SUB_CODE 300000)
# ---------------------------------------------------------------------------
pts = pd.read_csv(ORIG / "NYC_Planimetric_Database__Elevation_Points_20261007.csv",
                  dtype=str)
log(f"Step 1  elevation records read: {len(pts):,}")
pts["SUB_CODE"] = pts["SUB_CODE"].str.replace(",", "").astype(int)
pts = pts[pts["SUB_CODE"] == 300000].copy()
log(f"Step 1  spot elevations kept (SUB_CODE = 300000): {len(pts):,}")

pts["ELEVATION"] = pd.to_numeric(pts["ELEVATION"].str.replace(",", ""), errors="coerce")
xy = pts["the_geom"].str.extract(r"POINT \(([-\d.]+) ([-\d.]+)\)").astype(float)
bad = pts["ELEVATION"].isna() | xy.isna().any(axis=1)
log(f"Step 1  dropped for missing elevation or coordinates: {bad.sum():,}")
pts, xy = pts[~bad], xy[~bad]

# ---------------------------------------------------------------------------
# Step 2. Reproject points from lat/lon to NY State Plane feet (elevations are already feet)
# ---------------------------------------------------------------------------
gpts = gpd.GeoDataFrame({"elev_ft": pts["ELEVATION"].values},
                        geometry=gpd.points_from_xy(xy[0], xy[1]), crs="EPSG:4326").to_crs("EPSG:2263")
px, py = gpts.geometry.x.values, gpts.geometry.y.values
pz = gpts["elev_ft"].values
log(f"Step 2  points reprojected: {len(pz):,}; elevation range {pz.min():.1f} to {pz.max():.1f} ft")

# ---------------------------------------------------------------------------
# Step 3. Build the 500 ft grid over the extent of the five boroughs' land cover
# ---------------------------------------------------------------------------
bounds = np.array([pyogrio.read_info(ORIG / "Impervious_Surface" / f"{b}.gdb", layer=f"{c}_IApp")["total_bounds"]
                   for b, c in BOROUGHS])
x0 = np.floor(bounds[:, 0].min() / CELL_FT) * CELL_FT - CELL_FT    # one cell of padding all around
y1 = np.ceil(bounds[:, 3].max() / CELL_FT) * CELL_FT + CELL_FT
x1 = np.ceil(bounds[:, 2].max() / CELL_FT) * CELL_FT + CELL_FT
y0 = np.floor(bounds[:, 1].min() / CELL_FT) * CELL_FT - CELL_FT
ncols, nrows = int((x1 - x0) / CELL_FT), int((y1 - y0) / CELL_FT)
log(f"Step 3  grid {ncols} columns x {nrows} rows = {ncols * nrows:,} cells (land and water)")

# ---------------------------------------------------------------------------
# Step 4. Burn the land-cover polygons into a 10 ft raster, then count each class per cell
# ---------------------------------------------------------------------------
k = CELL_FT // RASTER_FT
raster = np.zeros((nrows * k, ncols * k), dtype=np.uint8)
transform = from_origin(x0, y1, RASTER_FT, RASTER_FT)
code_of = {c: i + 1 for i, c in enumerate(CLASSES)}
CHUNK = 500_000
for b, c in BOROUGHS:
    path, layer = ORIG / "Impervious_Surface" / f"{b}.gdb", f"{c}_IApp"
    n = pyogrio.read_info(path, layer=layer)["features"]
    unknown = 0
    for start in range(0, n, CHUNK):
        df = pyogrio.read_dataframe(path, layer=layer, columns=["Class"],
                                    skip_features=start, max_features=CHUNK)
        codes = df["Class"].map(code_of)
        unknown += codes.isna().sum()
        df = df[codes.notna() & df.geometry.notna()]
        features.rasterize(zip(df.geometry.values, codes[df.index].astype(int).values),
                           out=raster, transform=transform)
    log(f"Step 4  {b}: {n:,} land-cover polygons burned; unrecognized class: {unknown}")

# counts[r, c, code] = number of 10 ft pixels of that class in the cell
counts = np.zeros((nrows, ncols, len(CLASSES) + 1), dtype=np.int32)
for code in range(len(CLASSES) + 1):
    counts[:, :, code] = (raster == code).reshape(nrows, k, ncols, k).sum(axis=(1, 3))
del raster
pix = k * k
mapped = pix - counts[:, :, 0]
land_px = mapped - counts[:, :, OPEN_WATER_CODE]
unmapped = counts[:, :, 0]
# The land-cover polygons are tax lots only (every polygon has a BBL), so streets and sidewalks are missing.
# Assumption (agreed): inside a land cell, area with no lot polygon is street/sidewalk and counts as impervious (1.0).
STREET_VALUE = 1.0
values = np.array([STREET_VALUE] + [CLASS_VALUE[c] for c in CLASSES])
impervious = (counts * values).sum(axis=2) / pix

# spot elevations per cell (used to recognize street-only land in Step 5, and for confidence in Step 7)
pc = np.floor((px - x0) / CELL_FT).astype(int)
pr = np.floor((y1 - py) / CELL_FT).astype(int)
ok = (pc >= 0) & (pc < ncols) & (pr >= 0) & (pr < nrows)
pt_grid = np.zeros((nrows, ncols), dtype=np.int32)
np.add.at(pt_grid, (pr[ok], pc[ok]), 1)

# ---------------------------------------------------------------------------
# Step 5. Decide land vs water. A cell is land if at least half of it is lot land cover (not open water),
#         or if it has any lot land cover and at least one spot elevation (spot elevations are surveyed on
#         streets, so they mark land the lot polygons leave out). Everything else (rivers, harbor,
#         Jamaica Bay, areas outside NYC) is drawn as water and is where water leaves the study area.
# ---------------------------------------------------------------------------
by_lots = land_px / pix >= LAND_MIN
by_points = (pt_grid >= 1) & (land_px > 0)
is_land = by_lots | by_points
log(f"Step 5  land cells: {is_land.sum():,} (half or more lot cover: {by_lots.sum():,}; "
    f"added because they hold spot elevations: {(by_points & ~by_lots).sum():,})")
log(f"Step 5  water / outside-NYC cells: {(~is_land).sum():,}")
log(f"Step 5  water cells holding spot elevations but no lot cover (bridges, piers, edges): "
    f"{((pt_grid >= 1) & ~is_land).sum():,} cells, {pt_grid[(pt_grid >= 1) & ~is_land].sum():,} points")

rows, cols = np.nonzero(is_land)
cx = x0 + (cols + 0.5) * CELL_FT
cy = y1 - (rows + 0.5) * CELL_FT

# ---------------------------------------------------------------------------
# Step 6. Interpolate elevation: a TIN (linear triangulated surface) through the spot elevations,
#         sampled every 50 ft inside each land cell and averaged. Samples outside the TIN are ignored;
#         cells with no sample inside the TIN fall back to inverse-distance weighting (8 nearest points).
# ---------------------------------------------------------------------------
log("Step 6  building TIN ...")
tin = LinearNDInterpolator(np.column_stack([px, py]), pz)
m = CELL_FT // SAMPLE_FT
offs = (np.arange(m) + 0.5) * SAMPLE_FT - CELL_FT / 2
ox, oy = np.meshgrid(offs, offs)
sx = (cx[:, None] + ox.ravel()[None, :]).ravel()
sy = (cy[:, None] + oy.ravel()[None, :]).ravel()
samples = tin(sx, sy).reshape(len(cx), m * m)
inside = np.isfinite(samples)
n_inside = inside.sum(axis=1)
with np.errstate(invalid="ignore"):
    elev = np.nanmean(np.where(inside, samples, np.nan), axis=1)
outside_tin = n_inside == 0

tree = cKDTree(np.column_stack([px, py]))
if outside_tin.any():
    d, idx = tree.query(np.column_stack([cx[outside_tin], cy[outside_tin]]), k=8)
    w = 1.0 / np.maximum(d, 1.0) ** 2
    elev[outside_tin] = (pz[idx] * w).sum(axis=1) / w.sum(axis=1)
log(f"Step 6  land cells with TIN elevation: {(~outside_tin).sum():,}; "
    f"outside the TIN (IDW fallback, tagged): {outside_tin.sum():,}")
log(f"Step 6  cell elevation range {elev.min():.1f} to {elev.max():.1f} ft")

# ---------------------------------------------------------------------------
# Step 7. Elevation support: spot elevations inside each cell, distance from cell center to nearest one,
#         and a confidence rating.
# ---------------------------------------------------------------------------
source_count = pt_grid[rows, cols]
nearest_ft, _ = tree.query(np.column_stack([cx, cy]), k=1)

confidence = np.where(outside_tin, "low",
              np.where((source_count >= HIGH_MIN_POINTS) & (nearest_ft <= HIGH_NEAREST_FT), "high",
              np.where(source_count >= 1, "medium", "low")))
for lvl in ["high", "medium", "low"]:
    log(f"Step 7  elevation confidence {lvl}: {(confidence == lvl).sum():,}")
log(f"Step 7  spot elevations falling on water / outside cells: {(~is_land[pr[ok], pc[ok]]).sum():,}")

# ---------------------------------------------------------------------------
# Step 8. Impervious fraction per land cell (lots by class + assumed street area at 1.0), and a flag
#         where most of the cell has no lot record, so the fraction rests mostly on the street assumption.
# ---------------------------------------------------------------------------
imp = impervious[rows, cols]
coverage = mapped[rows, cols] / pix                  # share of the cell with a lot land-cover record
imp_flag = np.where(coverage >= 0.5, "lot record", "mostly assumed street")
log(f"Step 8  impervious fraction mean {imp.mean():.3f}; cells fully impervious (>=0.99): {(imp >= 0.99).sum():,}; "
    f"mostly pervious (<=0.25): {(imp <= 0.25).sum():,}")
log(f"Step 8  share of land area assumed street/sidewalk: {unmapped[rows, cols].sum() / (pix * len(rows)):.3f}")
log(f"Step 8  impervious record: lot record >= half the cell: {(imp_flag == 'lot record').sum():,}; "
    f"mostly assumed street: {(imp_flag == 'mostly assumed street').sum():,}")

# ---------------------------------------------------------------------------
# Step 9. Neighbors (up to 8 land cells) and boundary flag (touches water or the city edge)
# ---------------------------------------------------------------------------
cell_index = -np.ones((nrows, ncols), dtype=np.int64)
cell_index[rows, cols] = np.arange(len(rows))
neighbor_ids, is_boundary = [], np.zeros(len(rows), dtype=bool)
for i, (r, c) in enumerate(zip(rows, cols)):
    nb = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            j = cell_index[r + dr, c + dc]       # padding guarantees r+dr, c+dc are inside the array
            if j >= 0:
                nb.append(int(j))
    neighbor_ids.append(nb)
    is_boundary[i] = len(nb) < 8
log(f"Step 9  boundary cells (touch water or outside): {is_boundary.sum():,}; interior: {(~is_boundary).sum():,}")

# ---------------------------------------------------------------------------
# Step 10. Write outputs
# ---------------------------------------------------------------------------
cell_id = np.arange(len(rows))
gdf = gpd.GeoDataFrame({
    "cell_id": cell_id, "row": rows, "column": cols,
    "centroid_x": cx, "centroid_y": cy,
    "elevation_ft": elev.round(2),
    "elevation_source_count": source_count,
    "nearest_elevation_point_ft": nearest_ft.round(1),
    "elevation_confidence": confidence,
    "outside_interpolation": outside_tin,
    "impervious_fraction": imp.round(3),
    "landcover_coverage": coverage.round(3),
    "impervious_record": imp_flag,
    "neighbor_ids": [",".join(map(str, nb)) for nb in neighbor_ids],
    "is_boundary": is_boundary,
    "water_depth_in": 0.0,
}, geometry=[box(x - CELL_FT / 2, y - CELL_FT / 2, x + CELL_FT / 2, y + CELL_FT / 2) for x, y in zip(cx, cy)],
   crs="EPSG:2263")
gdf.to_file(PROC / "grid_cells.gpkg", driver="GPKG")

conf_code = {"high": 0, "medium": 1, "low": 2}
compact = {
    "meta": {"crs": "EPSG:2263", "cell_ft": CELL_FT, "x0": x0, "y1": y1, "nrows": nrows, "ncols": ncols,
             "confidence_codes": ["high", "medium", "low"],
             "class_values": CLASS_VALUE, "n_spot_elevations": int(len(pz))},
    "row": rows.tolist(), "col": cols.tolist(),
    "elev": elev.round(2).tolist(),
    "imp": imp.round(3).tolist(),
    "cov": coverage.round(2).tolist(),
    "conf": [conf_code[c] for c in confidence],
    "outside": outside_tin.astype(int).tolist(),
    "npts": source_count.tolist(),
    "near": nearest_ft.round(0).astype(int).tolist(),
    "nb": neighbor_ids,
    "boundary": is_boundary.astype(int).tolist(),
}
(PROC / "grid.json").write_text(json.dumps(compact, separators=(",", ":")))
log(f"Step 10 wrote {len(gdf):,} land cells -> grid.json "
    f"({(PROC / 'grid.json').stat().st_size / 1e6:.1f} MB) and grid_cells.gpkg")
