CREATE TABLE IF NOT EXISTS Locations
(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT,
    city      TEXT,
    region    TEXT,
    country   TEXT,
    latitude  REAL,
    longitude REAL
);
CREATE INDEX IF NOT EXISTS idx__Locations__country ON Locations (country);
CREATE INDEX IF NOT EXISTS idx__Locations__city ON Locations (city);
CREATE INDEX IF NOT EXISTS idx__Locations__country_region_city ON Locations (country, region, city);

CREATE TABLE IF NOT EXISTS Files
(
    md5_hash        TEXT PRIMARY KEY,
    file_name       TEXT,
    file_extension  TEXT,
    media_type      TEXT,
    directory       TEXT,
    last_indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx__Files__directory ON Files (directory);

CREATE TABLE IF NOT EXISTS FileDetails
(
    md5_hash         TEXT PRIMARY KEY,
    location_id      INTEGER REFERENCES Locations (id),
    latitude         REAL,
    longitude        REAL,
    description      TEXT,
    recorded_at      TEXT,
    last_modified_at TEXT,
    json             TEXT
);

CREATE TABLE IF NOT EXISTS VideoDetails
(
    md5_hash           TEXT PRIMARY KEY,
    width              INTEGER,
    height             INTEGER,
    frame_rate         REAL,
    frame_rate_verbose TEXT,
    video_codec        TEXT,
    bit_depth          INTEGER,
    audio_codec        TEXT,
    audio_bit_depth    INTEGER,
    audio_sample_rate  INTEGER,
    audio_channels     INTEGER,
    duration_tc        TEXT,
    shot               TEXT,
    scene              TEXT,
    take               TEXT,
    angle              TEXT,
    move               TEXT,
    shot_type          TEXT
);

CREATE TABLE IF NOT EXISTS PhotoDetails
(
    md5_hash      TEXT PRIMARY KEY,
    width         INTEGER,
    height        INTEGER,
    camera_make   TEXT,
    camera_model  TEXT,
    iso           INTEGER,
    aperture      REAL,
    shutter_speed TEXT,
    focal_length  REAL,
    color_space   TEXT,
    bit_depth     INTEGER
);

CREATE TABLE IF NOT EXISTS Keywords
(
    md5_hash TEXT,
    keyword  TEXT,

    PRIMARY KEY (md5_hash, keyword)
);
CREATE INDEX IF NOT EXISTS idx__Keywords__keyword ON Keywords (keyword);

CREATE TABLE IF NOT EXISTS ClipPreviews
(
    md5_hash       TEXT PRIMARY KEY,
    frames         INTEGER,
    frame_height   INTEGER,
    frame_width    INTEGER,
    padding        INTEGER,
    overall_height INTEGER,
    overall_width  INTEGER,
    data           BLOB
);
