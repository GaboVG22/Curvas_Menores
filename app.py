
from __future__ import annotations

import io
import json
import math
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import urlencode

import requests
import streamlit as st

APP_MODE = "MENOR_100K"
APP_TITLE = "HidroSed Curvas · Cuencas Menores a 100.000 km²"
APP_LIMIT_TEXT = "cuencas menores a 100.000 km²"
DEFAULT_MARGIN_KM = 40
DEFAULT_INTERVAL_INDEX = 5
DEFAULT_TILE_ROWS = 5
DEFAULT_TILE_COLS = 5
DEFAULT_MAX_LEVELS = 10000

BASE_URL = "https://portal.opentopography.org/API/globaldem"

st.set_page_config(page_title=APP_TITLE, page_icon="🌊", layout="wide")

OUT = Path("outputs")
OUT.mkdir(exist_ok=True)
if "project_id" not in st.session_state:
    st.session_state["project_id"] = str(int(time.time()))
PROJECT = OUT / st.session_state["project_id"]
PROJECT.mkdir(exist_ok=True)

st.markdown(
    f"""
<style>
.block-container {{padding-top:1.2rem; max-width:1500px;}}
.hero {{background:linear-gradient(135deg,#0b5cad,#00a0b0); color:white; padding:1.1rem 1.4rem; border-radius:18px; margin-bottom:1rem;}}
.hero h1 {{margin:0; font-size:2rem;}}
.hero p {{margin:.35rem 0 0 0; opacity:.96;}}
.ok {{background:#eaf6ff; border-left:5px solid #0b5cad; padding:.75rem; border-radius:10px;}}
.warn {{background:#fff7e6; border-left:5px solid #f59e0b; padding:.75rem; border-radius:10px;}}
</style>
<div class="hero">
<h1>🌊 {APP_TITLE}</h1>
<p>Generador de DEM, curvas de nivel KMZ y eje principal de cauce para {APP_LIMIT_TEXT}. Versión verificada v1.1.</p>
</div>
""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------
def has(key: str) -> bool:
    return key in st.session_state and st.session_state[key] is not None


def save_bytes(name: str, data: bytes) -> Path:
    path = PROJECT / name
    path.write_bytes(data)
    return path


def read_upload_bytes(uploaded):
    uploaded.seek(0)
    return uploaded.read()


def read_kml_from_upload(uploaded) -> str:
    data = read_upload_bytes(uploaded)
    name = uploaded.name.lower()
    if name.endswith(".kmz"):
        with zipfile.ZipFile(io.BytesIO(data), "r") as z:
            kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not kmls:
                raise ValueError("El KMZ no contiene KML.")
            return z.read(kmls[0]).decode("utf-8", errors="ignore")
    if name.endswith(".kml"):
        return data.decode("utf-8", errors="ignore")
    raise ValueError("Debe cargar KMZ o KML.")


def parse_coord_text(text: str):
    coords = []
    for tok in re.split(r"\s+", (text or "").strip()):
        if not tok:
            continue
        vals = tok.split(",")
        if len(vals) >= 2:
            try:
                lon = float(vals[0])
                lat = float(vals[1])
                z = float(vals[2]) if len(vals) >= 3 and vals[2] else 0.0
                coords.append((lon, lat, z))
            except Exception:
                pass
    return coords


def parse_first_point(kml: str):
    name_m = re.search(r"<name[^>]*>(.*?)</name>", kml, flags=re.I | re.S)
    name = re.sub("<.*?>", "", name_m.group(1)).strip() if name_m else "Punto de control"
    for block in re.findall(r"<coordinates[^>]*>(.*?)</coordinates>", kml, flags=re.I | re.S):
        coords = parse_coord_text(block)
        if coords:
            lon, lat, _ = coords[0]
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return {"name": name, "lat": lat, "lon": lon}
    raise ValueError("No se encontró un punto válido en el KMZ/KML.")


def parse_first_linestring(kml: str):
    m = re.search(r"<LineString[^>]*>.*?<coordinates[^>]*>(.*?)</coordinates>.*?</LineString>", kml, flags=re.I | re.S)
    if not m:
        raise ValueError("No se encontró LineString en el KMZ/KML.")
    coords = parse_coord_text(m.group(1))
    if len(coords) < 2:
        raise ValueError("El eje no tiene suficientes puntos.")
    return [(lon, lat) for lon, lat, _ in coords]


def kml_has_polygon(kml: str):
    return "<Polygon" in kml or "<gx:MultiTrack" in kml


def bbox_from_margin(lat, lon, margin, unit):
    if unit == "km":
        dlat = margin / 111.32
        dlon = margin / (111.32 * max(0.01, math.cos(math.radians(lat))))
    else:
        dlat = margin
        dlon = margin
    return {
        "south": round(lat - dlat, 8),
        "north": round(lat + dlat, 8),
        "west": round(lon - dlon, 8),
        "east": round(lon + dlon, 8),
    }


def bbox_area_km2(bbox):
    r = 6371.0088
    s = math.radians(bbox["south"])
    n = math.radians(bbox["north"])
    w = math.radians(bbox["west"])
    e = math.radians(bbox["east"])
    return r*r*abs(math.sin(n)-math.sin(s))*abs(e-w)


def build_url(dem_type, bbox, key="API_KEY_OCULTA"):
    params = {
        "demtype": dem_type,
        "south": bbox["south"],
        "north": bbox["north"],
        "west": bbox["west"],
        "east": bbox["east"],
        "outputFormat": "GTiff",
        "API_Key": key,
    }
    return f"{BASE_URL}?{urlencode(params)}"


def split_bbox(bbox, rows, cols):
    tiles = []
    for i in range(rows):
        south = bbox["south"] + (bbox["north"] - bbox["south"]) * i / rows
        north = bbox["south"] + (bbox["north"] - bbox["south"]) * (i + 1) / rows
        for j in range(cols):
            west = bbox["west"] + (bbox["east"] - bbox["west"]) * j / cols
            east = bbox["west"] + (bbox["east"] - bbox["west"]) * (j + 1) / cols
            tiles.append({"south": south, "north": north, "west": west, "east": east, "tile": f"T{i+1:02d}_{j+1:02d}"})
    return tiles


def download_dem_single(dem_type, bbox, api_key):
    if not api_key:
        raise ValueError("Ingresa API Key OpenTopography.")
    params = {
        "demtype": dem_type,
        "south": bbox["south"],
        "north": bbox["north"],
        "west": bbox["west"],
        "east": bbox["east"],
        "outputFormat": "GTiff",
        "API_Key": api_key.strip(),
    }
    r = requests.get(BASE_URL, params=params, timeout=(15, 420))
    if r.status_code >= 400:
        raise RuntimeError(f"OpenTopography respondió HTTP {r.status_code}: {r.text[:900]}")
    data = r.content
    if not (data.startswith(b"II*\x00") or data.startswith(b"MM\x00*")):
        txt = data[:900].decode("utf-8", errors="ignore")
        raise RuntimeError("La respuesta no parece GeoTIFF. Respuesta inicial:\n" + txt)
    return data


def download_dem_tiled(dem_type, bbox, api_key, rows, cols, progress=None, status=None):
    import tempfile
    import rasterio
    from rasterio.merge import merge

    tmp = Path(tempfile.mkdtemp(prefix="hidrosed_dem_tiles_"))
    paths = []
    tiles = split_bbox(bbox, rows, cols)

    for k, tb in enumerate(tiles, start=1):
        if status:
            status.info(f"Descargando DEM parcial {k}/{len(tiles)} · {tb['tile']}")
        bb = {key: tb[key] for key in ["south", "north", "west", "east"]}
        data = download_dem_single(dem_type, bb, api_key)
        fp = tmp / f"{tb['tile']}.tif"
        fp.write_bytes(data)
        paths.append(fp)
        if progress:
            progress.progress(min(0.80, k / len(tiles) * 0.80))

    if status:
        status.info("Uniendo DEM parciales...")
    datasets = [rasterio.open(p) for p in paths]
    try:
        mosaic, transform = merge(datasets)
        meta = datasets[0].meta.copy()
        meta.update({
            "driver": "GTiff",
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": transform,
            "compress": "deflate",
            "predictor": 2,
        })
        with rasterio.io.MemoryFile() as mem:
            with mem.open(**meta) as dst:
                dst.write(mosaic)
            return mem.read()
    finally:
        for ds in datasets:
            ds.close()


def kml_header(name):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>{name}</name>\n'


def kml_footer():
    return "</Document></kml>"


def kmz_from_kml(kml_bytes):
    if isinstance(kml_bytes, str):
        kml_bytes = kml_bytes.encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml_bytes)
    return buf.getvalue()


def extract_document_body(kml_blob):
    if not kml_blob:
        return ""
    txt = kml_blob.decode("utf-8", errors="ignore") if isinstance(kml_blob, bytes) else str(kml_blob)
    body = re.sub(r"^.*?<Document[^>]*>", "", txt, flags=re.I | re.S)
    body = re.sub(r"</Document>\s*</kml>\s*$", "", body, flags=re.I | re.S)
    return body


def to_wgs_transformer(crs):
    try:
        if crs is not None and crs.to_epsg() != 4326:
            from pyproj import Transformer
            return Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    except Exception:
        return None
    return None


def contour_kml_from_dem(dem_path, interval_m, tile_rows, tile_cols, max_levels, max_tile_cells=650_000, max_vertices=1800):
    import numpy as np
    import rasterio
    from rasterio.windows import Window
    from rasterio.windows import transform as window_transform
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    parts = [
        kml_header("Curvas de nivel HidroSed"),
        '<Style id="contour"><LineStyle><color>ff444444</color><width>1</width></LineStyle></Style>\n',
        '<Style id="index"><LineStyle><color>ff000000</color><width>2</width></LineStyle></Style>\n',
    ]
    total = 0
    reports = []

    with rasterio.open(dem_path) as src:
        nodata = src.nodata
        sample = src.read(1, out_shape=(1, min(src.height, 1200), min(src.width, 1200)), masked=True).astype("float64").filled(np.nan)
        if nodata is not None:
            sample = np.where(np.isclose(sample, nodata), np.nan, sample)
        finite = sample[np.isfinite(sample)]
        if finite.size < 25:
            raise ValueError("DEM sin datos válidos.")
        zmin, zmax = float(np.nanmin(finite)), float(np.nanmax(finite))
        start = math.ceil(zmin / interval_m) * interval_m
        end = math.floor(zmax / interval_m) * interval_m
        levels = list(np.arange(start, end + interval_m, interval_m))
        if len(levels) > max_levels:
            step = int(math.ceil(len(levels) / max_levels))
            levels = levels[::step]
        if not levels:
            raise ValueError("No hay niveles de cota para generar curvas.")

        row_edges = np.linspace(0, src.height, int(tile_rows)+1, dtype=int)
        col_edges = np.linspace(0, src.width, int(tile_cols)+1, dtype=int)
        tr = to_wgs_transformer(src.crs)

        for i in range(int(tile_rows)):
            for j in range(int(tile_cols)):
                r0, r1 = int(row_edges[i]), int(row_edges[i+1])
                c0, c1 = int(col_edges[j]), int(col_edges[j+1])
                if r1 <= r0 or c1 <= c0:
                    continue
                win = Window(c0, r0, c1-c0, r1-r0)
                arr = src.read(1, window=win, masked=True).astype("float64").filled(np.nan)
                if nodata is not None:
                    arr = np.where(np.isclose(arr, nodata), np.nan, arr)
                finite = arr[np.isfinite(arr)]
                if finite.size < 25:
                    continue
                factor = 1
                cells = int(arr.shape[0] * arr.shape[1])
                wt = window_transform(win, src.transform)
                if cells > max_tile_cells:
                    from rasterio import Affine
                    factor = int(math.ceil(math.sqrt(cells / max_tile_cells)))
                    arr = arr[::factor, ::factor]
                    wt = wt * Affine.scale(factor, factor)

                zlo, zhi = float(np.nanmin(arr)), float(np.nanmax(arr))
                lev_tile = [float(v) for v in levels if zlo <= float(v) <= zhi]
                if len(lev_tile) > 600:
                    step = int(math.ceil(len(lev_tile)/600))
                    lev_tile = lev_tile[::step]
                if not lev_tile:
                    continue

                fig, ax = plt.subplots(figsize=(4, 3))
                try:
                    cs = ax.contour(np.ma.masked_invalid(arr), levels=lev_tile)
                    for level, segs in zip(cs.levels, cs.allsegs):
                        style = "index" if abs(level / max(interval_m*10, 10) - round(level / max(interval_m*10, 10))) < 1e-6 else "contour"
                        for seg in segs:
                            if seg is None or len(seg) < 2:
                                continue
                            step = max(1, int(len(seg)/max_vertices))
                            coords = []
                            for xcol, yrow in seg[::step]:
                                x, y = wt * (float(xcol), float(yrow))
                                if tr:
                                    x, y = tr.transform(x, y)
                                coords.append(f"{x:.8f},{y:.8f},0")
                            if len(coords) >= 2:
                                total += 1
                                parts.append(
                                    f'<Placemark><name>Cota {float(level):.2f} m</name><styleUrl>#{style}</styleUrl>'
                                    '<LineString><tessellate>1</tessellate><coordinates>'
                                    + " ".join(coords) +
                                    '</coordinates></LineString></Placemark>\n'
                                )
                finally:
                    plt.close(fig)
                reports.append({"tile": f"T{i+1:02d}_{j+1:02d}", "factor": factor, "levels": len(lev_tile)})

    if total == 0:
        raise RuntimeError("No se generaron curvas. Aumenta la distancia entre curvas o revisa el DEM.")
    parts.append(kml_footer())
    kml = "".join(parts).encode("utf-8")
    return kml, kmz_from_kml(kml), {"curvas": total, "intervalo_m": interval_m, "teselas": f"{tile_rows}x{tile_cols}", "tiles": reports}


def read_dem_light(dem_path, max_cells=2_500_000):
    import numpy as np
    import rasterio
    from rasterio import Affine
    with rasterio.open(dem_path) as src:
        arr = src.read(1, masked=True).astype("float64").filled(np.nan)
        if src.nodata is not None:
            arr = np.where(np.isclose(arr, src.nodata), np.nan, arr)
        transform = src.transform
        crs = src.crs
        factor = 1
        cells = int(arr.shape[0] * arr.shape[1])
        if cells > max_cells:
            factor = int(math.ceil(math.sqrt(cells / max_cells)))
            arr = arr[::factor, ::factor]
            transform = transform * Affine.scale(factor, factor)
    return arr, transform, crs, factor


D8 = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]


def flow_dir_acc(dem):
    import numpy as np
    valid = np.isfinite(dem)
    h, w = dem.shape
    dst = np.full(h*w, -1, dtype=np.int64)
    indeg = np.zeros(h*w, dtype=np.int32)
    for r in range(1, h-1):
        for c in range(1, w-1):
            if not valid[r, c]:
                continue
            z = dem[r, c]
            best_s = 0.0
            best = -1
            for dr, dc in D8:
                rr, cc = r+dr, c+dc
                if not valid[rr, cc]:
                    continue
                dist = 1.4142 if dr != 0 and dc != 0 else 1.0
                s = (z - dem[rr, cc]) / dist
                if s > best_s:
                    best_s = s
                    best = rr*w + cc
            if best >= 0:
                dst[r*w+c] = best
                indeg[best] += 1

    from collections import deque
    vf = valid.ravel()
    q = deque([i for i in range(h*w) if vf[i] and indeg[i] == 0])
    acc = np.where(vf, 1.0, 0.0)
    while q:
        i = q.popleft()
        d = dst[i]
        if d >= 0:
            acc[d] += acc[i]
            indeg[d] -= 1
            if indeg[d] == 0:
                q.append(int(d))
    return dst, acc.reshape((h,w)), valid


def lonlat_to_rowcol(lon, lat, transform, crs):
    if crs is not None:
        try:
            if crs.to_epsg() != 4326:
                from pyproj import Transformer
                tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
                lon, lat = tr.transform(lon, lat)
        except Exception:
            pass
    inv = ~transform
    col, row = inv * (float(lon), float(lat))
    return int(round(row)), int(round(col))


def rowcol_to_lonlat(row, col, transform, crs):
    x, y = transform * (float(col), float(row))
    if crs is not None:
        try:
            if crs.to_epsg() != 4326:
                from pyproj import Transformer
                tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
                x, y = tr.transform(x, y)
        except Exception:
            pass
    return float(x), float(y)


def axis_kml_from_coords(coords, name="Eje principal de cauce"):
    coord_txt = " ".join(f"{x:.8f},{y:.8f},0" for x, y in coords)
    kml = (
        kml_header(name) +
        '<Style id="axis"><LineStyle><color>ffff9900</color><width>4</width></LineStyle></Style>'
        f'<Placemark><name>{name}</name><styleUrl>#axis</styleUrl><LineString><tessellate>1</tessellate><coordinates>{coord_txt}</coordinates></LineString></Placemark>' +
        kml_footer()
    ).encode("utf-8")
    return kml, kmz_from_kml(kml)


def generate_main_axis_from_dem(dem_path, outlet_lon, outlet_lat, snap_cells=40, max_cells=2_500_000, max_steps=5000):
    import numpy as np
    dem, transform, crs, factor = read_dem_light(dem_path, max_cells=max_cells)
    dst, acc, valid = flow_dir_acc(dem)
    h, w = dem.shape
    r0, c0 = lonlat_to_rowcol(outlet_lon, outlet_lat, transform, crs)
    r0 = min(max(0, r0), h-1)
    c0 = min(max(0, c0), w-1)

    r1, r2 = max(0, r0-snap_cells), min(h, r0+snap_cells+1)
    c1, c2 = max(0, c0-snap_cells), min(w, c0+snap_cells+1)
    sub = acc[r1:r2, c1:c2].copy()
    sub[~valid[r1:r2, c1:c2]] = -1
    rr, cc = np.unravel_index(int(np.nanargmax(sub)), sub.shape)
    r, c = r1 + rr, c1 + cc

    coords = []
    used = set()
    for _ in range(max_steps):
        used.add((r,c))
        lon, lat = rowcol_to_lonlat(r, c, transform, crs)
        coords.append((lon, lat))
        best = None
        best_acc = acc[r, c]
        for dr, dc in D8:
            nr, nc = r+dr, c+dc
            if nr <= 0 or nr >= h-1 or nc <= 0 or nc >= w-1 or not valid[nr, nc] or (nr,nc) in used:
                continue
            if dst[nr*w+nc] == r*w+c and acc[nr, nc] >= best_acc:
                best_acc = acc[nr, nc]
                best = (nr, nc)
        if best is None:
            break
        r, c = best
    if len(coords) < 2:
        raise RuntimeError("No se pudo generar eje principal desde el DEM. Ingresa un KMZ/KML de eje de cauce.")
    kml, kmz = axis_kml_from_coords(coords)
    return kml, kmz, {"puntos_eje": len(coords), "factor_reduccion_dem": factor, "snap_cells": snap_cells}


def combine_kml(basin_kml=None, curves_kml=None, axis_kml=None):
    content = [kml_header("HidroSed cuenca + curvas + eje")]
    for blob in [basin_kml, curves_kml, axis_kml]:
        content.append(extract_document_body(blob))
    content.append(kml_footer())
    kml = "".join(content).encode("utf-8")
    return kml, kmz_from_kml(kml)


def quality_score():
    score = 0.0
    if has("control_point"): score += 1.0
    if has("dem_path"): score += 2.0
    if has("curves_kmz"): score += 2.2
    if has("axis_kmz"): score += 1.4
    if has("basin_kml"): score += 1.4
    if has("curves_meta"): score += 1.0
    if has("combined_kmz"): score += 1.0
    return min(10.0, score)


# ---------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------
tabs = st.tabs(["1 · Entrada", "2 · DEM", "3 · Curvas KMZ", "4 · Eje de cauce", "5 · Exportar", "6 · QA"])

with tabs[0]:
    st.header("1 · Entrada")
    c1, c2, c3 = st.columns(3)
    with c1:
        point_file = st.file_uploader("KMZ/KML punto de control", type=["kmz", "kml"])
        if point_file and st.button("Leer punto de control", type="primary"):
            try:
                cp = parse_first_point(read_kml_from_upload(point_file))
                st.session_state["control_point"] = cp
                st.success(f"Punto leído: {cp['name']} · lat {cp['lat']:.8f}, lon {cp['lon']:.8f}")
            except Exception as exc:
                st.error(str(exc))
    with c2:
        basin_file = st.file_uploader("KMZ/KML polígono de cuenca opcional", type=["kmz","kml"])
        if basin_file and st.button("Leer cuenca opcional"):
            try:
                kml = read_kml_from_upload(basin_file)
                if not kml_has_polygon(kml):
                    st.warning("El archivo no parece contener Polygon. Se guardará igual, pero la App Modo DIOS puede tratarlo como no definitivo.")
                st.session_state["basin_kml"] = kml.encode("utf-8")
                st.session_state["basin_kmz"] = kmz_from_kml(st.session_state["basin_kml"])
                st.success("Polígono de cuenca cargado.")
            except Exception as exc:
                st.error(str(exc))
    with c3:
        axis_file = st.file_uploader("KMZ/KML eje de cauce opcional", type=["kmz", "kml"])
        if axis_file and st.button("Leer eje de cauce"):
            try:
                kml = read_kml_from_upload(axis_file)
                coords = parse_first_linestring(kml)
                st.session_state["axis_coords"] = coords
                st.session_state["axis_kml"] = kml.encode("utf-8")
                st.session_state["axis_kmz"] = kmz_from_kml(st.session_state["axis_kml"])
                st.success(f"Eje leído: {len(coords)} puntos.")
            except Exception as exc:
                st.warning(f"No se pudo leer eje. Puedes generarlo desde DEM. Detalle: {exc}")

    if has("control_point"): st.json(st.session_state["control_point"])
    if has("basin_kml"): st.info("Cuenca opcional cargada. Se incluirá en el KMZ final.")
    if has("axis_kml"): st.info("Eje de cauce cargado. Se incluirá en el KMZ final.")

with tabs[1]:
    st.header("2 · Descargar DEM")
    if not has("control_point"):
        st.warning("Primero lee el punto de control.")
    else:
        cp = st.session_state["control_point"]
        c1, c2, c3 = st.columns(3)
        with c1:
            api_key = st.text_input("API Key OpenTopography", type="password")
            dem_type = st.selectbox("DEM", ["COP30", "NASADEM", "SRTMGL1", "SRTMGL3"], index=0)
        with c2:
            unit = st.radio("Unidad margen", ["km", "grados"], horizontal=True)
            margin = st.number_input("Margen de descarga", min_value=0.001, value=float(DEFAULT_MARGIN_KM) if unit == "km" else 0.40, step=5.0 if unit == "km" else 0.05)
        bbox = bbox_from_margin(cp["lat"], cp["lon"], margin, unit)
        area = bbox_area_km2(bbox)
        st.session_state["bbox_area_km2"] = float(area)
        with c3:
            mode = st.selectbox("Modo descarga", ["Auto", "Normal", "Por partes"], index=0 if APP_MODE == "MENOR_100K" else 2)
            tile_rows = st.selectbox("Partes verticales DEM", [1,2,3,4,5,6,8], index=[1,2,3,4,5,6,8].index(DEFAULT_TILE_ROWS) if DEFAULT_TILE_ROWS in [1,2,3,4,5,6,8] else 2)
            tile_cols = st.selectbox("Partes horizontales DEM", [1,2,3,4,5,6,8], index=[1,2,3,4,5,6,8].index(DEFAULT_TILE_COLS) if DEFAULT_TILE_COLS in [1,2,3,4,5,6,8] else 2)

        st.metric("Área bbox aproximada", f"{area:,.0f} km²".replace(",", "."))
        st.json(bbox)
        st.code(build_url(dem_type, bbox), language="text")

        if st.button("Descargar DEM", type="primary"):
            try:
                progress = st.progress(0)
                status = st.empty()
                use_tiled = (mode == "Por partes") or (mode == "Auto" and area > (3000 if APP_MODE == "MENOR_100K" else 10000))
                if use_tiled:
                    data = download_dem_tiled(dem_type, bbox, api_key, int(tile_rows), int(tile_cols), progress=progress, status=status)
                else:
                    status.info("Descargando DEM en una solicitud...")
                    data = download_dem_single(dem_type, bbox, api_key)
                progress.progress(1.0)
                path = save_bytes("dem_hidrosed_curvas.tif", data)
                st.session_state["dem_bytes"] = data
                st.session_state["dem_path"] = str(path)
                st.success(f"DEM listo: {len(data)/(1024*1024):.2f} MB")
            except Exception as exc:
                st.error(str(exc))
        if has("dem_bytes"):
            st.download_button("Descargar DEM GeoTIFF", st.session_state["dem_bytes"], file_name="dem_hidrosed_curvas.tif", mime="image/tiff")

with tabs[2]:
    st.header("3 · Generar KMZ de curvas de nivel")
    if not has("dem_path"):
        st.warning("Primero descarga el DEM.")
    else:
        vals = [1, 2, 5, 10, 20, 25, 50, 100, 200]
        c1, c2, c3 = st.columns(3)
        with c1:
            interval = st.selectbox("Distancia entre curvas de nivel [m]", vals, index=int(DEFAULT_INTERVAL_INDEX))
        with c2:
            rows = st.selectbox("Teselas verticales", [2,3,4,5,6,8,10,12,16], index=[2,3,4,5,6,8,10,12,16].index(DEFAULT_TILE_ROWS) if DEFAULT_TILE_ROWS in [2,3,4,5,6,8,10,12,16] else 3)
            cols = st.selectbox("Teselas horizontales", [2,3,4,5,6,8,10,12,16], index=[2,3,4,5,6,8,10,12,16].index(DEFAULT_TILE_COLS) if DEFAULT_TILE_COLS in [2,3,4,5,6,8,10,12,16] else 3)
        with c3:
            max_levels = st.selectbox("Máximo de cotas", [1000,3000,5000,10000,20000,30000], index=[1000,3000,5000,10000,20000,30000].index(DEFAULT_MAX_LEVELS) if DEFAULT_MAX_LEVELS in [1000,3000,5000,10000,20000,30000] else 3)
            max_tile_cells = st.selectbox("Celdas máximas por tesela", [300_000,650_000,1_000_000], index=1, format_func=lambda x: f"{x:,}".replace(",", "."))

        if interval == 1:
            st.warning("1 m es máximo detalle. Úsalo solo si el DEM y el tamaño de cuenca lo soportan.")
        elif APP_MODE == "MAYOR_100K" and interval < 50:
            st.warning("Para cuencas mayores a 100.000 km² se recomienda partir con 100 m o 200 m.")

        if st.button("Generar KMZ curvas de nivel", type="primary"):
            try:
                with st.spinner("Generando curvas por teselas..."):
                    kml, kmz, meta = contour_kml_from_dem(st.session_state["dem_path"], float(interval), int(rows), int(cols), int(max_levels), int(max_tile_cells))
                st.session_state["curves_kml"] = kml
                st.session_state["curves_kmz"] = kmz
                st.session_state["curves_meta"] = meta
                save_bytes("curvas_nivel.kml", kml)
                save_bytes("curvas_nivel.kmz", kmz)
                st.success("Curvas de nivel generadas correctamente.")
            except Exception as exc:
                st.error(str(exc))
        if has("curves_meta"): st.json(st.session_state["curves_meta"])
        if has("curves_kmz"):
            st.download_button("Descargar KMZ curvas de nivel", st.session_state["curves_kmz"], file_name="curvas_nivel.kmz", mime="application/vnd.google-earth.kmz")

with tabs[3]:
    st.header("4 · Eje de cauce")
    if has("axis_kmz"):
        st.success("Existe eje de cauce cargado o generado.")
        st.download_button("Descargar eje de cauce KMZ", st.session_state["axis_kmz"], file_name="eje_cauce.kmz", mime="application/vnd.google-earth.kmz")
    else:
        st.info("No se ingresó KMZ de eje de cauce. Puedes generar un eje principal estimado desde el DEM.")
        if not has("dem_path") or not has("control_point"):
            st.warning("Necesitas DEM y punto de control para generar eje principal.")
        else:
            c1, c2 = st.columns(2)
            with c1:
                snap = st.selectbox("Radio búsqueda cauce [celdas]", [20,40,80,120], index=1)
            with c2:
                cells = st.selectbox("Detalle DEM para eje", [500_000,1_000_000,2_500_000,5_000_000], index=2, format_func=lambda x: f"{x:,}".replace(",", "."))
            if st.button("Generar eje principal estimado", type="primary"):
                try:
                    cp = st.session_state["control_point"]
                    with st.spinner("Calculando acumulación de flujo y eje principal..."):
                        kml, kmz, meta = generate_main_axis_from_dem(st.session_state["dem_path"], cp["lon"], cp["lat"], int(snap), int(cells))
                    st.session_state["axis_kml"] = kml
                    st.session_state["axis_kmz"] = kmz
                    st.session_state["axis_meta"] = meta
                    save_bytes("eje_cauce_estimado.kmz", kmz)
                    st.success("Eje principal generado.")
                except Exception as exc:
                    st.error(str(exc))
        if has("axis_meta"): st.json(st.session_state["axis_meta"])
        if has("axis_kmz"):
            st.download_button("Descargar eje generado KMZ", st.session_state["axis_kmz"], file_name="eje_cauce_estimado.kmz", mime="application/vnd.google-earth.kmz")

with tabs[4]:
    st.header("5 · Exportar paquete compatible con Modo DIOS")
    if has("curves_kml") or has("axis_kml") or has("basin_kml"):
        kml, kmz = combine_kml(st.session_state.get("basin_kml"), st.session_state.get("curves_kml"), st.session_state.get("axis_kml"))
        st.session_state["combined_kmz"] = kmz
        meta = {
            "app": APP_TITLE,
            "modo": APP_MODE,
            "calidad_estimada_10": quality_score(),
            "incluye_cuenca": has("basin_kml"),
            "incluye_curvas": has("curves_kml"),
            "incluye_eje": has("axis_kml"),
            "area_bbox_km2": st.session_state.get("bbox_area_km2"),
            "curvas_meta": st.session_state.get("curves_meta"),
            "axis_meta": st.session_state.get("axis_meta"),
        }
        st.json(meta)
        st.download_button("Descargar KMZ cuenca + curvas + eje", kmz, file_name="hidrosed_cuenca_curvas_eje.kmz", mime="application/vnd.google-earth.kmz")
        st.download_button("Descargar metadata JSON", json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"), file_name="metadata_hidrosed_curvas.json", mime="application/json")
    else:
        st.info("Genera curvas, carga cuenca o eje para exportar paquete combinado.")

with tabs[5]:
    st.header("6 · QA de cumplimiento")
    score = quality_score()
    st.metric("Puntaje de completitud del paquete", f"{score:.1f} / 10")
    st.write("Para superar 8,7 el paquete debe incluir DEM procesado, curvas KMZ, eje de cauce y preferentemente polígono de cuenca.")
    checklist = {
        "Punto de control": has("control_point"),
        "DEM descargado": has("dem_path"),
        "Curvas KMZ": has("curves_kmz"),
        "Eje de cauce cargado o generado": has("axis_kmz"),
        "Polígono de cuenca opcional incluido": has("basin_kml"),
        "KMZ combinado": has("combined_kmz"),
    }
    st.json(checklist)
    if score >= 8.7:
        st.success("Cumple nivel objetivo mayor a 8,7 para pasar a la aplicación KMZ Modo DIOS.")
    else:
        st.warning("Aún no supera 8,7. Agrega eje de cauce y polígono de cuenca para maximizar compatibilidad.")
