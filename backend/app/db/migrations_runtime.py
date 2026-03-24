"""啟動時補齊舊資料庫缺少的欄位（無 Alembic 時使用）。"""

from sqlalchemy import inspect, text

from app.db.database import engine


def _backfill_watchlist_sort_order() -> None:
    from app.db.database import SessionLocal
    from app.models.watchlist import Watchlist

    db = SessionLocal()
    try:
        pairs = db.query(Watchlist.user_id, Watchlist.market).distinct().all()
        for uid, mkt in pairs:
            rows = (
                db.query(Watchlist)
                .filter(Watchlist.user_id == uid, Watchlist.market == mkt)
                .order_by(Watchlist.id.asc())
                .all()
            )
            for i, w in enumerate(rows):
                w.sort_order = i
        db.commit()
    except Exception as e:
        print("_backfill_watchlist_sort_order:", repr(e))
        db.rollback()
    finally:
        db.close()


def ensure_watchlist_sort_order_column() -> None:
    try:
        insp = inspect(engine)
        if "watchlist_items" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("watchlist_items")}
        if "sort_order" in cols:
            return
        dialect = engine.dialect.name
        if dialect == "postgresql":
            ddl = "ALTER TABLE watchlist_items ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
        else:
            ddl = "ALTER TABLE watchlist_items ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
        with engine.begin() as conn:
            conn.execute(text(ddl))
        _backfill_watchlist_sort_order()
    except Exception as e:
        print("ensure_watchlist_sort_order_column:", repr(e))


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
