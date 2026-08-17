"""SQLite access layer for the user store."""

import os
import sqlite3

from flask import g

# Where the SQLite file lives. Overridable so the app can point at a mounted
# persistent volume in production instead of the project folder.
DATABASE_PATH = os.environ.get(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def get_db():
    """Return the per-request connection, opening one if needed."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
        # Better concurrency and durability for a small web app.
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the schema if it does not exist yet."""
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


def create_user(username, password_hash):
    """Insert a user. Returns the new id, or None if the username is taken."""
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hash),
        )
        db.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        db.rollback()
        return None


def get_user_by_username(username):
    return get_db().execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def get_user_by_id(user_id):
    return get_db().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def update_password_hash(user_id, password_hash):
    db = get_db()
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
    )
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
