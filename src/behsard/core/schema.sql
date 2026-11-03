PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS worker (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    personnel_code TEXT UNIQUE,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS stamp (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS conveyor (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    coefficient REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS camera (
    id TEXT PRIMARY KEY,
    conveyor_id INTEGER NOT NULL REFERENCES conveyor(id),
    side TEXT NOT NULL CHECK (side IN ('L', 'R')),
    host TEXT
);

CREATE TABLE IF NOT EXISTS shift (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT
);

CREATE TABLE IF NOT EXISTS shift_assignment (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL REFERENCES shift(id) ON DELETE CASCADE,
    worker_id INTEGER NOT NULL REFERENCES worker(id),
    stamp_id INTEGER NOT NULL REFERENCES stamp(id),
    conveyor_id INTEGER NOT NULL REFERENCES conveyor(id),
    UNIQUE (shift_id, stamp_id),
    UNIQUE (shift_id, worker_id)
);

CREATE TABLE IF NOT EXISTS carton_event (
    id INTEGER PRIMARY KEY,
    conveyor_id INTEGER NOT NULL REFERENCES conveyor(id),
    shift_id INTEGER REFERENCES shift(id),
    ts TEXT NOT NULL,
    status TEXT NOT NULL,
    stamp_id INTEGER REFERENCES stamp(id),
    confidence REAL NOT NULL DEFAULT 0,
    frame_path TEXT,
    crop_path TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_carton_event_status ON carton_event(status);
CREATE INDEX IF NOT EXISTS ix_carton_event_shift ON carton_event(shift_id);
CREATE INDEX IF NOT EXISTS ix_carton_event_ts ON carton_event(ts);

CREATE TABLE IF NOT EXISTS recognition_result (
    id INTEGER PRIMARY KEY,
    carton_event_id INTEGER NOT NULL REFERENCES carton_event(id) ON DELETE CASCADE,
    recognizer TEXT NOT NULL,
    stamp_code TEXT,
    confidence REAL NOT NULL,
    raw_text TEXT NOT NULL DEFAULT '',
    camera_id TEXT,
    crop_path TEXT,
    frame_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_review (
    id INTEGER PRIMARY KEY,
    carton_event_id INTEGER NOT NULL REFERENCES carton_event(id) ON DELETE CASCADE,
    decision TEXT NOT NULL,
    stamp_id INTEGER REFERENCES stamp(id),
    reviewer TEXT,
    created_at TEXT NOT NULL
);
