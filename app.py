import csv, io, os, random, sqlite3
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, Response, jsonify, request, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "heatwave.db")
app = Flask(__name__, static_folder="static")
app.secret_key = "dbms-mini-project-change-me"


def get_db():
    # isolation_level=None -> we control transactions manually (BEGIN IMMEDIATE / COMMIT)
    c = sqlite3.connect(DB, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")  # readers don't block writers
    return c


def q(sql, args=()):
    c = get_db()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def init_db():
    if os.path.exists(DB):
        return
    from load_data import load
    c = get_db()
    c.executescript(open(os.path.join(BASE, "schema.sql")).read())
    c.execute("BEGIN IMMEDIATE")
    c.executemany("INSERT INTO risk_threshold(level,min_temp,advisory) VALUES (?,?,?)", [
        ("Watch", 40, "Stay hydrated and avoid peak sun hours."),
        ("Warning", 43, "Limit outdoor work; hospitals prepare for heatstroke cases."),
        ("Severe", 45, "Red alert: avoid going outdoors; activate cooling shelters.")])
    for u, p, r in (("admin", "admin123", "admin"), ("analyst", "analyst123", "analyst"), ("public", "public123", "public")):
        c.execute("INSERT INTO app_user(username,password_hash,role) VALUES (?,?,?)", (u, generate_password_hash(p), r))
    print("Loading real dataset from data/ ...")
    n = load(c, os.path.join(BASE, "data"))
    if n == 0:
        c.execute("ROLLBACK"); c.close(); os.remove(DB)
        raise SystemExit("No CSV files found. Put the dataset CSVs inside a folder named 'data' next to app.py.")
    c.execute("COMMIT")
    c.close()


def role_required(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if session.get("role") not in roles:
                return jsonify(error="Forbidden for your role"), 403
            return f(*a, **k)
        return w
    return deco


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/login")
def login():
    d = request.get_json() or {}
    u = q("SELECT * FROM app_user WHERE username=?", (d.get("username", ""),))
    if not u or not check_password_hash(u[0]["password_hash"], d.get("password", "")):
        return jsonify(error="Invalid credentials"), 401
    session["role"], session["user"] = u[0]["role"], u[0]["username"]
    return jsonify(user=u[0]["username"], role=u[0]["role"])


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    return jsonify(user=session.get("user"), role=session.get("role", "public"))


# ---- Public (read-only view v_public_advisory) ----
@app.get("/api/districts")
def districts():
    return jsonify([r["district"] for r in q("SELECT district FROM location ORDER BY district")])


@app.get("/api/advisories")
def advisories():
    d = request.args.get("district", "")
    return jsonify(q("""SELECT district, level, temperature, message, created_at FROM v_public_advisory
        WHERE (?='' OR district=?) ORDER BY created_at DESC, alert_id DESC LIMIT 40""", (d, d)))


# ---- Analyst / Admin: analytics (JOIN, GROUP BY, HAVING, nested queries, views) ----
@app.get("/api/summary")
@role_required("analyst", "admin")
def summary():
    return jsonify(q("SELECT * FROM v_district_summary ORDER BY max_temp DESC"))


@app.get("/api/readings")
@role_required("analyst", "admin")
def readings():
    return jsonify(q("""SELECT r.recorded_at, l.district, s.name AS station, r.temperature AS max_temp, r.temp_min, r.rainfall
        FROM climate_reading r JOIN weather_station s ON s.station_id=r.station_id
        JOIN location l ON l.location_id=s.location_id ORDER BY r.recorded_at DESC, r.reading_id DESC LIMIT 30"""))


@app.get("/api/hotspots")
@role_required("analyst", "admin")
def hotspots():
    # Nested query: districts whose alert count is >=3 and whose peak beats the overall average temperature
    return jsonify(q("""SELECT l.district, l.zone_type, COUNT(*) AS alerts, MAX(a.temperature) AS peak_temp,
            SUM(a.level='Severe') AS severe_alerts
        FROM alert a JOIN location l ON l.location_id=a.location_id
        WHERE a.location_id IN (SELECT location_id FROM alert GROUP BY location_id HAVING COUNT(*)>=3)
          AND a.temperature > (SELECT AVG(temperature) FROM climate_reading)
        GROUP BY l.location_id ORDER BY alerts DESC"""))


@app.get("/api/forecast-accuracy")
@role_required("analyst", "admin")
def forecast_accuracy():
    return jsonify(q("""SELECT l.district, ROUND(AVG(ABS(f.predicted_max_temp - d.actual)),2) AS mean_abs_error,
            COUNT(*) AS days_compared
        FROM forecast f JOIN location l ON l.location_id=f.location_id
        JOIN (SELECT s.location_id, date(r.recorded_at) AS day, MAX(r.temperature) AS actual
              FROM climate_reading r JOIN weather_station s ON s.station_id=r.station_id
              GROUP BY s.location_id, date(r.recorded_at)) d
          ON d.location_id=f.location_id AND d.day=f.forecast_date
        GROUP BY l.location_id ORDER BY mean_abs_error"""))


# ---- Admin: ingestion (transaction) and configuration ----
@app.get("/api/stations")
@role_required("admin")
def stations():
    return jsonify(q("SELECT s.station_id, s.name, l.district FROM weather_station s JOIN location l USING(location_id)"))


@app.post("/api/readings")
@role_required("admin")
def add_reading():
    d = request.get_json() or {}
    c = get_db()
    try:
        c.execute("BEGIN IMMEDIATE")  # serialises concurrent writers; trigger runs inside same transaction
        cur = c.execute("INSERT INTO climate_reading(station_id,recorded_at,temperature,humidity,wind_speed) VALUES (?,?,?,?,?)",
                        (d["station_id"], d.get("recorded_at") or datetime.now().strftime("%Y-%m-%d %H:%M"),
                         d["temperature"], d.get("humidity"), d.get("wind_speed")))
        n = c.execute("SELECT COUNT(*) FROM alert WHERE reading_id=?", (cur.lastrowid,)).fetchone()[0]
        c.execute("COMMIT")
        return jsonify(ok=True, alert_generated=n > 0)
    except (sqlite3.Error, KeyError) as e:
        if c.in_transaction:
            c.execute("ROLLBACK")
        return jsonify(error=f"Rejected & rolled back: {e}"), 400
    finally:
        c.close()


@app.get("/api/thresholds")
@role_required("admin", "analyst")
def thresholds():
    return jsonify(q("SELECT * FROM risk_threshold ORDER BY min_temp"))


@app.put("/api/thresholds/<int:tid>")
@role_required("admin")
def update_threshold(tid):
    c = get_db()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE risk_threshold SET min_temp=? WHERE threshold_id=?", (request.get_json()["min_temp"], tid))
        c.execute("COMMIT")
        return jsonify(ok=True)
    except (sqlite3.Error, KeyError) as e:
        if c.in_transaction:
            c.execute("ROLLBACK")
        return jsonify(error=str(e)), 400
    finally:
        c.close()

# ---- Analyst / Admin: analysis reports (enter + export) ----
ANALYSIS_COLS = ["report_id", "district", "period_start", "period_end", "title", "risk_level",
                 "avg_max_temp", "peak_temp", "alert_days", "findings", "analyst", "created_at"]
ANALYSIS_SQL = """SELECT a.report_id, l.district, a.period_start, a.period_end, a.title, a.risk_level,
        a.avg_max_temp, a.peak_temp, a.alert_days, a.findings, u.username AS analyst, a.created_at
    FROM analysis_report a JOIN location l ON l.location_id=a.location_id
    JOIN app_user u ON u.user_id=a.author_id ORDER BY a.created_at DESC, a.report_id DESC"""


@app.get("/api/analysis")
@role_required("analyst", "admin")
def list_analysis():
    return jsonify(q(ANALYSIS_SQL))


@app.post("/api/analysis")
@role_required("analyst", "admin")
def add_analysis():
    d = request.get_json() or {}
    c = get_db()
    try:
        title, findings = (d.get("title") or "").strip(), (d.get("findings") or "").strip()
        if not title or not findings:
            raise ValueError("Title and findings are required")
        for k in ("period_start", "period_end"):
            datetime.strptime(d.get(k) or "", "%Y-%m-%d")
        loc = c.execute("SELECT location_id FROM location WHERE district=?", (d.get("district"),)).fetchone()
        usr = c.execute("SELECT user_id FROM app_user WHERE username=?", (session["user"],)).fetchone()
        if not loc:
            raise ValueError("Unknown district")
        c.execute("BEGIN IMMEDIATE")
        # statistics are computed by the database from the real readings, not typed in by hand
        c.execute("""INSERT INTO analysis_report(location_id,period_start,period_end,title,risk_level,findings,author_id,
                                                 avg_max_temp,peak_temp,alert_days)
            SELECT ?,?,?,?,?,?,?, ROUND(AVG(r.temperature),1), MAX(r.temperature),
                   COALESCE(SUM(r.temperature >= (SELECT MIN(min_temp) FROM risk_threshold)),0)
            FROM climate_reading r JOIN weather_station s ON s.station_id=r.station_id
            WHERE s.location_id=? AND r.recorded_at >= ? AND r.recorded_at < date(?, '+1 day')""",
                  (loc[0], d["period_start"], d["period_end"], title, d.get("risk_level"), findings, usr[0],
                   loc[0], d["period_start"], d["period_end"]))
        c.execute("COMMIT")
        return jsonify(ok=True)
    except (sqlite3.Error, ValueError) as e:
        if c.in_transaction:
            c.execute("ROLLBACK")
        return jsonify(error=f"Not saved: {e}"), 400
    finally:
        c.close()


@app.delete("/api/analysis/<int:rid>")
@role_required("analyst", "admin")
def delete_analysis(rid):
    c = get_db()
    try:
        c.execute("BEGIN IMMEDIATE")
        n = c.execute("""DELETE FROM analysis_report WHERE report_id=? AND
            (author_id=(SELECT user_id FROM app_user WHERE username=?) OR ?='admin')""",
                      (rid, session["user"], session["role"])).rowcount
        c.execute("COMMIT")
        return (jsonify(ok=True) if n else (jsonify(error="Not found, or not your report"), 403))
    finally:
        c.close()


@app.get("/api/analysis/export")
@role_required("analyst", "admin")
def export_analysis():
    safe = lambda v: "'" + v if isinstance(v, str) and v[:1] in "=+-@" else v  # stop spreadsheet formula injection
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(ANALYSIS_COLS)
    for r in q(ANALYSIS_SQL):
        w.writerow([safe(r[k]) for k in ANALYSIS_COLS])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=analysis_reports.csv"})


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
