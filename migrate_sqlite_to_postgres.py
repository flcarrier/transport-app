import os
import sqlite3
from contextlib import closing

import psycopg2

SQLITE_DB = os.environ.get("SQLITE_DB", "transport_mvp.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise SystemExit("Set DATABASE_URL first. Example: set DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME")

TYPE_MAP = {
    "INTEGER": "INTEGER",
    "INT": "INTEGER",
    "TEXT": "TEXT",
    "REAL": "NUMERIC",
    "NUMERIC": "NUMERIC",
    "BLOB": "BYTEA",
}


def pg_ident(name):
    return '"' + name.replace('"', '""') + '"'


def sqlite_type_to_pg(sqlite_type):
    upper = (sqlite_type or "TEXT").upper()
    for key, pg_type in TYPE_MAP.items():
        if key in upper:
            return pg_type
    return "TEXT"


def main():
    if not os.path.exists(SQLITE_DB):
        raise SystemExit(f"SQLite file not found: {SQLITE_DB}")

    with closing(sqlite3.connect(SQLITE_DB)) as sq, closing(psycopg2.connect(DATABASE_URL)) as pg:
        sq.row_factory = sqlite3.Row
        sc = sq.cursor()
        pc = pg.cursor()

        sc.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        tables = [row[0] for row in sc.fetchall()]

        for table in tables:
            sc.execute(f"PRAGMA table_info({table})")
            cols = sc.fetchall()
            col_defs = []
            primary_key_col = None
            for col in cols:
                name = col[1]
                col_type = sqlite_type_to_pg(col[2])
                is_pk = bool(col[5])
                not_null = bool(col[3])
                default = col[4]

                if is_pk and col_type == "INTEGER":
                    primary_key_col = name
                    col_defs.append(f"{pg_ident(name)} INTEGER PRIMARY KEY")
                else:
                    definition = f"{pg_ident(name)} {col_type}"
                    if not_null:
                        definition += " NOT NULL"
                    if default is not None:
                        definition += f" DEFAULT {default}"
                    col_defs.append(definition)

            pc.execute(f"CREATE TABLE IF NOT EXISTS {pg_ident(table)} ({', '.join(col_defs)})")
            pg.commit()

            sc.execute(f"SELECT * FROM {table}")
            rows = sc.fetchall()
            if not rows:
                print(f"{table}: created, no rows to copy")
                continue

            column_names = rows[0].keys()
            columns_sql = ", ".join(pg_ident(c) for c in column_names)
            placeholders = ", ".join(["%s"] * len(column_names))
            update_sql = ", ".join(f"{pg_ident(c)} = EXCLUDED.{pg_ident(c)}" for c in column_names if c != primary_key_col)
            conflict_sql = f" ON CONFLICT ({pg_ident(primary_key_col)}) DO UPDATE SET {update_sql}" if primary_key_col and update_sql else ""
            insert_sql = f"INSERT INTO {pg_ident(table)} ({columns_sql}) VALUES ({placeholders}){conflict_sql}"

            for row in rows:
                pc.execute(insert_sql, tuple(row[c] for c in column_names))
            pg.commit()

            if primary_key_col:
                seq_name = f"{table}_{primary_key_col}_seq"
                pc.execute("SELECT to_regclass(%s)", (seq_name,))
                if pc.fetchone()[0]:
                    pc.execute(
                        f"SELECT setval(%s, COALESCE((SELECT MAX({pg_ident(primary_key_col)}) FROM {pg_ident(table)}), 1), true)",
                        (seq_name,),
                    )
                    pg.commit()

            print(f"{table}: copied {len(rows)} rows")

        print("Done. Your PostgreSQL database now has the SQLite data.")


if __name__ == "__main__":
    main()
