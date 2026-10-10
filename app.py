import io
import os
import time
import zipfile
import logging
import xml.etree.ElementTree as ET
from math import cos, radians

import ezdxf
import osmnx as ox
from flask import Flask, request, jsonify, send_file, render_template
from pyproj import Transformer
from shapely.geometry import LineString, box

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bản demo giữ dữ liệu trong RAM; dùng 1 worker trên Render.
stored_points = []
stored_lines = []
MAX_OSM_AREA_KM2 = 5.0

try:
    ox.settings.timeout = 25
    ox.settings.overpass_rate_limit = True
    ox.settings.use_cache = True
except Exception:
    logger.exception("Không thiết lập được cấu hình OSMnx")

TO_WEBMERCATOR = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


def read_kml_bytes(upload_bytes, filename):
    name = (filename or "").lower()
    if name.endswith(".kmz"):
        with zipfile.ZipFile(io.BytesIO(upload_bytes)) as zf:
            candidates = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not candidates:
                raise ValueError("File KMZ không chứa file KML.")
            kml_name = next((n for n in candidates if n.lower().endswith("doc.kml")), candidates[0])
            kml_bytes = zf.read(kml_name)
    elif name.endswith(".kml"):
        kml_bytes = upload_bytes
    else:
        raise ValueError("Chỉ hỗ trợ file .kml hoặc .kmz.")

    root = ET.fromstring(kml_bytes)
    points, lines = [], []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag not in ("Point", "LineString"):
            continue
        coord_el = next((c for c in elem.iter() if c.tag.split("}")[-1] == "coordinates"), None)
        if coord_el is None or not coord_el.text:
            continue
        tokens = coord_el.text.strip().split()
        if tag == "Point":
            if tokens:
                parts = tokens[0].split(",")
                if len(parts) >= 2:
                    points.append((float(parts[0]), float(parts[1])))
        else:
            coords = []
            for token in tokens:
                parts = token.split(",")
                if len(parts) >= 2:
                    coords.append((float(parts[0]), float(parts[1])))
            if len(coords) >= 2:
                lines.append(coords)
    return points, lines


@app.errorhandler(413)
def file_too_large(_error):
    return jsonify({"error": "File quá lớn. Vui lòng chọn file dưới 25 MB."}), 413


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/upload_kml", methods=["POST"])
def upload_kml():
    global stored_points, stored_lines
    if "file" not in request.files:
        return jsonify({"error": "Không tìm thấy file upload (trường 'file')."}), 400
    uploaded_file = request.files["file"]
    if not uploaded_file.filename:
        return jsonify({"error": "Bạn chưa chọn file."}), 400
    try:
        points, lines = read_kml_bytes(uploaded_file.read(), uploaded_file.filename)
        stored_points, stored_lines = points, lines
        logger.info("Upload KML/KMZ: %d điểm, %d tuyến", len(points), len(lines))
        return jsonify({"message": "Đọc file thành công.", "points": len(points), "lines": len(lines)})
    except Exception as exc:
        logger.exception("Không đọc được KML/KMZ")
        return jsonify({"error": f"Không đọc được file: {exc}"}), 400


def add_line_to_dxf(msp, coords_lonlat, layer):
    coords_m = [TO_WEBMERCATOR.transform(lon, lat) for lon, lat in coords_lonlat]
    if len(coords_m) >= 2:
        msp.add_lwpolyline(coords_m, dxfattribs={"layer": layer})


def add_geometry_to_dxf(msp, geom, layer):
    if geom is None or geom.is_empty:
        return 0
    if geom.geom_type == "LineString":
        parts = (geom,)
    elif geom.geom_type == "MultiLineString":
        parts = geom.geoms
    elif geom.geom_type == "GeometryCollection":
        parts = (g for g in geom.geoms if g.geom_type == "LineString")
    else:
        return 0
    count = 0
    for part in parts:
        coords_m = [TO_WEBMERCATOR.transform(x, y) for x, y in part.coords]
        if len(coords_m) >= 2:
            msp.add_lwpolyline(coords_m, dxfattribs={"layer": layer})
            count += 1
    return count


def get_area_km2(north, south, east, west):
    mid_lat = (north + south) / 2
    width_m = (east - west) * 111320 * max(abs(cos(radians(mid_lat))), 0.01)
    height_m = (north - south) * 111320
    return width_m * height_m / 1_000_000


