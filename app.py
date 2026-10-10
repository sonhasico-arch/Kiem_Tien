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
from shapely.geometry import Point, LineString, box
from shapely.ops import transform as shapely_transform

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # giới hạn upload 25 MB

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Dữ liệu được giữ trong RAM theo phiên bản demo này.
# Với nhiều người dùng hoặc nhiều worker, nên chuyển sang lưu file theo mã phiên.
stored_points = []
stored_lines = []

# OSMnx 1.3.0: cấu hình timeout để tránh chờ quá lâu khi Overpass chậm.
try:
    ox.settings.timeout = 45
    ox.settings.overpass_rate_limit = True
    ox.settings.use_cache = True
except Exception:
    pass

TO_WEBMERCATOR = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}


def read_kml_bytes(upload_bytes, filename):
    """Đọc KML hoặc KMZ, trả về danh sách điểm và đường theo (lon, lat)."""
    name = (filename or "").lower()
    if name.endswith(".kmz"):
        with zipfile.ZipFile(io.BytesIO(upload_bytes)) as zf:
            candidates = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not candidates:
                raise ValueError("File KMZ không chứa file KML.")
            # Ưu tiên doc.kml nếu có
            kml_name = next((n for n in candidates if n.lower().endswith("doc.kml")), candidates[0])
            kml_bytes = zf.read(kml_name)
    elif name.endswith(".kml"):
        kml_bytes = upload_bytes
    else:
        raise ValueError("Chỉ hỗ trợ file .kml hoặc .kmz.")

    root = ET.fromstring(kml_bytes)
    points, lines = [], []

    # Tìm các thẻ theo local-name để hỗ trợ KML có/không có namespace.
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "Point":
            coord_el = next((c for c in elem.iter() if c.tag.split("}")[-1] == "coordinates"), None)
            if coord_el is not None and coord_el.text:
                token = coord_el.text.strip().split()[0]
                parts = token.split(",")
                if len(parts) >= 2:
                    points.append((float(parts[0]), float(parts[1])))
        elif tag == "LineString":
            coord_el = next((c for c in elem.iter() if c.tag.split("}")[-1] == "coordinates"), None)
            if coord_el is not None and coord_el.text:
                coords = []
                for token in coord_el.text.strip().split():
                    parts = token.split(",")
                    if len(parts) >= 2:
                        coords.append((float(parts[0]), float(parts[1])))
                if len(coords) >= 2:
                    lines.append(coords)

    return points, lines


