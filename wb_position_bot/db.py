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
          owner_chat_id integer not null default 0,
          marketplace text not null default 'wb',
          external_id text not null default '',
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

        create index if not exists idx_tracked_products_sku on tracked_products(sku);
        create index if not exists idx_tracked_products_active on tracked_products(active);
        create table if not exists bot_users (
          chat_id integer primary key,
          username text not null default '',
          display_name text not null default '',
          active integer not null default 1,
          created_at text not null default current_timestamp,
          updated_at text not null default current_timestamp
        );

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
          check_source text not null default 'manual',
          created_at text not null default current_timestamp,
          foreign key(product_id) references tracked_products(id)
        );

        create index if not exists idx_position_checks_product_checked
          on position_checks(product_id, checked_at);
        """
    )
    ensure_column(conn, "tracked_products", "marketplace", "text not null default 'wb'")
    ensure_column(conn, "tracked_products", "external_id", "text not null default ''")
    ensure_column(conn, "tracked_products", "owner_chat_id", "integer not null default 0")
    ensure_column(conn, "position_checks", "check_source", "text not null default 'manual'")
    conn.execute("create index if not exists idx_tracked_products_owner on tracked_products(owner_chat_id)")
    conn.execute("update tracked_products set marketplace = 'wb' where marketplace is null or marketplace = ''")
    conn.execute(
        "update tracked_products set external_id = cast(nm_id as text) "
        "where marketplace = 'wb' and nm_id is not null and external_id = ''"
    )
    conn.execute("drop index if exists idx_tracked_products_nm_id")
    conn.execute("drop index if exists idx_tracked_products_wb_nm_id")
    conn.execute("drop index if exists idx_tracked_products_market_external")
    conn.execute("drop index if exists idx_tracked_products_market_external_query")
    conn.execute(
        "create unique index if not exists idx_tracked_products_market_external_query "
        "on tracked_products(owner_chat_id, marketplace, external_id, lower(search_query)) "
        "where external_id != ''"
    )
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


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


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("delete from settings where key = ?", (key,))
    conn.commit()


def authorize_user(conn: sqlite3.Connection, chat_id: int, username: str = "", display_name: str = "") -> None:
    conn.execute(
        """
        insert into bot_users(chat_id, username, display_name, active)
        values (?, ?, ?, 1)
        on conflict(chat_id) do update set
          username = excluded.username,
          display_name = excluded.display_name,
          active = 1,
          updated_at = current_timestamp
        """,
        (int(chat_id), str(username or ""), str(display_name or "")),
    )
    conn.commit()


def get_authorized_user(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "select * from bot_users where chat_id = ? and active = 1",
        (int(chat_id),),
    ).fetchone()


def active_authorized_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "select * from bot_users where active = 1 order by created_at, chat_id"
    ).fetchall()


def revoke_user(conn: sqlite3.Connection, chat_id: int) -> bool:
    cursor = conn.execute(
        "update bot_users set active = 0, updated_at = current_timestamp where chat_id = ? and active = 1",
        (int(chat_id),),
    )
    conn.commit()
    return cursor.rowcount > 0


def claim_unowned_targets(conn: sqlite3.Connection, owner_chat_id: int) -> None:
    conn.execute(
        "update tracked_products set owner_chat_id = ? where owner_chat_id = 0",
        (int(owner_chat_id),),
    )
    conn.commit()


def disable_targets_for_owner(conn: sqlite3.Connection, owner_chat_id: int) -> None:
    conn.execute(
        """
        update tracked_products
        set active = 0,
            updated_at = current_timestamp
        where owner_chat_id = ?
        """,
        (int(owner_chat_id),),
    )
    conn.commit()


def row_to_target(row: sqlite3.Row) -> ProductTarget:
    return ProductTarget(
        id=int(row["id"]),
        owner_chat_id=int(row["owner_chat_id"] or 0),
        marketplace=str(row["marketplace"] or "wb"),
        external_id=str(row["external_id"] or ""),
        nm_id=int(row["nm_id"]) if row["nm_id"] is not None else None,
        sku=str(row["sku"] or ""),
        name=str(row["name"] or ""),
        search_query=str(row["search_query"] or ""),
        own_supplier_id=int(row["own_supplier_id"]) if row["own_supplier_id"] is not None else None,
        own_supplier_name=str(row["own_supplier_name"] or ""),
        note=str(row["note"] or ""),
        active=bool(row["active"]),
    )


def active_targets(
    conn: sqlite3.Connection,
    include_inactive: bool = False,
    owner_chat_id: int | None = None,
) -> list[ProductTarget]:
    sql = "select * from tracked_products"
    clauses: list[str] = []
    params: list[object] = []
    if not include_inactive:
        clauses.append("active = 1")
    if owner_chat_id is not None:
        clauses.append("owner_chat_id = ?")
        params.append(int(owner_chat_id))
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by id"
    return [row_to_target(row) for row in conn.execute(sql, params).fetchall()]


def get_target_by_id(conn: sqlite3.Connection, target_id: int) -> ProductTarget | None:
    row = conn.execute("select * from tracked_products where id = ?", (target_id,)).fetchone()
    return row_to_target(row) if row else None


def get_target_by_nm_id(conn: sqlite3.Connection, nm_id: int) -> ProductTarget | None:
    row = conn.execute(
        "select * from tracked_products where marketplace = 'wb' and nm_id = ? order by id limit 1",
        (nm_id,),
    ).fetchone()
    return row_to_target(row) if row else None


def get_target_by_external_id(
    conn: sqlite3.Connection,
    marketplace: str,
    external_id: str,
) -> ProductTarget | None:
    row = conn.execute(
        "select * from tracked_products where marketplace = ? and external_id = ? order by id limit 1",
        (marketplace, str(external_id)),
    ).fetchone()
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
    if target.external_id:
        row = conn.execute(
            "select id from tracked_products "
            "where owner_chat_id = ? and marketplace = ? and external_id = ? "
            "and lower(search_query) = lower(?)",
            (target.owner_chat_id, target.marketplace, target.external_id, target.search_query),
        ).fetchone()
        if row:
            return int(row["id"])
    if target.marketplace == "wb" and target.nm_id:
        row = conn.execute(
            "select id from tracked_products "
            "where owner_chat_id = ? and marketplace = 'wb' and nm_id = ? "
            "and lower(search_query) = lower(?)",
            (target.owner_chat_id, target.nm_id, target.search_query),
        ).fetchone()
        if row:
            return int(row["id"])
    if target.sku:
        row = conn.execute(
            "select id from tracked_products "
            "where owner_chat_id = ? and marketplace = ? and sku = ? and sku != '' "
            "and lower(search_query) = lower(?)",
            (target.owner_chat_id, target.marketplace, target.sku, target.search_query),
        ).fetchone()
        if row:
            return int(row["id"])
    if not target.external_id and not target.nm_id and target.search_query:
        row = conn.execute(
            """
            select id
            from tracked_products
            where marketplace = ?
              and owner_chat_id = ?
              and lower(search_query) = lower(?)
              and lower(own_supplier_name) = lower(?)
            order by id
            limit 1
            """,
            (target.marketplace, target.owner_chat_id, target.search_query, target.own_supplier_name),
        ).fetchone()
        if row:
            return int(row["id"])
    return None


def upsert_target(conn: sqlite3.Connection, target: ProductTarget) -> ProductTarget:
    existing_id = _existing_target_id(conn, target)
    default_name = (
        f"Яндекс Маркет {target.external_id}"
        if target.marketplace == "ym" and target.external_id
        else f"WB {target.nm_id}"
        if target.nm_id
        else target.search_query
    )
    values = (
        target.owner_chat_id,
        target.marketplace,
        target.external_id or (str(target.nm_id) if target.marketplace == "wb" and target.nm_id else ""),
        target.nm_id,
        target.sku or (str(target.nm_id) if target.nm_id else ""),
        target.name or default_name,
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
            set owner_chat_id = ?,
                marketplace = ?,
                external_id = ?,
                nm_id = ?,
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
          owner_chat_id, marketplace, external_id, nm_id, sku, name, search_query,
          own_supplier_id, own_supplier_name, note, active
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    conn.commit()
    saved = get_target_by_id(conn, int(cursor.lastrowid))
    assert saved is not None
    return saved


def save_position_check(conn: sqlite3.Connection, analysis: PositionAnalysis, check_source: str = "manual") -> int:
    if not analysis.target.id:
        saved = upsert_target(conn, analysis.target)
        analysis = analysis.with_target(saved)
    cursor = conn.execute(
        """
        insert into position_checks(
          product_id, query, checked_at, own_position, match_reason,
          pages_checked, top_json, own_item_json, warnings_json, check_source
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            normalize_check_source(check_source),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def normalize_check_source(value: str) -> str:
    return "auto" if str(value or "").strip().lower() == "auto" else "manual"


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
