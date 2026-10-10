import os
import zipfile
import uuid
import xml.etree.ElementTree as ET
from datetime import timedelta

import ezdxf
import osmnx as ox
import pyproj
from shapely.geometry import LineString, box
from shapely.ops import unary_union
from flask import (
    Flask, render_template, request, jsonify, send_file,
    redirect, url_for, session
)
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# =========================
# APP INIT
# =========================
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_ME_IN_RENDER")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///users.db"
).replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

UPLOAD_FOLDER = "uploads"
EXPORT_FOLDER = "exports"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EXPORT_FOLDER, exist_ok=True)

# Lưu trong bộ nhớ tiến trình: phù hợp để thử nghiệm một người dùng.
# Nếu nhiều người dùng đồng thời, nên chuyển dữ liệu upload sang lưu theo user/session.
stored_points = []
stored_lines = []

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default="user")


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


db_initialized = False


@app.before_request
def init_db_once():
    global db_initialized
    if not db_initialized:
        db.create_all()
        # Tài khoản mặc định chỉ để chạy thử. Hãy đặt ADMIN_PASSWORD trên Render
        # và đổi mật khẩu ngay sau khi đăng nhập.
        if not User.query.filter_by(username="admin").first():
            admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
            db.session.add(User(
                username="admin",
                password=generate_password_hash(admin_password),
                role="admin"
            ))
            db.session.commit()
        db_initialized = True


# =========================
# LOGIN / LOGOUT
# =========================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            session.permanent = True
            return redirect(url_for("index"))

        return render_template("login.html", error="Sai tài khoản hoặc mật khẩu")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html", user=current_user)


@app.route("/admin")
@login_required
def admin():
    if current_user.role != "admin":
        return "Không có quyền truy cập", 403
    users = User.query.order_by(User.id).all()
    return render_template("admin.html", users=users)


@app.route("/admin/create_user", methods=["POST"])
@login_required
def create_user():
    if current_user.role != "admin":
        return "Forbidden", 403

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")

    if not username or not password:
        return "Thiếu tên đăng nhập hoặc mật khẩu", 400
    if User.query.filter_by(username=username).first():
        return "Tên đăng nhập đã tồn tại", 400
    if role not in ("admin", "user"):
        role = "user"

    db.session.add(User(
        username=username,
        password=generate_password_hash(password),
        role=role
    ))
    db.session.commit()
    return redirect(url_for("admin"))


# =========================
# KML/KMZ PARSER
# =========================
def parse_coordinates(text):
    """Đọc danh sách tọa độ KML theo thứ tự lon,lat[,alt]."""
    coords = []
    if not text:
        return coords
    for token in text.strip().split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
            coords.append((lon, lat))
        except (ValueError, TypeError):
            continue
    return coords


