-- Heatwave Climate Intelligence System (KJS-CES-01) -- SQLite schema (3NF/BCNF)
PRAGMA foreign_keys = ON;

CREATE TABLE location (
  location_id INTEGER PRIMARY KEY,
  district    TEXT NOT NULL UNIQUE,
  state       TEXT NOT NULL,
  latitude    REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
  longitude   REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
  zone_type   TEXT NOT NULL CHECK (zone_type IN ('Urban','Rural','Coastal'))
);

CREATE TABLE weather_station (
  station_id  INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  location_id INTEGER NOT NULL REFERENCES location(location_id) ON DELETE CASCADE,
  status      TEXT NOT NULL DEFAULT 'Active' CHECK (status IN ('Active','Inactive','Maintenance')),
  installed_on TEXT NOT NULL
);

CREATE TABLE sensor (
  sensor_id   INTEGER PRIMARY KEY,
  station_id  INTEGER NOT NULL REFERENCES weather_station(station_id) ON DELETE CASCADE,
  sensor_type TEXT NOT NULL CHECK (sensor_type IN ('Temperature','Humidity','Wind Speed')),
  unit        TEXT NOT NULL,
  UNIQUE (station_id, sensor_type)
);

CREATE TABLE risk_threshold (
  threshold_id INTEGER PRIMARY KEY,
  level        TEXT NOT NULL UNIQUE CHECK (level IN ('Watch','Warning','Severe')),
  min_temp     REAL NOT NULL UNIQUE,
  advisory     TEXT NOT NULL
);

CREATE TABLE climate_reading (
  reading_id  INTEGER PRIMARY KEY,
  station_id  INTEGER NOT NULL REFERENCES weather_station(station_id) ON DELETE CASCADE,
  recorded_at TEXT NOT NULL,
  temperature REAL NOT NULL CHECK (temperature BETWEEN -50 AND 60),   -- daily MAXIMUM temperature (C)
  temp_min    REAL,
  temp_avg    REAL,
  rainfall    REAL CHECK (rainfall IS NULL OR rainfall >= 0),
  humidity    REAL CHECK (humidity BETWEEN 0 AND 100),                -- optional (not in dataset)
  wind_speed  REAL CHECK (wind_speed >= 0),                           -- optional (km/h)
  UNIQUE (station_id, recorded_at)          -- prevents duplicate ingestion
);

CREATE TABLE forecast (
  forecast_id INTEGER PRIMARY KEY,
  location_id INTEGER NOT NULL REFERENCES location(location_id) ON DELETE CASCADE,
  forecast_date TEXT NOT NULL,
  predicted_max_temp REAL NOT NULL,
  model_name  TEXT NOT NULL,
  UNIQUE (location_id, forecast_date, model_name)
);

CREATE TABLE alert (
  alert_id    INTEGER PRIMARY KEY,
  location_id INTEGER NOT NULL REFERENCES location(location_id) ON DELETE CASCADE,
  station_id  INTEGER NOT NULL REFERENCES weather_station(station_id),
  reading_id  INTEGER NOT NULL REFERENCES climate_reading(reading_id) ON DELETE CASCADE,
  level       TEXT NOT NULL CHECK (level IN ('Watch','Warning','Severe')),
  temperature REAL NOT NULL,
  message     TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE app_user (
  user_id  INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role     TEXT NOT NULL CHECK (role IN ('admin','analyst','public'))
);

-- Analyst-entered analysis reports (stats are computed from climate_reading when saved)
CREATE TABLE analysis_report (
  report_id    INTEGER PRIMARY KEY,
  location_id  INTEGER NOT NULL REFERENCES location(location_id) ON DELETE CASCADE,
  period_start TEXT NOT NULL,
  period_end   TEXT NOT NULL,
  title        TEXT NOT NULL,
  risk_level   TEXT NOT NULL CHECK (risk_level IN ('Low','Moderate','High','Extreme')),
  findings     TEXT NOT NULL,
  avg_max_temp REAL,
  peak_temp    REAL,
  alert_days   INTEGER,
  author_id    INTEGER NOT NULL REFERENCES app_user(user_id),
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (period_end >= period_start)
);

-- Indexes for time-series retrieval
CREATE INDEX idx_analysis_location ON analysis_report(location_id, created_at DESC);
CREATE INDEX idx_reading_station_time ON climate_reading(station_id, recorded_at DESC);
CREATE INDEX idx_reading_time         ON climate_reading(recorded_at);
CREATE INDEX idx_alert_location_time  ON alert(location_id, created_at DESC);
CREATE INDEX idx_station_location     ON weather_station(location_id);

-- Trigger: auto-generate an alert at the highest threshold level breached
CREATE TRIGGER trg_reading_alert
AFTER INSERT ON climate_reading
WHEN NEW.temperature >= (SELECT MIN(min_temp) FROM risk_threshold)
BEGIN
  INSERT INTO alert(location_id, station_id, reading_id, level, temperature, message, created_at)
  SELECT s.location_id, NEW.station_id, NEW.reading_id, t.level, NEW.temperature,
         t.level || ': ' || NEW.temperature || ' C recorded. ' || t.advisory, NEW.recorded_at
  FROM weather_station s, risk_threshold t
  WHERE s.station_id = NEW.station_id
    AND t.min_temp = (SELECT MAX(min_temp) FROM risk_threshold WHERE min_temp <= NEW.temperature);
END;

-- Views
CREATE VIEW v_district_summary AS
SELECT l.district, l.zone_type,
       COUNT(r.reading_id) AS readings,
       ROUND(AVG(r.temperature),1) AS avg_max_temp,
       MAX(r.temperature) AS max_temp,
       ROUND(AVG(r.temp_avg),1) AS avg_daily_mean
FROM location l
JOIN weather_station s ON s.location_id = l.location_id
JOIN climate_reading r ON r.station_id = s.station_id
GROUP BY l.location_id;

CREATE VIEW v_public_advisory AS       -- read-only view exposed to the public role
SELECT a.alert_id, l.district, l.state, a.level, a.temperature, a.message, a.created_at
FROM alert a JOIN location l ON l.location_id = a.location_id;
