"""Loads the real Kaggle dataset 'Weather data Indian cities (1990 to 2022)' into the database."""
import csv, glob, os, re
from datetime import date, timedelta

# district, state, latitude, longitude, zone, file pattern, station name
# NOTE: the Jodhpur CSV in the dataset is an exact copy of the Bangalore CSV, so it is not used.
# NOTE: the dataset's geolocation file has the Bangalore and Rajasthan coordinates swapped; correct ones used here.
CITIES = [
    ("Delhi", "Delhi", 28.5833, 77.2, "Urban", "Delhi*", "Safdarjung"),
    ("Lucknow", "Uttar Pradesh", 26.8667, 80.933, "Urban", "Lucknow*", "Lucknow"),
    ("Mumbai", "Maharashtra", 19.1167, 72.85, "Coastal", "Mumbai*", "Santacruz"),
    ("Chennai", "Tamil Nadu", 13.0667, 80.25, "Coastal", "Chennai*", "Madras"),
    ("Bangalore", "Karnataka", 12.9667, 77.5833, "Urban", "Bangalore*", "Bangalore City"),
    ("Bhubaneswar", "Odisha", 20.25, 85.8333, "Urban", "*Bhubhneshwar*", "Bhubaneswar"),
    ("Rourkela", "Odisha", 22.2167, 84.8833, "Urban", "*Rourkela*", "Rourkela"),
]


def num(x):
    try:
        return float(x) if x not in ("", None) else None
    except ValueError:
        return None


def iso(s):
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})$", s)
    return f"{m[3]}-{m[2]}-{m[1]}" if m else s[:10]


def baseline_forecast(tm, last_n=365):
    """Climatology baseline: mean max temp of the same day (+/-3 days) over the previous 5 years."""
    out = []
    for d in sorted(tm)[-last_n:]:
        vals = []
        for y in range(d.year - 5, d.year):
            try:
                base = d.replace(year=y)
            except ValueError:
                base = d.replace(year=y, day=28)
            vals += [tm[base + timedelta(k)] for k in range(-3, 4) if base + timedelta(k) in tm]
        if vals:
            out.append((d.isoformat(), round(sum(vals) / len(vals), 1)))
    return out


def load(c, data_dir):
    loaded = 0
    for lid, (dist, state, lat, lon, zone, pat, stn) in enumerate(CITIES, 1):
        files = glob.glob(os.path.join(data_dir, pat + ".csv"))
        if not files:
            print(f"  skipped {dist}: no file matching {pat}.csv in {data_dir}")
            continue
        c.execute("INSERT INTO location VALUES (?,?,?,?,?,?)", (lid, dist, state, lat, lon, zone))
        c.execute("INSERT INTO weather_station(station_id,name,location_id,installed_on) VALUES (?,?,?,?)", (lid, stn, lid, "1990-01-01"))
        c.execute("INSERT INTO sensor(station_id,sensor_type,unit) VALUES (?,?,?)", (lid, "Temperature", "C"))
        rows, tm = [], {}
        for r in csv.DictReader(open(files[0], newline="")):
            tmax = num(r.get("tmax"))
            if tmax is None:  # daily max drives alerts, so days without it are skipped
                continue
            day = iso(r["time"])
            tm[date.fromisoformat(day)] = tmax
            rows.append((lid, day, tmax, num(r.get("tmin")), num(r.get("tavg")), num(r.get("prcp")), num(r.get("wspd"))))
        c.executemany("INSERT OR IGNORE INTO climate_reading(station_id,recorded_at,temperature,temp_min,temp_avg,rainfall,wind_speed) VALUES (?,?,?,?,?,?,?)", rows)
        c.executemany("INSERT INTO forecast(location_id,forecast_date,predicted_max_temp,model_name) VALUES (?,?,?,?)",
                      [(lid, d, p, "Climatology-5yr") for d, p in baseline_forecast(tm)])
        print(f"  {dist}: {len(rows)} readings")
        loaded += 1
    return loaded
