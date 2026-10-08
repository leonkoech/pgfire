import os
import pytest
import psycopg2

from pgfire import PostgresClient


def _dsn() -> str:
    return os.environ.get(
        "PGFIRE_TEST_DSN",
        "host=localhost port=5432 dbname=pgfire_test user=postgres password=postgres sslmode=disable",
    )


@pytest.fixture
def db():
    client = PostgresClient(_dsn())
    with client.conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS test_users, test_users__notes")
        cur.execute("""
            CREATE TABLE test_users (
                id TEXT PRIMARY KEY,
                data JSONB NOT NULL,
                user_type TEXT GENERATED ALWAYS AS (data->>'user_type') STORED,
                is_demo BOOLEAN GENERATED ALWAYS AS (COALESCE((data->>'is_demo')::boolean, false)) STORED,
                created_at TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE test_users__notes (
                id TEXT NOT NULL,
                parent_id TEXT NOT NULL,
                data JSONB NOT NULL,
                PRIMARY KEY (parent_id, id)
            )
        """)
    client.conn.commit()
    yield client
    with client.conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS test_users, test_users__notes")
    client.conn.commit()
    client.close()
