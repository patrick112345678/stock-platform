"""啟動時補齊舊資料庫缺少的欄位（無 Alembic 時使用）。"""

from sqlalchemy import inspect, text

from app.db.database import engine


def ensure_users_plan_expires_column() -> None:
    try:
        insp = inspect(engine)
        if "users" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("users")}
        if "plan_expires_at" in cols:
            return
        dialect = engine.dialect.name
        if dialect == "postgresql":
            ddl = "ALTER TABLE users ADD COLUMN plan_expires_at TIMESTAMP WITH TIME ZONE"
        else:
            ddl = "ALTER TABLE users ADD COLUMN plan_expires_at DATETIME"
        with engine.begin() as conn:
            conn.execute(text(ddl))
    except Exception as e:
        print("ensure_users_plan_expires_column:", repr(e))
