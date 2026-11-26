import sqlite3

from repository import find_users


def test_input_is_not_treated_as_sql():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE users (id INTEGER, name TEXT)")
    connection.execute("INSERT INTO users VALUES (1, 'ann')")
    assert find_users(connection, "x' OR '1'='1") == []