@app.route("/upload_kml", methods=["POST"])
@login_required
def upload_kml():
    global stored_points, stored_lines
    stored_points = []
    stored_lines = []

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Bạn chưa chọn file KML/KMZ."}), 400

    safe_name = secure_filename(uploaded.filename)
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in (".kml", ".kmz"):
        return jsonify({"error": "Chỉ hỗ trợ file .kml hoặc .kmz."}), 400

    saved_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4().hex}_{safe_name}")
    uploaded.save(saved_path)

    try:
        if ext == ".kmz":
            with zipfile.ZipFile(saved_path) as archive:
                kml_names = [
                    name for name in archive.namelist()
                    if name.lower().endswith(".kml") and not name.endswith("/")
                ]
                if not kml_names:
                    return jsonify({"error": "KMZ không chứa file KML."}), 400
                # Ưu tiên doc.kml nếu có.
                kml_name = next(
                    (n for n in kml_names if os.path.basename(n).lower() == "doc.kml"),
                    kml_names[0]
                )
                kml_data = archive.read(kml_name)
        else:
            with open(saved_path, "rb") as f:
                kml_data = f.read()

        root = ET.fromstring(kml_data)

        for placemark in root.findall(".//{*}Placemark"):
            name_el = placemark.find("./{*}name")
            name = (name_el.text or "").strip() if name_el is not None else "KML"

            # Đọc Point
            for point_el in placemark.findall(".//{*}Point"):
                coord_el = point_el.find("./{*}coordinates")
                coords = parse_coordinates(coord_el.text if coord_el is not None else "")
                if coords:
                    lon, lat = coords[0]
                    stored_points.append({"name": name, "lon": lon, "lat": lat})

            # Đọc LineString, thay vì chỉ nối các Point rời rạc trên giao diện.
            for line_el in placemark.findall(".//{*}LineString"):
                coord_el = line_el.find("./{*}coordinates")
                coords = parse_coordinates(coord_el.text if coord_el is not None else "")
                if len(coords) >= 2:
                    stored_lines.append({"name": name, "coords": coords})

        if not stored_points and not stored_lines:
            return jsonify({
                "error": "Đã đọc file nhưng không tìm thấy Placemark Point hoặc LineString có tọa độ."
            }), 400

        return jsonify({
            "points": stored_points,
            "lines": stored_lines,
            "point_count": len(stored_points),
            "line_count": len(stored_lines)
        })

    except zipfile.BadZipFile:
        return jsonify({"error": "File KMZ không hợp lệ hoặc bị hỏng."}), 400
    except ET.ParseError as exc:
        return jsonify({"error": f"Không đọc được XML/KML: {exc}"}), 400
    except Exception as exc:
        app.logger.exception("Lỗi upload KML/KMZ")
        return jsonify({"error": str(exc)}), 500


