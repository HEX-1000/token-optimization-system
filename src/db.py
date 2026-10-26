import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).parent / "data" / "optimizer.db"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    return conn


def init_db():
    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            extension TEXT,
            size INTEGER NOT NULL,
            modified REAL NOT NULL,
            content_hash TEXT,
            indexed_at REAL NOT NULL
        )
    """)

    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            path,
            section,
            content
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS allowed_roots (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            registered_at REAL NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY,
            timestamp REAL NOT NULL,
            operation TEXT NOT NULL,
            target TEXT,
            characters INTEGER NOT NULL,
            estimated_tokens INTEGER NOT NULL,
            result_count INTEGER NOT NULL DEFAULT 0,
            potential_tokens INTEGER NOT NULL DEFAULT 0,
            saved_tokens INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Migration for databases created before the savings columns existed.
    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(usage_events)"
        ).fetchall()
    }

    if "potential_tokens" not in columns:
        conn.execute("""
            ALTER TABLE usage_events
            ADD COLUMN potential_tokens INTEGER NOT NULL DEFAULT 0
        """)

    if "saved_tokens" not in columns:
        conn.execute("""
            ALTER TABLE usage_events
            ADD COLUMN saved_tokens INTEGER NOT NULL DEFAULT 0
        """)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized: {DB_PATH}")