@app.errorhandler(413)
def file_too_large(_error):
    return jsonify({"error": "File quá lớn. Vui lòng chọn file dưới 25 MB."}), 413


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/upload_kml", methods=["POST"])
def upload_kml():
    global stored_points, stored_lines

    if "file" not in request.files:
        return jsonify({"error": "Không tìm thấy file upload (trường 'file')."}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Bạn chưa chọn file."}), 400

    try:
        points, lines = read_kml_bytes(file.read(), file.filename)
        stored_points = points
        stored_lines = lines
        return jsonify({
            "message": "Đọc file thành công.",
            "points": len(stored_points),
            "lines": len(stored_lines)
        })
    except Exception as exc:
        logger.exception("Không đọc được KML/KMZ")
        return jsonify({"error": f"Không đọc được file: {exc}"}), 400


def add_line_to_dxf(msp, coords_lonlat, layer):
    coords_m = [TO_WEBMERCATOR.transform(lon, lat) for lon, lat in coords_lonlat]
    if len(coords_m) >= 2:
        msp.add_lwpolyline(coords_m, dxfattribs={"layer": layer})


def add_geometry_to_dxf(msp, geom, layer):
    """Ghi LineString/MultiLineString vào DXF, không buffer/unary_union để giảm RAM."""
    if geom is None or geom.is_empty:
        return 0
    count = 0
    if geom.geom_type == "LineString":
        parts = [geom]
    elif geom.geom_type == "MultiLineString":
        parts = list(geom.geoms)
    elif geom.geom_type == "GeometryCollection":
        parts = [g for g in geom.geoms if g.geom_type == "LineString"]
    else:
        return 0

    for part in parts:
        coords = list(part.coords)
        if len(coords) < 2:
            continue
        # OSMnx geometry thường có tọa độ lon/lat.
        coords_m = [TO_WEBMERCATOR.transform(x, y) for x, y in coords]
        msp.add_lwpolyline(coords_m, dxfattribs={"layer": layer})
        count += 1
    return count


@app.route("/export_dxf", methods=["POST"])
def export_dxf():
    global stored_points, stored_lines

    payload = request.get_json(silent=True) or {}
    try:
        north = float(payload["north"])
        south = float(payload["south"])
        east = float(payload["east"])
        west = float(payload["west"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Thiếu hoặc sai tọa độ khu vực north/south/east/west."}), 400

    if north <= south or east <= west:
        return jsonify({"error": "Khu vực chọn không hợp lệ."}), 400
    if north > 85 or south < -85 or east > 180 or west < -180:
        return jsonify({"error": "Tọa độ ngoài phạm vi hỗ trợ."}), 400

    # Tính diện tích gần đúng để chặn truy vấn quá lớn.
    mid_lat = (north + south) / 2
    width_m = (east - west) * 111320 * max(abs(cos(radians(mid_lat))), 0.01)
    height_m = (north - south) * 111320
    area_km2 = (width_m * height_m) / 1_000_000

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for layer_name, color in [
        ("BASE_ROAD", 7), ("KML_ROUTE", 3), ("KML_POINT", 1), ("POINT_TEXT", 2)
    ]:
        if layer_name not in doc.layers:
            doc.layers.new(layer_name, dxfattribs={"color": color})

    # Ghi các đối tượng KML/KMZ nằm trong khung chọn.
    bounds = box(west, south, east, north)
    kml_line_count = 0
    kml_point_count = 0

    for coords in stored_lines:
        try:
            line = LineString(coords)
            clipped = line.intersection(bounds)
            if clipped.is_empty:
                continue
            if clipped.geom_type == "LineString":
                parts = [clipped]
            elif clipped.geom_type == "MultiLineString":
                parts = list(clipped.geoms)
            elif clipped.geom_type == "GeometryCollection":
                parts = [g for g in clipped.geoms if g.geom_type == "LineString"]
            else:
                parts = []
            for part in parts:
                add_line_to_dxf(msp, list(part.coords), "KML_ROUTE")
                kml_line_count += 1
        except Exception:
            logger.exception("Lỗi khi ghi một tuyến KML")

    for lon, lat in stored_points:
        if west <= lon <= east and south <= lat <= north:
            x, y = TO_WEBMERCATOR.transform(lon, lat)
            msp.add_point((x, y), dxfattribs={"layer": "KML_POINT"})
            msp.add_text("KML_POINT", dxfattribs={"layer": "POINT_TEXT", "height": 2.5}).set_placement((x, y))
            kml_point_count += 1

    osm_count = 0
    osm_warning = ""
    osm_start = time.monotonic()

    if area_km2 > 5:
        osm_warning = f"Bỏ qua OSM vì vùng chọn khoảng {area_km2:.2f} km², vượt giới hạn 5 km²."
    else:
        try:
            logger.info("Bắt đầu truy vấn OSM cho vùng %.2f km²", area_km2)
            # graph_from_bbox theo API OSMnx 1.3.x
            graph = ox.graph_from_bbox(
                north, south, east, west,
                network_type="drive",
                simplify=True,
                retain_all=False,
                truncate_by_edge=True
            )
            edges = ox.graph_to_gdfs(graph, nodes=False, edges=True)
            del graph

            # Chỉ xuất đường tâm OSM. Không buffer và unary_union vì dễ tăng RAM mạnh.
            for geom in edges.geometry:
                osm_count += add_geometry_to_dxf(msp, geom, "BASE_ROAD")
            del edges
            logger.info("Đã ghi %s đoạn OSM trong %.1f giây", osm_count, time.monotonic() - osm_start)
        except Exception as exc:
            osm_warning = f"Không lấy được đường OSM: {type(exc).__name__}: {exc}"
            logger.exception("OSM query/export failed; vẫn tiếp tục xuất dữ liệu KML/KMZ")

    output = io.BytesIO()
    doc.write(output)
    output.seek(0)

    response = send_file(
        output,
        mimetype="application/dxf",
        as_attachment=True,
        download_name="export_kml_osm.dxf"
    )
    response.headers["X-KML-Lines"] = str(kml_line_count)
    response.headers["X-KML-Points"] = str(kml_point_count)
    response.headers["X-OSM-Entities"] = str(osm_count)
    response.headers["X-OSM-Warning"] = osm_warning[:500]
    response.headers["X-Selected-Area-Km2"] = f"{area_km2:.2f}"
    return response


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Chỉ dùng khi chạy local; trên Render dùng Gunicorn.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
