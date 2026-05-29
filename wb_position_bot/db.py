from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .models import PositionAnalysis, ProductTarget


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists settings (
          key text primary key,
          value text not null
        );

        create table if not exists tracked_products (
          id integer primary key autoincrement,
          nm_id integer,
          sku text not null default '',
          name text not null default '',
          search_query text not null,
          own_supplier_id integer,
          own_supplier_name text not null default '',
          note text not null default '',
          active integer not null default 1,
          created_at text not null default current_timestamp,
          updated_at text not null default current_timestamp
        );

        create unique index if not exists idx_tracked_products_nm_id
          on tracked_products(nm_id)
          where nm_id is not null;

        create index if not exists idx_tracked_products_sku on tracked_products(sku);
        create index if not exists idx_tracked_products_active on tracked_products(active);

        create table if not exists position_checks (
          id integer primary key autoincrement,
          product_id integer not null,
          query text not null,
          checked_at text not null,
          own_position integer,
          match_reason text not null default '',
          pages_checked integer not null default 0,
          top_json text not null,
          own_item_json text,
          warnings_json text not null default '[]',
          created_at text not null default current_timestamp,
          foreign key(product_id) references tracked_products(id)
        );

        create index if not exists idx_position_checks_product_checked
          on position_checks(product_id, checked_at);
        """
    )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("select value from settings where key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "insert into settings(key, value) values(?, ?) "
        "on conflict(key) do update set value = excluded.value",
        (key, value),
    )
    conn.commit()


def row_to_target(row: sqlite3.Row) -> ProductTarget:
    return ProductTarget(
        id=int(row["id"]),
        nm_id=int(row["nm_id"]) if row["nm_id"] is not None else None,
        sku=str(row["sku"] or ""),
        name=str(row["name"] or ""),
        search_query=str(row["search_query"] or ""),
        own_supplier_id=int(row["own_supplier_id"]) if row["own_supplier_id"] is not None else None,
        own_supplier_name=str(row["own_supplier_name"] or ""),
        note=str(row["note"] or ""),
        active=bool(row["active"]),
    )


def active_targets(conn: sqlite3.Connection, include_inactive: bool = False) -> list[ProductTarget]:
    sql = "select * from tracked_products"
    if not include_inactive:
        sql += " where active = 1"
    sql += " order by id"
    return [row_to_target(row) for row in conn.execute(sql).fetchall()]


def get_target_by_id(conn: sqlite3.Connection, target_id: int) -> ProductTarget | None:
    row = conn.execute("select * from tracked_products where id = ?", (target_id,)).fetchone()
    return row_to_target(row) if row else None


def get_target_by_nm_id(conn: sqlite3.Connection, nm_id: int) -> ProductTarget | None:
    row = conn.execute("select * from tracked_products where nm_id = ?", (nm_id,)).fetchone()
    return row_to_target(row) if row else None


def get_target_by_sku(conn: sqlite3.Connection, sku: str) -> ProductTarget | None:
    row = conn.execute("select * from tracked_products where sku = ?", (sku,)).fetchone()
    return row_to_target(row) if row else None


def set_target_active(conn: sqlite3.Connection, target_id: int, active: bool) -> ProductTarget | None:
    conn.execute(
        """
        update tracked_products
        set active = ?,
            updated_at = current_timestamp
        where id = ?
        """,
        (1 if active else 0, target_id),
    )
    conn.commit()
    return get_target_by_id(conn, target_id)


def _existing_target_id(conn: sqlite3.Connection, target: ProductTarget) -> int | None:
    if target.id:
        return target.id
    if target.nm_id:
        row = conn.execute("select id from tracked_products where nm_id = ?", (target.nm_id,)).fetchone()
        if row:
            return int(row["id"])
    if target.sku:
        row = conn.execute("select id from tracked_products where sku = ? and sku != ''", (target.sku,)).fetchone()
        if row:
            return int(row["id"])
    if target.search_query:
        row = conn.execute(
            """
            select id
            from tracked_products
            where lower(search_query) = lower(?)
              and lower(own_supplier_name) = lower(?)
            order by id
            limit 1
            """,
            (target.search_query, target.own_supplier_name),
        ).fetchone()
        if row:
            return int(row["id"])
    return None


def upsert_target(conn: sqlite3.Connection, target: ProductTarget) -> ProductTarget:
    existing_id = _existing_target_id(conn, target)
    values = (
        target.nm_id,
        target.sku or (str(target.nm_id) if target.nm_id else ""),
        target.name or (f"WB {target.nm_id}" if target.nm_id else target.search_query),
        target.search_query,
        target.own_supplier_id,
        target.own_supplier_name,
        target.note,
        1 if target.active else 0,
    )
    if existing_id:
        conn.execute(
            """
            update tracked_products
            set nm_id = ?,
                sku = ?,
                name = ?,
                search_query = ?,
                own_supplier_id = ?,
                own_supplier_name = ?,
                note = ?,
                active = ?,
                updated_at = current_timestamp
            where id = ?
            """,
            values + (existing_id,),
        )
        conn.commit()
        saved = get_target_by_id(conn, existing_id)
        assert saved is not None
        return saved

    cursor = conn.execute(
        """
        insert into tracked_products(
          nm_id, sku, name, search_query, own_supplier_id, own_supplier_name, note, active
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    conn.commit()
    saved = get_target_by_id(conn, int(cursor.lastrowid))
    assert saved is not None
    return saved


def save_position_check(conn: sqlite3.Connection, analysis: PositionAnalysis) -> int:
    if not analysis.target.id:
        saved = upsert_target(conn, analysis.target)
        analysis = analysis.with_target(saved)
    cursor = conn.execute(
        """
        insert into position_checks(
          product_id, query, checked_at, own_position, match_reason,
          pages_checked, top_json, own_item_json, warnings_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis.target.id,
            analysis.query,
            analysis.checked_at,
            analysis.own_position,
            analysis.match_reason,
            analysis.pages_checked,
            json.dumps([asdict(item) for item in analysis.top_items], ensure_ascii=False),
            json.dumps(asdict(analysis.own_item), ensure_ascii=False) if analysis.own_item else None,
            json.dumps(analysis.warnings, ensure_ascii=False),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def latest_checks(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        select pc.*, tp.nm_id, tp.search_query
        from position_checks pc
        join tracked_products tp on tp.id = pc.product_id
        order by datetime(pc.checked_at) desc, pc.id desc
        limit ?
        """,
        (limit,),
    ).fetchall()


def position_checks_for_target(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        select *
        from position_checks
        where product_id = ?
        order by checked_at, id
        """,
        (target_id,),
    ).fetchall()
