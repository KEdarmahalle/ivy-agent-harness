"""Data Analytics Tool Pack: SQLite queries and JMESPath JSON transformations."""

from __future__ import annotations

import sqlite3
from typing import Any


def json_transform(data: Any, query: str) -> Any:
    import jmespath
    return jmespath.search(query, data)


def sql_query(query: str, db_path: str | None = None, limit: int = 100) -> dict[str, Any]:
    db = db_path or ":memory:"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        if ";" in query.strip()[:-1]:
            cur.executescript(query)
            conn.commit()
            return {"rows": [], "count": 0, "status": "script_executed"}
        cur.execute(query)
        rows = [dict(r) for r in cur.fetchmany(limit)]
        conn.commit()
        return {"rows": rows, "count": len(rows), "columns": [d[0] for d in cur.description] if cur.description else []}
    finally:
        conn.close()