def add_kml_to_dxf(msp, north, south, east, west):
    bounds = box(west, south, east, north)
    line_count, point_count = 0, 0
    for coords in stored_lines:
        try:
            clipped = LineString(coords).intersection(bounds)
            if clipped.is_empty:
                continue
            if clipped.geom_type == "LineString":
                parts = (clipped,)
            elif clipped.geom_type == "MultiLineString":
                parts = clipped.geoms
            elif clipped.geom_type == "GeometryCollection":
                parts = (g for g in clipped.geoms if g.geom_type == "LineString")
            else:
                parts = ()
            for part in parts:
                add_line_to_dxf(msp, list(part.coords), "KML_ROUTE")
                line_count += 1
        except Exception:
            logger.exception("Lỗi khi ghi tuyến KML")
    for lon, lat in stored_points:
        if west <= lon <= east and south <= lat <= north:
            x, y = TO_WEBMERCATOR.transform(lon, lat)
            msp.add_point((x, y), dxfattribs={"layer": "KML_POINT"})
            txt = msp.add_text("KML_POINT", dxfattribs={"layer": "POINT_TEXT", "height": 2.5})
            txt.set_placement((x, y))
            point_count += 1
    return line_count, point_count


def add_osm_roads(msp, north, south, east, west):
    started = time.monotonic()
    graph = None
    edges = None
    try:
        logger.info("Bắt đầu truy vấn OSM")
        graph = ox.graph_from_bbox(
            north, south, east, west,
            network_type="drive",
            simplify=True,
            retain_all=False,
            truncate_by_edge=False
        )
        edges = ox.graph_to_gdfs(graph, nodes=False, edges=True)
        del graph
        graph = None

        count = 0
        for geom in edges.geometry:
            count += add_geometry_to_dxf(msp, geom, "BASE_ROAD")
        logger.info("Đã ghi %d đoạn OSM trong %.1f giây", count, time.monotonic() - started)
        return count, ""
    except Exception as exc:
        logger.exception("OSM thất bại; vẫn tiếp tục xuất dữ liệu KML/KMZ")
        return 0, f"Không lấy được đường OSM ({type(exc).__name__}): {str(exc)[:300]}"
    finally:
        if edges is not None:
            del edges
        if graph is not None:
            del graph


@app.route("/export_dxf", methods=["POST"])
def export_dxf():
    payload = request.get_json(silent=True) or {}
    try:
        north = float(payload["north"])
        south = float(payload["south"])
        east = float(payload["east"])
        west = float(payload["west"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Thiếu hoặc sai tọa độ north/south/east/west."}), 400

    if not (-85 <= south < north <= 85):
        return jsonify({"error": "Vĩ độ không hợp lệ."}), 400
    if not (-180 <= west < east <= 180):
        return jsonify({"error": "Kinh độ không hợp lệ."}), 400

    area_km2 = get_area_km2(north, south, east, west)
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for layer_name, color in [
        ("BASE_ROAD", 7), ("KML_ROUTE", 3), ("KML_POINT", 1), ("POINT_TEXT", 2)
    ]:
        if layer_name not in doc.layers:
            doc.layers.new(layer_name, dxfattribs={"color": color})

    kml_line_count, kml_point_count = add_kml_to_dxf(msp, north, south, east, west)
    osm_count, osm_warning = 0, ""

    if area_km2 > MAX_OSM_AREA_KM2:
        osm_warning = f"Vùng chọn {area_km2:.2f} km² vượt giới hạn OSM {MAX_OSM_AREA_KM2:.1f} km². DXF vẫn được xuất với KML/KMZ."
        logger.warning(osm_warning)
    else:
        osm_count, osm_warning = add_osm_roads(msp, north, south, east, west)

    output = io.BytesIO()
    doc.write(output)
    output.seek(0)
    response = send_file(output, mimetype="application/dxf", as_attachment=True, download_name="export_kml_osm.dxf")
    response.headers["X-KML-Lines"] = str(kml_line_count)
    response.headers["X-KML-Points"] = str(kml_point_count)
    response.headers["X-OSM-Entities"] = str(osm_count)
    response.headers["X-OSM-Warning"] = osm_warning[:500]
    response.headers["X-Selected-Area-Km2"] = f"{area_km2:.2f}"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
