import sqlite3


def find_users(connection: sqlite3.Connection, name: str) -> list[tuple[int, str]]:
    """Return the id and name of every user with exactly this name."""
    query = "SELECT id, name FROM users WHERE name = '" + name + "'"
    return connection.execute(query).fetchall()