# =========================
# EXPORT DXF: KML + OSM
# =========================
@app.route("/export_dxf", methods=["POST"])
@login_required
def export_dxf():
    try:
        data = request.get_json(silent=True) or {}
        bbox_data = data.get("bbox")
        if not bbox_data:
            return jsonify({"error": "Chưa chọn vùng export."}), 400

        north = float(bbox_data["north"])
        south = float(bbox_data["south"])
        east = float(bbox_data["east"])
        west = float(bbox_data["west"])
        if north <= south or east <= west:
            return jsonify({"error": "Vùng chọn không hợp lệ."}), 400

        width_slider = float(data.get("width", 0) or 0)
        selected_area = box(west, south, east, north)

        # Tạo bản vẽ trước để có thể xuất tuyến KML kể cả khi OSM tạm lỗi.
        doc = ezdxf.new("R2013", setup=True)
        msp = doc.modelspace()
        for layer_name, color in (
            ("BASE_ROAD", 7),
            ("KML_ROUTE", 1),
            ("KML_POINT", 3),
            ("POINT_TEXT", 2),
        ):
            if layer_name not in doc.layers:
                doc.layers.new(layer_name, dxfattribs={"color": color})

        transformer = pyproj.Transformer.from_crs(
            "EPSG:4326", "EPSG:3857", always_xy=True
        )

        # Xuất các LineString thật trong KML, cắt theo vùng chọn.
        kml_line_count = 0
        for item in stored_lines:
            try:
                line_wgs84 = LineString(item["coords"])
                clipped = line_wgs84.intersection(selected_area)
                if clipped.is_empty:
                    continue

                def draw_kml_line(geom):
                    nonlocal kml_line_count
                    if geom.geom_type == "LineString":
                        coords_m = [
                            transformer.transform(lon, lat)
                            for lon, lat in geom.coords
                        ]
                        if len(coords_m) >= 2:
                            msp.add_lwpolyline(
                                coords_m,
                                dxfattribs={"layer": "KML_ROUTE"}
                            )
                            kml_line_count += 1
                    elif hasattr(geom, "geoms"):
                        for part in geom.geoms:
                            draw_kml_line(part)

                draw_kml_line(clipped)
            except Exception:
                app.logger.exception("Không thể xuất một tuyến KML")

        # Xuất điểm KML trong vùng chọn.
        point_count = 0
        for point in stored_points:
            lon, lat = point["lon"], point["lat"]
            if not (south <= lat <= north and west <= lon <= east):
                continue
            x, y = transformer.transform(lon, lat)
            msp.add_point((x, y), dxfattribs={"layer": "KML_POINT"})
            label = f'{point["name"]} ({lat:.6f}, {lon:.6f})'
            msp.add_text(
                label,
                dxfattribs={"height": 2.0, "layer": "POINT_TEXT"}
            ).set_placement(
                (x, y + 2),
                align=ezdxf.enums.TextEntityAlignment.BOTTOM_CENTER
            )
            point_count += 1

        # Lấy đường OSM. Nếu OSM lỗi, vẫn trả về tuyến KML nếu đã có.
        osm_count = 0
        osm_warning = None
        try:
            # Cú pháp này tương thích với osmnx 1.3.0.
            graph = ox.graph_from_bbox(
                north, south, east, west,
                network_type="drive",
                simplify=True
            )
            edges = ox.graph_to_gdfs(graph, nodes=False, edges=True)

            road_width_map = {
                "motorway": 7.0, "trunk": 7.0, "primary": 7.0,
                "secondary": 6.5, "tertiary": 6.0, "residential": 5.0,
                "unclassified": 4.0, "service": 3.0,
                "living_street": 2.5, "track": 2.5
            }
            road_polygons = []

            def line_parts(geom):
                if geom is None or geom.is_empty:
                    return
                if geom.geom_type == "LineString":
                    yield geom
                elif hasattr(geom, "geoms"):
                    for part in geom.geoms:
                        yield from line_parts(part)

            for _, row in edges.iterrows():
                geom = row.geometry
                if geom is None or geom.is_empty:
                    continue

                highway = row.get("highway", "")
                if isinstance(highway, list):
                    highway = highway[0] if highway else ""
                width = width_slider if width_slider > 0 else road_width_map.get(highway, 3.0)

                for part in line_parts(geom):
                    coords_m = [
                        transformer.transform(lon, lat)
                        for lon, lat in part.coords
                    ]
                    if len(coords_m) < 2:
                        continue
                    line_m = LineString(coords_m)
                    road_polygons.append(line_m.buffer(width / 2.0, join_style=1))

            if road_polygons:
                combined_area = unary_union(road_polygons)
                road_boundary = combined_area.boundary

                def draw_boundary(geom):
                    nonlocal osm_count
                    if geom.geom_type == "LineString":
                        if geom.length > 2.0 and len(geom.coords) >= 2:
                            msp.add_lwpolyline(
                                list(geom.coords),
                                dxfattribs={"layer": "BASE_ROAD"}
                            )
                            osm_count += 1
                    elif hasattr(geom, "geoms"):
                        for part in geom.geoms:
                            draw_boundary(part)

                draw_boundary(road_boundary)
        except Exception as exc:
            app.logger.exception("Truy vấn hoặc xuất OSM thất bại")
            osm_warning = str(exc)

        total_entities = len(msp)
        if total_entities == 0:
            message = (
                "Không có đối tượng nào để xuất trong vùng chọn. "
                "Hãy kiểm tra vùng chọn và dữ liệu KML/OSM."
            )
            if osm_warning:
                message += f" Lỗi OSM: {osm_warning}"
            return jsonify({"error": message}), 400

        filename = f"survey_{uuid.uuid4().hex[:8]}.dxf"
        filepath = os.path.join(EXPORT_FOLDER, filename)
        doc.saveas(filepath)

        response = send_file(
            filepath,
            as_attachment=True,
            download_name=filename,
            mimetype="application/dxf"
        )
        response.headers["X-KML-Lines"] = str(kml_line_count)
        response.headers["X-KML-Points"] = str(point_count)
        response.headers["X-OSM-Entities"] = str(osm_count)
        if osm_warning:
            response.headers["X-OSM-Warning"] = "OSM query failed; exported KML data only"
        return response

    except Exception as exc:
        app.logger.exception("Lỗi export DXF")
        return jsonify({"error": f"Lỗi export DXF: {exc}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
