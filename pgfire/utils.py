"""Internal SQL-compilation helpers: field-expression building, value
coercion for JSONB comparisons, and NULL-check handling."""

from __future__ import annotations

import json
from datetime import datetime, date
from typing import Any, Optional

OP_SQL = {
    "==": "=",
    "!=": "!=",
    "<": "<",
    "<=": "<=",
    ">": ">",
    ">=": ">=",
    "in": "IN",
}


def field_expr(field: str, real_columns: set) -> str:
    """Build the SQL expression for a field: a real column if promoted,
    otherwise a JSONB text-extraction from `data`."""
    if field in real_columns:
        return field
    # supports dotted paths for nested JSON, e.g. "company_data.company_id"
    parts = field.split(".")
    if len(parts) == 1:
        return f"data->>'{parts[0]}'"
    path = "->".join(f"'{p}'" for p in parts[:-1])
    return f"data->{path}->>'{parts[-1]}'"


def field_jsonb_expr(field: str) -> str:
    """Like field_expr's JSONB fallback but keeps the value as jsonb
    (`->` instead of `->>`). jsonb compares numbers numerically, while
    `->>` text compares them lexically ("10" < "9")."""
    parts = field.split(".")
    return "data->" + "->".join(f"'{p}'" for p in parts)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compare_sql(field: str, op: str, value: Any, real_columns: set) -> tuple:
    """SQL fragment + params for one (field, op, value) filter."""
    sql_op = OP_SQL.get(op)
    if sql_op is None:
        raise NotImplementedError(f"Unsupported operator: {op}")
    is_real_column = field in real_columns
    expr = field_expr(field, real_columns)
    null_sql = null_check_sql(expr, op, value)
    if null_sql is not None:
        return null_sql, []
    if op == "in":
        placeholders = ", ".join(["%s"] * len(value))
        return f"{expr} IN ({placeholders})", [coerce(v, is_real_column) for v in value]
    if not is_real_column and is_number(value):
        # Numeric comparison against a JSONB number: compare as jsonb so
        # 10 > 9 and 5 == 5.0, matching Firestore's numeric semantics.
        return f"{field_jsonb_expr(field)} {sql_op} %s::jsonb", [json.dumps(value)]
    return f"{expr} {sql_op} %s", [coerce(value, is_real_column)]


def order_expr(field: str, real_columns: set) -> str:
    """ORDER BY expression: real columns as-is; JSONB fields as jsonb so
    numbers sort numerically (string ordering is unchanged - jsonb strings
    compare with the same collation as text)."""
    return field if field in real_columns else field_jsonb_expr(field)


def null_check_sql(expr: str, op: str, value: Any) -> Optional[str]:
    """SQL has no `= NULL`/`!= NULL` (always evaluates to NULL, never
    matches) - `IS [NOT] NULL` is required. Returns the SQL fragment if
    this is a None-comparison, else None (caller falls back to a normal
    parameterized comparison)."""
    if value is not None or op not in ("==", "!="):
        return None
    return f"{expr} IS NULL" if op == "==" else f"{expr} IS NOT NULL"


def coerce(value: Any, is_real_column: bool) -> Any:
    """`data->>'field'` always returns TEXT (the JSON scalar's text form:
    true/false, a number's textual form, or the raw string) - a Python
    bool/int/float compared against that needs to become that same textual
    form, or Postgres raises "operator does not exist". Real/promoted
    columns keep their native type since they compare directly."""
    if is_real_column or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        # Must match how json_default() serialized it on write (isoformat,
        # "T" separator). str(datetime) uses a space instead, and since
        # " " sorts before "T", same-day range comparisons against stored
        # timestamps would silently give the wrong answer.
        return value.isoformat()
    return str(value)


def json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
