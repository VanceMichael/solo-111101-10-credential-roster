import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_records (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS persons (
    person_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    first_generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS employment_intervals (
    id INTEGER PRIMARY KEY,
    person_id TEXT NOT NULL,
    project TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL DEFAULT '',
    certificate TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL,
    source TEXT NOT NULL,
    closed_generation INTEGER,
    UNIQUE (person_id, project, start, end)
);
CREATE TABLE IF NOT EXISTS certificate_intervals (
    id INTEGER PRIMARY KEY,
    certificate TEXT NOT NULL,
    person_id TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL DEFAULT '',
    UNIQUE (certificate, person_id, start, end)
);
CREATE TABLE IF NOT EXISTS work_records (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    project TEXT NOT NULL,
    worked_on TEXT NOT NULL,
    received_on TEXT
);
CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    generation INTEGER NOT NULL,
    person_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (reason, source, generation, person_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    options TEXT NOT NULL,
    executed_at TEXT NOT NULL
);
"""


class RecordStore:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def count(self):
        with self.connect() as connection:
            row = connection.execute("SELECT count(*) AS total FROM source_records").fetchone()
            return row["total"]
