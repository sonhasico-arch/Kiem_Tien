import os
import zipfile
import uuid
import ezdxf
import pyproj
import osmnx as ox
from shapely.geometry import LineString
from shapely.ops import unary_union
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for
import xml.etree.ElementTree as ET

from flask_login import (
    LoginManager, UserMixin,
    login_user, login_required,
    logout_user, current_user
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# =========================
# APP INIT
# =========================
app = Flask(__name__)
from datetime import timedelta

app.secret_key = "NOI_BO_CONG_TY_SECRET_KEY"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)

UPLOAD_FOLDER = "uploads"
EXPORT_FOLDER = "exports"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EXPORT_FOLDER, exist_ok=True)

stored_points = []

# =========================
# DATABASE
# =========================
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///users.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"

# =========================
# USER MODEL
# =========================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True)
    password = db.Column(db.String(200))
    role = db.Column(db.String(20))  # admin | user

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# =========================
# INIT DB + ADMIN
# =========================
db_initialized = False

@app.before_request
def init_db_once():
    global db_initialized

    if not db_initialized:
        db.create_all()

        if not User.query.filter_by(username="admin").first():
            admin = User(
                username="admin",
                password=generate_password_hash("admin123"),
                role="admin"
            )
            db.session.add(admin)
            db.session.commit()

        db_initialized = True

# =========================
# LOGIN
# =========================
from flask import session

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            login_user(user)

            session.permanent = True   # 👈 QUAN TRỌNG

            return redirect(url_for("index"))

        return render_template("login.html", error="Sai tài khoản hoặc mật khẩu")

    return render_template("login.html")



# =========================
# LOGOUT
# =========================
@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# =========================
# HOME (CHỈ 1 ROUTE)
# =========================
@app.route("/")
@login_required
def index():
    return render_template("index.html", user=current_user)

# =========================
# ADMIN
# =========================
@app.route("/admin")
@login_required
def admin():
    if current_user.role != "admin":
        return "Không có quyền truy cập", 403
    users = User.query.all()
    return render_template("admin.html", users=users)

@app.route("/admin/create_user", methods=["POST"])
@login_required
def create_user():
    if current_user.role != "admin":
        return "Forbidden", 403

    user = User(
        username=request.form["username"],
        password=generate_password_hash(request.form["password"]),
        role=request.form["role"]
    )
    db.session.add(user)
    db.session.commit()
    return redirect(url_for("admin"))

# ===============================
# UPLOAD KML
# ===============================
@app.route("/upload_kml", methods=["POST"])
@login_required
def upload_kml():
    global stored_points
    stored_points = []

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file"}), 400

    path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(path)

    try:
        if file.filename.lower().endswith(".kmz"):
            with zipfile.ZipFile(path) as z:
                kml_name = next(n for n in z.namelist() if n.endswith(".kml"))
                kml_data = z.read(kml_name)
        else:
            kml_data = open(path, "rb").read()

        root = ET.fromstring(kml_data)
        ns = {'kml': 'http://www.opengis.net/kml/2.2'}

        for pm in root.findall(".//kml:Placemark", ns):
            p = pm.find(".//kml:Point", ns)
            if p is None:
                continue

            lon, lat, *_ = p.find("kml:coordinates", ns).text.strip().split(",")
            name = pm.findtext("kml:name", default="P", namespaces=ns)

            stored_points.append({
                "name": name,
                "lon": float(lon),
                "lat": float(lat)
            })

        return jsonify({"points": stored_points})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===============================
# EXPORT DXF
# ===============================
@app.route("/export_dxf", methods=["POST"])
def export_dxf():
    global stored_points

    try:
        data = request.get_json()

        if not data or "bbox" not in data:
            return "Chưa chọn vùng", 400

        bbox = data["bbox"]
        width_slider = float(data.get("width", 0))

        north = float(bbox["north"])
        south = float(bbox["south"])
        east = float(bbox["east"])
        west = float(bbox["west"])

        # ===============================
        # Lấy OSM
        # ===============================
        G = ox.graph_from_bbox(
            north,
            south,
            east,
            west,
            network_type="drive"
        )

        edges = ox.graph_to_gdfs(G, nodes=False)

        if edges.empty:
            return "Không có dữ liệu đường trong vùng chọn", 400

        # ===============================
        # Tạo DXF
        # ===============================
        doc = ezdxf.new(setup=True)
        msp = doc.modelspace()

        doc.layers.new("BASE_ROAD", dxfattribs={"color": 255})
        doc.layers.new("POINT_TEXT", dxfattribs={"color": 1})

        transformer = pyproj.Transformer.from_crs(
            "EPSG:4326", "EPSG:3857", always_xy=True
        )

        road_polygons = []

        # ===============================
        # Vẽ đường theo phân cấp độ rộng
        # ===============================
        for _, row in edges.iterrows():

            if row.geometry.geom_type != "LineString":
                continue

            coords_m = [
                transformer.transform(lon, lat)
                for lon, lat in row.geometry.coords
            ]

            line_m = LineString(coords_m)

            # ===== TÍNH WIDTH =====
            if width_slider > 0:
                width = width_slider
            else:
                highway = row.get("highway", "")

                if isinstance(highway, list):
                    highway = highway[0]

                road_width_map = {
                    "motorway": 7,
                    "trunk": 7,
                    "primary": 7,
                    "secondary": 6.5,
                    "tertiary": 6,
                    "residential": 5,
                    "unclassified": 4,
                    "service": 3,
                    "living_street": 2.5,
                    "track": 2.5
                }

                width = road_width_map.get(highway, 3)

            road_polygons.append(line_m.buffer(width, join_style=1))

        # ===============================
        # Vẽ boundary
        # ===============================
        if road_polygons:
            combined_area = unary_union(road_polygons)
            road_boundary = combined_area.boundary

            def draw_geometry(geom):
                if isinstance(geom, LineString):
                    if geom.length > 2.0:
                        msp.add_lwpolyline(
                            list(geom.coords),
                            dxfattribs={"layer": "BASE_ROAD"}
                        )
                elif hasattr(geom, "geoms"):
                    for part in geom.geoms:
                        draw_geometry(part)

            draw_geometry(road_boundary)

        # ===============================
        # Vẽ điểm khảo sát
        # ===============================
        for p in stored_points:

            if not (south <= p["lat"] <= north and west <= p["lon"] <= east):
                continue

            x, y = transformer.transform(p["lon"], p["lat"])

            text_string = (
                f'{p["name"]}\\P'
                f'({p["lat"]:.6f}, {p["lon"]:.6f})'
            )

            text = msp.add_text(
                text_string,
                dxfattribs={
                    "height": 2.0,
                    "layer": "POINT_TEXT"
                }
            )

            text.set_placement(
                (x, y),
                align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER
            )

        # ===============================
        # Lưu file
        # ===============================
        filename = f"survey_{uuid.uuid4().hex[:6]}.dxf"
        filepath = os.path.join(EXPORT_FOLDER, filename)

        doc.saveas(filepath)

        return send_file(filepath, as_attachment=True)

    except Exception as e:
        return f"Lỗi export: {e}", 500

# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
