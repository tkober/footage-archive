CREATE TABLE IF NOT EXISTS Files
(
    md5_hash        TEXT PRIMARY KEY,
    file_name       TEXT,
    file_extension  TEXT,
    directory       TEXT,
    last_indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx__Files__directory ON Files (directory);

CREATE TABLE IF NOT EXISTS FileDetails
(
    md5_hash           TEXT PRIMARY KEY,
    file_path          TEXT,
    width              INTEGER,
    height             INTEGER,
    frame_rate         REAL,
    video_codec        TEXT,
    bit_depth          INTEGER,
    audio_codec        INTEGER,
    audio_bit_depth    INTEGER,
    audio_sample_rate  INTEGER,
    audio_channels     INTEGER,
    duration_tc        TEXT,
    description        TEXT,
    shot               TEXT,
    scene              TEXT,
    take               TEXT,
    angle              TEXT,
    move               TEXT,
    shot_type          TEXT,
    recorded_at        TEXT,
    last_modified_at   TEXT,
    frame_rate_verbose TEXT,
    resolution         INTEGER,
    json               TEXT
);
CREATE INDEX IF NOT EXISTS idx__FileDetails__file_path ON FileDetails (file_path);

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
