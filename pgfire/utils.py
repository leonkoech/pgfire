"""Internal SQL-compilation helpers: field-expression building, value
coercion for JSONB comparisons, and NULL-check handling."""

from __future__ import annotations

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
    return str(value)


def json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
