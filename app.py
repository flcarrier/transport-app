from flask import Flask, request, redirect, render_template_string, jsonify, send_from_directory, send_file
import os
import sqlite3

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:
    psycopg2 = None
    RealDictCursor = None
import textwrap
from contextlib import closing
from datetime import datetime, timedelta
from io import BytesIO
import math

try:
    import pgeocode
except Exception:
    pgeocode = None

try:
    import requests
except Exception:
    requests = None
from werkzeug.utils import secure_filename

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter
from PIL import Image

app = Flask(__name__)
DB = "transport_mvp.db"
# To use a shared PostgreSQL database, set DATABASE_URL in your environment.
# If DATABASE_URL is not set, the app keeps using the local SQLite file above.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
UPLOAD_FOLDER = "uploads"
SAVED_INVOICE_FOLDER = "saved_invoice_batches"
ALLOWED_EXTENSIONS = {
    "pdf", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "doc", "docx", "xls", "xlsx", "txt", "csv"
}
LOAD_STATUSES = ["Planned", "Dispatched", "In Transit", "Delivered", "Cancelled"]

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SAVED_INVOICE_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

BASE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Transport MVP</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; background: #f7f7f7; color: #222; }
    h1, h2, h3 { margin-bottom: 8px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
    .card { background: white; padding: 16px; border-radius: 14px; box-shadow: 0 2px 8px rgba(0,0,0,.08); }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; background: white; }
    th, td { padding: 10px; border-bottom: 1px solid #e7e7e7; text-align: left; vertical-align: top; }
    input, select, button, textarea { padding: 10px; margin: 6px 0; width: 100%; box-sizing: border-box; }
    input[type='file'] { padding: 6px; }
    a { color: #0a58ca; text-decoration: none; }
    .nav a { margin-right: 14px; }
    .muted { color: #666; font-size: 14px; }
    .pill { display: inline-block; padding: 4px 8px; border-radius: 999px; background: #eef3ff; }
    .inline-form { display: inline; }
    .inline-form select, .inline-form button { width: auto; margin: 0 4px 0 0; padding: 6px 8px; }
    .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
    .customer-search-box { position: relative; }
    .suggestions {
      position: absolute;
      top: 100%;
      left: 0;
      right: 0;
      background: white;
      border: 1px solid #dcdcdc;
      border-radius: 10px;
      box-shadow: 0 8px 20px rgba(0,0,0,.08);
      z-index: 20;
      max-height: 220px;
      overflow-y: auto;
      display: none;
    }
    .suggestion-item {
      padding: 10px;
      cursor: pointer;
      border-bottom: 1px solid #efefef;
    }
    .suggestion-item:last-child { border-bottom: none; }
    .suggestion-item:hover { background: #f6f9ff; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; }
    .file-list { margin: 0; padding-left: 18px; }
    .file-list li { margin-bottom: 6px; }
    .upload-box { min-width: 220px; }

    .invoice-wrap { background: white; padding: 24px; border-radius: 14px; box-shadow: 0 2px 8px rgba(0,0,0,.08); }
    .invoice-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }
    .invoice-title { font-size: 34px; font-weight: bold; margin: 0 0 10px 0; }
    .invoice-meta { min-width: 280px; }
    .invoice-meta table { margin-top: 0; }
    .invoice-meta td { border: none; padding: 4px 8px; }
    .billto-box { margin-top: 18px; margin-bottom: 18px; }
    .invoice-table th, .invoice-table td { font-size: 14px; }
    .invoice-total { margin-top: 12px; display: flex; justify-content: flex-end; }
    .invoice-total-box { width: 260px; }
    .invoice-total-box table td { padding: 8px 10px; }
    .right { text-align: right; }
    .nowrap { white-space: nowrap; }

    .action-links {
      display: flex;
      gap: 12px;
      justify-content: flex-end;
      margin-bottom: 12px;
    }

    .action-links a {
      padding: 8px 12px;
      background: #eef3ff;
      border-radius: 8px;
      font-size: 14px;
    }

    .customer-table { table-layout: auto; }
    .customer-table input, .customer-table textarea {
      width: 100%;
      min-width: 160px;
      font-size: 14px;
    }
    .customer-table textarea {
      min-height: 70px;
      resize: vertical;
      white-space: pre-wrap;
    }
    .customer-list-card { overflow-x: auto; }

    .invoiced-load td { background: #eeeeee !important; color: #666; }
    .invoiced-badge { display: inline-block; padding: 6px 10px; border-radius: 8px; background: #d9ead3; color: #274e13; font-weight: bold; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/">Dashboard</a>
    <a href="/customers">Customers</a>
    <a href="/drivers">Drivers</a>
    <a href="/trucks">Trucks</a>
    <a href="/loads">Loads</a>
    <a href="/invoices">Invoices</a>
    <a href="/invoice-archive">Invoice Archive</a>
    <a href="/driver-loads">Driver Load Report</a>
  </div>
  <hr>
  {{ body|safe }}
</body>
</html>
"""


def db():
    if USE_POSTGRES:
        if psycopg2 is None:
            raise RuntimeError("PostgreSQL mode requires: pip install psycopg2-binary")
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_sql(sql):
    """Make the app's SQLite-style queries work with PostgreSQL too."""
    if not USE_POSTGRES:
        return sql
    sql = sql.replace("?", "%s")
    sql = sql.replace("invoice_no GLOB '[0-9]*'", "invoice_no ~ '^[0-9]+$'")
    return sql


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def html_attr(value):
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
        .replace("\n", " ")
    )


def today_ymd():
    return datetime.now().strftime("%Y-%m-%d")


def plus_30_days_ymd():
    return (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")


def clean_zip(value):
    if value is None:
        return ""
    digits = "".join(ch for ch in str(value).strip() if ch.isdigit())
    return digits[:5] if len(digits) >= 5 else ""


def zip_lat_lon(zip_code):
    zip_code = clean_zip(zip_code)
    if not zip_code or pgeocode is None:
        return None
    nomi = pgeocode.Nominatim("us")
    row = nomi.query_postal_code(zip_code)
    try:
        lat = float(row.latitude)
        lon = float(row.longitude)
    except Exception:
        return None
    if math.isnan(lat) or math.isnan(lon):
        return None
    return lat, lon


def straight_line_miles_between_zips(zip_a, zip_b):
    point_a = zip_lat_lon(zip_a)
    point_b = zip_lat_lon(zip_b)
    if not point_a or not point_b:
        return None

    lat1, lon1 = point_a
    lat2, lon2 = point_b
    radius_miles = 3958.8

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_miles * c


def driving_miles_between_zips(zip_a, zip_b):
    """Return real road miles using the free public OSRM routing service."""
    point_a = zip_lat_lon(zip_a)
    point_b = zip_lat_lon(zip_b)
    if not point_a or not point_b or requests is None:
        return None

    lat1, lon1 = point_a
    lat2, lon2 = point_b
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{lon1},{lat1};{lon2},{lat2}?overview=false"
    )

    try:
        response = requests.get(url, timeout=8)
        response.raise_for_status()
        data = response.json()
        meters = data["routes"][0]["distance"]
        return meters * 0.000621371
    except Exception:
        return None


def total_route_miles(origin_zip, stop_1_zip, stop_2_zip, destination_zip):
    zips = [
        clean_zip(origin_zip),
        clean_zip(stop_1_zip),
        clean_zip(stop_2_zip),
        clean_zip(destination_zip),
    ]
    zips = [z for z in zips if z]
    if len(zips) < 2:
        return None

    total = 0.0
    for start_zip, end_zip in zip(zips, zips[1:]):
        segment = driving_miles_between_zips(start_zip, end_zip)
        if segment is None:
            return None
        total += segment
    return total

def format_miles(value):
    if value is None:
        if pgeocode is None:
            return "Install pgeocode"
        if requests is None:
            return "Install requests"
        return "Miles unavailable"
    return f"{value:,.1f}"


def unique_file_path(folder, filename):
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(folder, filename)
    counter = 1

    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base}_{counter}{ext}")
        counter += 1

    return candidate


def init_db_postgres():
    """Create/update the shared PostgreSQL schema used by this app."""
    with closing(db()) as conn:
        cur = conn.cursor()
        statements = [
            """
            CREATE TABLE IF NOT EXISTS customers (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                address TEXT,
                phone TEXT,
                fax TEXT,
                email TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS drivers (
                id SERIAL PRIMARY KEY,
                name TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trucks (
                id SERIAL PRIMARY KEY,
                unit_no TEXT,
                trailer_no TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trailers (
                id SERIAL PRIMARY KEY,
                trailer_no TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS loads (
                id SERIAL PRIMARY KEY,
                load_no TEXT,
                broker_load_no TEXT,
                load_trailer_no TEXT,
                optional_stop_1 TEXT,
                optional_stop_1_zip TEXT,
                optional_stop_2 TEXT,
                optional_stop_2_zip TEXT,
                pickup_location_name TEXT,
                pickup_address TEXT,
                origin TEXT,
                origin_zip TEXT,
                delivery_location_name TEXT,
                delivery_address TEXT,
                destination TEXT,
                destination_zip TEXT,
                pickup_date TEXT,
                pickup_time TEXT,
                delivery_date TEXT,
                delivery_time TEXT,
                rate NUMERIC,
                status TEXT,
                driver_id INTEGER,
                truck_id INTEGER,
                trailer_id INTEGER,
                customer_id INTEGER
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS load_files (
                id SERIAL PRIMARY KEY,
                load_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS driver_files (
                id SERIAL PRIMARY KEY,
                driver_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS truck_files (
                id SERIAL PRIMARY KEY,
                truck_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trailer_files (
                id SERIAL PRIMARY KEY,
                trailer_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                invoice_no TEXT,
                invoice_date TEXT,
                terms TEXT,
                due_date TEXT,
                customer_id INTEGER,
                bill_to_name TEXT,
                bill_to_address TEXT,
                load_id INTEGER,
                line_date TEXT,
                truck_no TEXT,
                load_no TEXT,
                origin_text TEXT,
                destination_text TEXT,
                quantity NUMERIC,
                rate NUMERIC,
                amount NUMERIC,
                total NUMERIC,
                notes TEXT,
                created_at TEXT,
                is_hidden INTEGER DEFAULT 0
            )
            """,
        ]
        for statement in statements:
            cur.execute(statement)

        # Safe upgrades for older PostgreSQL copies of the database.
        alter_statements = [
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS origin_zip TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS destination_zip TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS customer_id INTEGER",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS broker_load_no TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS pickup_location_name TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS pickup_address TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS delivery_location_name TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS delivery_address TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS pickup_time TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS delivery_time TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS load_trailer_no TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS optional_stop_1 TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS optional_stop_1_zip TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS optional_stop_2 TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS optional_stop_2_zip TEXT",
            "ALTER TABLE loads ADD COLUMN IF NOT EXISTS trailer_id INTEGER",
            "ALTER TABLE trucks ADD COLUMN IF NOT EXISTS trailer_no TEXT",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS is_hidden INTEGER DEFAULT 0",
        ]
        for statement in alter_statements:
            cur.execute(statement)

        # Move trailer numbers from old fields into the trailer table.
        cur.execute("""
            INSERT INTO trailers (trailer_no)
            SELECT DISTINCT TRIM(trailer_no)
            FROM trucks
            WHERE trailer_no IS NOT NULL
              AND TRIM(trailer_no) != ''
              AND TRIM(trailer_no) NOT IN (SELECT TRIM(trailer_no) FROM trailers WHERE trailer_no IS NOT NULL)
        """)
        cur.execute("""
            INSERT INTO trailers (trailer_no)
            SELECT DISTINCT TRIM(load_trailer_no)
            FROM loads
            WHERE load_trailer_no IS NOT NULL
              AND TRIM(load_trailer_no) != ''
              AND TRIM(load_trailer_no) NOT IN (SELECT TRIM(trailer_no) FROM trailers WHERE trailer_no IS NOT NULL)
        """)
        cur.execute("""
            UPDATE loads
            SET trailer_id = (
                SELECT tr.id
                FROM trailers tr
                WHERE TRIM(tr.trailer_no) = TRIM(loads.load_trailer_no)
                LIMIT 1
            )
            WHERE trailer_id IS NULL
              AND load_trailer_no IS NOT NULL
              AND TRIM(load_trailer_no) != ''
        """)
        conn.commit()


def init_db():
    if USE_POSTGRES:
        init_db_postgres()
        return

    with closing(db()) as conn:
        cur = conn.cursor()

        # Rename old clients table if needed
        old_clients_exists = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='clients'"
        ).fetchone()
        customers_exists = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='customers'"
        ).fetchone()

        if old_clients_exists and not customers_exists:
            cur.execute("ALTER TABLE clients RENAME TO customers")

        cur.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT,
            phone TEXT,
            fax TEXT,
            email TEXT
        );

        CREATE TABLE IF NOT EXISTS drivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT
        );

        CREATE TABLE IF NOT EXISTS trucks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_no TEXT,
            trailer_no TEXT
        );

        CREATE TABLE IF NOT EXISTS trailers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trailer_no TEXT
        );

        CREATE TABLE IF NOT EXISTS loads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            load_no TEXT,
            broker_load_no TEXT,
            load_trailer_no TEXT,
            optional_stop_1 TEXT,
            optional_stop_1_zip TEXT,
            optional_stop_2 TEXT,
            optional_stop_2_zip TEXT,
            pickup_location_name TEXT,
            pickup_address TEXT,
            origin TEXT,
            origin_zip TEXT,
            delivery_location_name TEXT,
            delivery_address TEXT,
            destination TEXT,
            destination_zip TEXT,
            pickup_date TEXT,
            pickup_time TEXT,
            delivery_date TEXT,
            delivery_time TEXT,
            rate REAL,
            status TEXT,
            driver_id INTEGER,
            truck_id INTEGER,
            trailer_id INTEGER,
            customer_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS load_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            load_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY(load_id) REFERENCES loads(id)
        );

        CREATE TABLE IF NOT EXISTS driver_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY(driver_id) REFERENCES drivers(id)
        );

        CREATE TABLE IF NOT EXISTS truck_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            truck_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY(truck_id) REFERENCES trucks(id)
        );

        CREATE TABLE IF NOT EXISTS trailer_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trailer_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY(trailer_id) REFERENCES trailers(id)
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_no TEXT,
            invoice_date TEXT,
            terms TEXT,
            due_date TEXT,
            customer_id INTEGER,
            bill_to_name TEXT,
            bill_to_address TEXT,
            load_id INTEGER,
            line_date TEXT,
            truck_no TEXT,
            load_no TEXT,
            origin_text TEXT,
            destination_text TEXT,
            quantity REAL,
            rate REAL,
            amount REAL,
            total REAL,
            notes TEXT,
            created_at TEXT,
            FOREIGN KEY(customer_id) REFERENCES customers(id),
            FOREIGN KEY(load_id) REFERENCES loads(id)
        );
        """)

        load_columns = [row["name"] for row in cur.execute("PRAGMA table_info(loads)").fetchall()]
        if "origin_zip" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN origin_zip TEXT")
        if "destination_zip" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN destination_zip TEXT")
        if "customer_id" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN customer_id INTEGER")
        if "broker_load_no" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN broker_load_no TEXT")
        if "pickup_location_name" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN pickup_location_name TEXT")
        if "pickup_address" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN pickup_address TEXT")
        if "delivery_location_name" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN delivery_location_name TEXT")
        if "delivery_address" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN delivery_address TEXT")
        if "pickup_time" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN pickup_time TEXT")
        if "delivery_time" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN delivery_time TEXT")
        if "load_trailer_no" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN load_trailer_no TEXT")
        if "optional_stop_1" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN optional_stop_1 TEXT")
        if "optional_stop_1_zip" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN optional_stop_1_zip TEXT")
        if "optional_stop_2" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN optional_stop_2 TEXT")
        if "optional_stop_2_zip" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN optional_stop_2_zip TEXT")
        if "trailer_id" not in load_columns:
            cur.execute("ALTER TABLE loads ADD COLUMN trailer_id INTEGER")

        truck_columns = [row["name"] for row in cur.execute("PRAGMA table_info(trucks)").fetchall()]
        if "trailer_no" not in truck_columns:
            cur.execute("ALTER TABLE trucks ADD COLUMN trailer_no TEXT")

        # Move old trailer numbers into the separate trailers table
        cur.execute("""
            INSERT INTO trailers (trailer_no)
            SELECT DISTINCT TRIM(trailer_no)
            FROM trucks
            WHERE trailer_no IS NOT NULL
              AND TRIM(trailer_no) != ''
              AND TRIM(trailer_no) NOT IN (SELECT TRIM(trailer_no) FROM trailers WHERE trailer_no IS NOT NULL)
        """)
        cur.execute("""
            INSERT INTO trailers (trailer_no)
            SELECT DISTINCT TRIM(load_trailer_no)
            FROM loads
            WHERE load_trailer_no IS NOT NULL
              AND TRIM(load_trailer_no) != ''
              AND TRIM(load_trailer_no) NOT IN (SELECT TRIM(trailer_no) FROM trailers WHERE trailer_no IS NOT NULL)
        """)
        cur.execute("""
            UPDATE loads
            SET trailer_id = (
                SELECT tr.id
                FROM trailers tr
                WHERE TRIM(tr.trailer_no) = TRIM(loads.load_trailer_no)
                LIMIT 1
            )
            WHERE trailer_id IS NULL
              AND load_trailer_no IS NOT NULL
              AND TRIM(load_trailer_no) != ''
        """)

        # Copy old client_id -> customer_id if old column exists
        load_columns = [row["name"] for row in cur.execute("PRAGMA table_info(loads)").fetchall()]
        if "client_id" in load_columns and "customer_id" in load_columns:
            cur.execute("""
                UPDATE loads
                SET customer_id = client_id
                WHERE customer_id IS NULL AND client_id IS NOT NULL
            """)

        invoice_columns = [row["name"] for row in cur.execute("PRAGMA table_info(invoices)").fetchall()]
        invoice_needed = {
            "invoice_no": "TEXT",
            "invoice_date": "TEXT",
            "terms": "TEXT",
            "due_date": "TEXT",
            "customer_id": "INTEGER",
            "bill_to_name": "TEXT",
            "bill_to_address": "TEXT",
            "load_id": "INTEGER",
            "line_date": "TEXT",
            "truck_no": "TEXT",
            "load_no": "TEXT",
            "origin_text": "TEXT",
            "destination_text": "TEXT",
            "quantity": "REAL",
            "rate": "REAL",
            "amount": "REAL",
            "total": "REAL",
            "notes": "TEXT",
            "created_at": "TEXT",
            "is_hidden": "INTEGER DEFAULT 0",
        }

        for col_name, col_type in invoice_needed.items():
            if col_name not in invoice_columns:
                cur.execute(f"ALTER TABLE invoices ADD COLUMN {col_name} {col_type}")

        # Rebuild invoices table if old schema has load_id as NOT NULL
        invoice_info = cur.execute("PRAGMA table_info(invoices)").fetchall()
        load_id_col = next((col for col in invoice_info if col["name"] == "load_id"), None)

        if load_id_col and load_id_col["notnull"] == 1:
            cur.executescript("""
            ALTER TABLE invoices RENAME TO invoices_old;

            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_no TEXT,
                invoice_date TEXT,
                terms TEXT,
                due_date TEXT,
                customer_id INTEGER,
                bill_to_name TEXT,
                bill_to_address TEXT,
                load_id INTEGER,
                line_date TEXT,
                truck_no TEXT,
                load_no TEXT,
                origin_text TEXT,
                destination_text TEXT,
                quantity REAL,
                rate REAL,
                amount REAL,
                total REAL,
                notes TEXT,
                created_at TEXT,
                is_hidden INTEGER DEFAULT 0,
                FOREIGN KEY(customer_id) REFERENCES customers(id),
                FOREIGN KEY(load_id) REFERENCES loads(id)
            );

            INSERT INTO invoices (
                id, invoice_no, invoice_date, terms, due_date,
                customer_id, bill_to_name, bill_to_address,
                load_id, line_date, truck_no, load_no,
                origin_text, destination_text, quantity, rate,
                amount, total, notes, created_at, is_hidden
            )
            SELECT
                id, invoice_no, invoice_date, terms, due_date,
                customer_id, bill_to_name, bill_to_address,
                load_id, line_date, truck_no, load_no,
                origin_text, destination_text, quantity, rate,
                amount, total, notes, created_at, COALESCE(is_hidden, 0)
            FROM invoices_old;

            DROP TABLE invoices_old;
            """)

        conn.commit()


def query(sql, params=()):
    with closing(db()) as conn:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(normalize_sql(sql), params)
            return cur.fetchall()
        return conn.execute(sql, params).fetchall()


def query_one(sql, params=()):
    with closing(db()) as conn:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(normalize_sql(sql), params)
            return cur.fetchone()
        return conn.execute(sql, params).fetchone()


def execute(sql, params=()):
    with closing(db()) as conn:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(normalize_sql(sql), params)
        else:
            conn.execute(sql, params)
        conn.commit()


def page(body):
    return render_template_string(BASE_HTML, body=body)


def selected(value, current):
    return "selected" if str(value) == str(current) else ""


@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def pdf_wrap_lines(text, max_chars=38, max_lines=3):
    """Return wrapped lines for drawing long pickup/delivery locations in PDFs."""
    if not text:
        return [""]
    wrapped = textwrap.wrap(str(text), width=max_chars)
    return wrapped[:max_lines] if wrapped else [""]


@app.route("/")
def dashboard():
    load_count = query_one("SELECT COUNT(*) AS count FROM loads")["count"]
    driver_count = query_one("SELECT COUNT(*) AS count FROM drivers")["count"]
    truck_count = query_one("SELECT COUNT(*) AS count FROM trucks")["count"]
    customer_count = query_one("SELECT COUNT(*) AS count FROM customers")["count"]
    delivered_count = query_one("SELECT COUNT(*) AS count FROM loads WHERE status='Delivered'")["count"]
    file_count = query_one("SELECT COUNT(*) AS count FROM load_files")["count"]
    invoice_count = query_one("SELECT COUNT(*) AS count FROM invoices")["count"]

    return page(f"""
    <div class='stats'>
      <div class='card'><h2>{customer_count}</h2><div>Customers</div></div>
      <div class='card'><h2>{driver_count}</h2><div>Drivers</div></div>
      <div class='card'><h2>{truck_count}</h2><div>Trucks</div></div>
      <div class='card'><h2>{load_count}</h2><div>Loads</div></div>
      <div class='card'><h2>{delivered_count}</h2><div>Delivered Loads</div></div>
      <div class='card'><h2>{file_count}</h2><div>Load Files</div></div>
      <div class='card'><h2>{invoice_count}</h2><div>Invoices</div></div>
    </div>
    <div class='card' style='margin-top:16px;'>
      <h1>Dashboard</h1>
      <p class='muted'>Customers, ZIP codes, live customer lookup, driver load reporting, file uploads by load, and invoice entry are ready.</p>
    </div>
    """)


@app.route("/customers", methods=["GET", "POST"])
def customers():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        address = request.form.get("address", "").strip()
        phone = request.form.get("phone", "").strip()
        fax = request.form.get("fax", "").strip()
        email = request.form.get("email", "").strip()

        if name:
            execute(
                "INSERT INTO customers (name, address, phone, fax, email) VALUES (?, ?, ?, ?, ?)",
                (name, address, phone, fax, email),
            )
        return redirect("/customers")

    rows = query("SELECT * FROM customers ORDER BY name")
    tr = ""
    for r in rows:
        tr += f"""
        <tr>
          <form method='post' action='/customers/{r['id']}/edit'>
            <td><textarea name='name' required>{html_attr(r['name'])}</textarea></td>
            <td><textarea name='address'>{html_attr(r['address'])}</textarea></td>
            <td><textarea name='phone'>{html_attr(r['phone'])}</textarea></td>
            <td><textarea name='fax'>{html_attr(r['fax'])}</textarea></td>
            <td><textarea name='email'>{html_attr(r['email'])}</textarea></td>
            <td><button type='submit'>Save Edit</button></td>
          </form>
        </tr>
        """

    if not tr:
        tr = "<tr><td colspan='6'>No customers yet.</td></tr>"

    return page(f"""
    <div class='grid'>
      <div class='card'>
        <h1>Customers</h1>
        <form method='post'>
          <input name='name' placeholder='Customer name' required>
          <input name='address' placeholder='Address'>
          <input name='phone' placeholder='Phone number'>
          <input name='fax' placeholder='Fax number'>
          <input name='email' placeholder='Email'>
          <button>Add Customer</button>
        </form>
      </div>
      <div class='card customer-list-card'>
        <h2>Customer List</h2>
        <p class='muted'>Customer fields are expanded so you can read the full name, address, phone, fax, and email before saving edits.</p>
        <table class='customer-table'>
          <tr><th>Name</th><th>Address</th><th>Phone</th><th>Fax</th><th>Email</th><th>Actions</th></tr>
          {tr}
        </table>
      </div>
    </div>
    """)


@app.route("/customers/<int:customer_id>/edit", methods=["POST"])
def edit_customer(customer_id):
    name = request.form.get("name", "").strip()
    address = request.form.get("address", "").strip()
    phone = request.form.get("phone", "").strip()
    fax = request.form.get("fax", "").strip()
    email = request.form.get("email", "").strip()

    if name:
        execute(
            "UPDATE customers SET name = ?, address = ?, phone = ?, fax = ?, email = ? WHERE id = ?",
            (name, address, phone, fax, email, customer_id),
        )

    return redirect("/customers")


@app.route("/customers/search")
def search_customers():
    term = request.args.get("q", "").strip()
    if not term:
        return jsonify([])

    like_term = f"%{term}%"
    rows = query(
        "SELECT id, name, address, phone, fax, email FROM customers WHERE name LIKE ? ORDER BY name LIMIT 10",
        (like_term,),
    )
    return jsonify([
        {
            "id": r["id"],
            "name": r["name"],
            "address": r["address"] or "",
            "phone": r["phone"] or "",
            "fax": r["fax"] or "",
            "email": r["email"] or "",
        }
        for r in rows
    ])


@app.route("/drivers", methods=["GET", "POST"])
def drivers():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            execute("INSERT INTO drivers (name) VALUES (?)", (name,))
        return redirect("/drivers")

    rows = query("SELECT * FROM drivers ORDER BY name")

    files_by_driver = {}
    driver_file_rows = query("SELECT * FROM driver_files ORDER BY uploaded_at DESC, id DESC")
    for row in driver_file_rows:
        files_by_driver.setdefault(row["driver_id"], []).append(row)

    tr = ""
    for r in rows:
        file_items = files_by_driver.get(r["id"], [])
        file_html = "<div class='muted'>No files</div>"
        if file_items:
            file_html = "<ul class='file-list'>" + "".join(
                f"""
                <li>
                  <a href='/uploads/{f['stored_name']}' target='_blank'>{f['original_name']}</a>
                  <form method='post' action='/driver-files/{f['id']}/delete' style='display:inline;' onsubmit="return confirm('Remove this driver file?');">
                    <button type='submit' style='width:auto; padding:4px 8px; background:#ffdddd;'>Remove</button>
                  </form>
                </li>
                """
                for f in file_items
            ) + "</ul>"

        tr += f"""
        <tr>
          <td>
            <form method='post' action='/drivers/{r['id']}/edit' style='display:flex; gap:6px;'>
              <input name='name' value='{html_attr(r['name'])}' required>
              <button type='submit'>Save Edit</button>
            </form>
          </td>
          <td>
            <form method='post' action='/drivers/{r['id']}/upload' enctype='multipart/form-data'>
              <input type='file' name='file' required>
              <button>Upload Driver File</button>
            </form>
            {file_html}
          </td>
        </tr>
        """

    if not tr:
        tr = "<tr><td colspan='2'>No drivers yet.</td></tr>"

    return page(f"""
    <div class='grid'>
      <div class='card'>
        <h1>Drivers</h1>
        <form method='post'>
          <input name='name' placeholder='Driver name' required>
          <button>Add</button>
        </form>
      </div>
      <div class='card'>
        <h2>Driver List</h2>
        <table>
          <tr><th>Name</th><th>Files</th></tr>
          {tr}
        </table>
      </div>
    </div>
    """)


@app.route("/drivers/<int:driver_id>/edit", methods=["POST"])
def edit_driver(driver_id):
    name = request.form.get("name", "").strip()
    if name:
        execute("UPDATE drivers SET name = ? WHERE id = ?", (name, driver_id))
    return redirect("/drivers")


@app.route("/trucks", methods=["GET", "POST"])
def trucks():
    if request.method == "POST":
        unit_no = request.form.get("unit_no", "").strip()
        if unit_no:
            execute("INSERT INTO trucks (unit_no) VALUES (?)", (unit_no,))
        return redirect("/trucks")

    truck_rows = query("SELECT * FROM trucks ORDER BY unit_no")
    trailer_rows = query("SELECT * FROM trailers ORDER BY trailer_no")

    files_by_truck = {}
    truck_file_rows = query("SELECT * FROM truck_files ORDER BY uploaded_at DESC, id DESC")
    for row in truck_file_rows:
        files_by_truck.setdefault(row["truck_id"], []).append(row)

    files_by_trailer = {}
    trailer_file_rows = query("SELECT * FROM trailer_files ORDER BY uploaded_at DESC, id DESC")
    for row in trailer_file_rows:
        files_by_trailer.setdefault(row["trailer_id"], []).append(row)

    truck_tr = ""
    for r in truck_rows:
        file_items = files_by_truck.get(r["id"], [])
        file_html = "<div class='muted'>No files</div>"
        if file_items:
            file_html = "<ul class='file-list'>" + "".join(
                f"""
                <li>
                  <a href='/uploads/{f['stored_name']}' target='_blank'>{f['original_name']}</a>
                  <form method='post' action='/truck-files/{f['id']}/delete' style='display:inline;' onsubmit="return confirm('Remove this truck file?');">
                    <button type='submit' style='width:auto; padding:4px 8px; background:#ffdddd;'>Remove</button>
                  </form>
                </li>
                """
                for f in file_items
            ) + "</ul>"

        truck_tr += f"""
        <tr>
          <td>
            <form method='post' action='/trucks/{r['id']}/edit' style='display:flex; gap:6px; align-items:center;'>
              <input name='unit_no' value='{html_attr(r['unit_no'])}' placeholder='Truck / Unit #' style='width:160px;'>
              <button type='submit'>Save Edit</button>
            </form>
          </td>
          <td>
            <form method='post' action='/trucks/{r['id']}/upload' enctype='multipart/form-data'>
              <input type='file' name='file' required>
              <button>Upload Truck File</button>
            </form>
            {file_html}
          </td>
          <td>
            <form method='post' action='/trucks/{r['id']}/delete' style='display:inline;' onsubmit="return confirm('Delete this truck?');">
              <button type='submit' style='background:#ffdddd;'>Delete</button>
            </form>
          </td>
        </tr>
        """

    if not truck_tr:
        truck_tr = "<tr><td colspan='3'>No trucks yet.</td></tr>"

    trailer_tr = ""
    for r in trailer_rows:
        file_items = files_by_trailer.get(r["id"], [])
        file_html = "<div class='muted'>No files</div>"
        if file_items:
            file_html = "<ul class='file-list'>" + "".join(
                f"""
                <li>
                  <a href='/uploads/{f['stored_name']}' target='_blank'>{f['original_name']}</a>
                  <form method='post' action='/trailer-files/{f['id']}/delete' style='display:inline;' onsubmit="return confirm('Remove this trailer file?');">
                    <button type='submit' style='width:auto; padding:4px 8px; background:#ffdddd;'>Remove</button>
                  </form>
                </li>
                """
                for f in file_items
            ) + "</ul>"

        trailer_tr += f"""
        <tr>
          <td>
            <form method='post' action='/trailers/{r['id']}/edit' style='display:flex; gap:6px; align-items:center;'>
              <input name='trailer_no' value='{html_attr(r['trailer_no'])}' placeholder='Trailer #' style='width:160px;'>
              <button type='submit'>Save Edit</button>
            </form>
          </td>
          <td>
            <form method='post' action='/trailers/{r['id']}/upload' enctype='multipart/form-data'>
              <input type='file' name='file' required>
              <button>Upload Trailer File</button>
            </form>
            {file_html}
          </td>
          <td>
            <form method='post' action='/trailers/{r['id']}/delete' style='display:inline;' onsubmit="return confirm('Delete this trailer?');">
              <button type='submit' style='background:#ffdddd;'>Delete</button>
            </form>
          </td>
        </tr>
        """

    if not trailer_tr:
        trailer_tr = "<tr><td colspan='3'>No trailers yet.</td></tr>"

    return page(f"""
    <div class='grid'>
      <div class='card'>
        <h1>Trucks</h1>
        <form method='post'>
          <input name='unit_no' placeholder='Truck / Unit #' required>
          <button>Add Truck</button>
        </form>
      </div>
      <div class='card'>
        <h1>Trailers</h1>
        <form method='post' action='/trailers/add'>
          <input name='trailer_no' placeholder='Trailer #' required>
          <button>Add Trailer</button>
        </form>
      </div>
      <div class='card'>
        <h2>Truck List</h2>
        <table>
          <tr><th>Truck / Unit</th><th>Files</th><th>Actions</th></tr>
          {truck_tr}
        </table>
      </div>
      <div class='card'>
        <h2>Trailer List</h2>
        <table>
          <tr><th>Trailer #</th><th>Files</th><th>Actions</th></tr>
          {trailer_tr}
        </table>
      </div>
    </div>
    """)


@app.route("/trucks/<int:truck_id>/edit", methods=["POST"])
def edit_truck(truck_id):
    unit_no = request.form.get("unit_no", "").strip()

    if not unit_no:
        return redirect("/trucks")

    execute("UPDATE trucks SET unit_no = ? WHERE id = ?", (unit_no, truck_id))
    return redirect("/trucks")


@app.route("/trucks/<int:truck_id>/delete", methods=["POST"])
def delete_truck(truck_id):
    execute("DELETE FROM trucks WHERE id = ?", (truck_id,))
    return redirect("/trucks")


@app.route("/trailers/add", methods=["POST"])
def add_trailer():
    trailer_no = request.form.get("trailer_no", "").strip()
    if trailer_no:
        execute("INSERT INTO trailers (trailer_no) VALUES (?)", (trailer_no,))
    return redirect("/trucks")


@app.route("/trailers/<int:trailer_id>/edit", methods=["POST"])
def edit_trailer(trailer_id):
    trailer_no = request.form.get("trailer_no", "").strip()
    if not trailer_no:
        return redirect("/trucks")
    execute("UPDATE trailers SET trailer_no = ? WHERE id = ?", (trailer_no, trailer_id))
    return redirect("/trucks")


@app.route("/trailers/<int:trailer_id>/delete", methods=["POST"])
def delete_trailer(trailer_id):
    execute("DELETE FROM trailers WHERE id = ?", (trailer_id,))
    execute("UPDATE loads SET trailer_id = NULL WHERE trailer_id = ?", (trailer_id,))
    return redirect("/trucks")


@app.route("/loads", methods=["GET", "POST"])

def loads():
    if request.method == "POST":
        execute(
            """
            INSERT INTO loads (
                load_no, broker_load_no, trailer_id, optional_stop_1, optional_stop_1_zip, optional_stop_2, optional_stop_2_zip,
                pickup_location_name, pickup_address, origin, origin_zip,
                delivery_location_name, delivery_address, destination, destination_zip,
                pickup_date, pickup_time, delivery_date, delivery_time, rate, status, driver_id, truck_id, customer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.form.get("load_no"),
                request.form.get("broker_load_no"),
                request.form.get("trailer_id") or None,
                request.form.get("optional_stop_1"),
                request.form.get("optional_stop_1_zip"),
                request.form.get("optional_stop_2"),
                request.form.get("optional_stop_2_zip"),
                request.form.get("pickup_location_name"),
                request.form.get("pickup_address"),
                request.form.get("origin"),
                request.form.get("origin_zip"),
                request.form.get("delivery_location_name"),
                request.form.get("delivery_address"),
                request.form.get("destination"),
                request.form.get("destination_zip"),
                request.form.get("pickup_date") or None,
                request.form.get("pickup_time") or None,
                request.form.get("delivery_date") or None,
                request.form.get("delivery_time") or None,
                request.form.get("rate") or 0,
                request.form.get("status"),
                request.form.get("driver_id") or None,
                request.form.get("truck_id") or None,
                request.form.get("customer_id") or None,
            ),
        )
        return redirect("/loads")

    drivers = query("SELECT * FROM drivers ORDER BY name")
    trucks = query("SELECT * FROM trucks ORDER BY unit_no")
    trailers = query("SELECT * FROM trailers ORDER BY trailer_no")
    loads_list = query(
        """
        SELECT l.*, d.name AS driver_name, t.unit_no AS truck_unit, tr.trailer_no AS trailer_no, c.name AS customer_name,
               (SELECT COUNT(*) FROM invoices i WHERE i.load_id = l.id) AS invoice_count
        FROM loads l
        LEFT JOIN drivers d ON d.id = l.driver_id
        LEFT JOIN trucks t ON t.id = l.truck_id
        LEFT JOIN trailers tr ON tr.id = l.trailer_id
        LEFT JOIN customers c ON c.id = l.customer_id
        ORDER BY l.id DESC
        """
    )

    files_by_load = {}
    file_rows = query("SELECT * FROM load_files ORDER BY uploaded_at DESC, id DESC")
    for row in file_rows:
        files_by_load.setdefault(row["load_id"], []).append(row)

    driver_opts = "<option value=''>Select driver</option>" + "".join(
        f"<option value='{d['id']}'>{d['name']}</option>" for d in drivers
    )
    truck_opts = "<option value=''>Select truck</option>" + "".join(
        f"<option value='{t['id']}'>{t['unit_no'] or ''}</option>"
        for t in trucks
    )
    trailer_opts = "<option value=''>Select trailer</option>" + "".join(
        f"<option value='{tr['id']}'>{tr['trailer_no'] or ''}</option>"
        for tr in trailers
    )

    tr = ""
    for l in loads_list:
        file_items = files_by_load.get(l["id"], [])
        file_html = "<div class='muted'>No files</div>"
        if file_items:
            file_html = "<ul class='file-list'>" + "".join(
                f"""
                <li>
                  <a href='/uploads/{f['stored_name']}' target='_blank'>{f['original_name']}</a>
                  <form method='post'
                        action='/load-files/{f['id']}/delete'
                        style='display:inline; margin-left:8px;'
                        onsubmit="return confirm('Remove this attached file from the load board?');">
                    <button type='submit' style='width:auto; padding:4px 8px; background:#ffdddd;'>Remove</button>
                  </form>
                </li>
                """
                for f in file_items
            ) + "</ul>"

        invoiced_class = "invoiced-load" if int(l["invoice_count"] or 0) > 0 else ""
        if int(l["invoice_count"] or 0) > 0:
            invoice_cell = "<span class='invoiced-badge'>Invoiced</span>"
        else:
            invoice_cell = f"<form method='post' action='/loads/{l['id']}/create-invoice' class='inline-form'><button>Create Invoice</button></form>"

        tr += f"""
        <tr data-load-row class='{invoiced_class}'>
          <td>{l['load_no'] or ''}</td>
          <td>{l['broker_load_no'] or ''}</td>
          <td>{l['trailer_no'] or l['load_trailer_no'] or ''}</td>
          <td>{l['customer_name'] or ''}</td>
          <td>{l['pickup_location_name'] or ''}</td>
          <td>{l['pickup_address'] or ''}</td>
          <td>{l['origin'] or ''}</td>
          <td>{l['origin_zip'] or ''}</td>
          <td>{l['pickup_date'] or ''}</td>
          <td>{l['pickup_time'] or ''}</td>
          <td>{l['delivery_location_name'] or ''}</td>
          <td>{l['delivery_address'] or ''}</td>
          <td>{l['destination'] or ''}</td>
          <td>{l['destination_zip'] or ''}</td>
          <td>{l['delivery_date'] or ''}</td>
          <td>{l['delivery_time'] or ''}</td>
          <td>${float(l['rate'] or 0):,.2f}</td>
          <td>{l['optional_stop_1'] or ''}</td>
          <td>{l['optional_stop_1_zip'] or ''}</td>
          <td>{l['optional_stop_2'] or ''}</td>
          <td>{l['optional_stop_2_zip'] or ''}</td>
          <td>{l['driver_name'] or ''}</td>
          <td>{l['truck_unit'] or ''}</td>
          <td>{l['status'] or ''}</td>
          <td>
            <form method='post' action='/update-status/{l['id']}' class='inline-form'>
              <select name='status'>
                {''.join(f"<option value='{s}' {'selected' if s == l['status'] else ''}>{s}</option>" for s in LOAD_STATUSES)}
              </select>
              <button>Update</button>
            </form>
          </td>
          <td>
            <a href='/loads/{l["id"]}/edit'>Edit</a>
          </td>
          <td>
            {invoice_cell}
          </td>
          <td>
            <div class='upload-box'>
              <form method='post' action='/loads/{l['id']}/upload' enctype='multipart/form-data'>
                <input type='file' name='file' required>
                <button>Upload</button>
              </form>
              {file_html}
            </div>
          </td>
        </tr>
        """

    if not tr:
        tr = "<tr><td colspan='28'>No loads yet.</td></tr>"

    return page(f"""
    <div class='grid'>
      <div class='card'>
        <h1>Loads</h1>
        <form method='post' id='load-form'>
          <div class='form-grid'>
            <input name='load_no' placeholder='Load # / Internal Load #' required>
            <input name='broker_load_no' placeholder='Broker Load #'>
            <div class='customer-search-box'>
              <input type='text' id='customer_search' placeholder='Type customer name' autocomplete='off'>
              <input type='hidden' name='customer_id' id='customer_id'>
              <div id='customer_suggestions' class='suggestions'></div>
            </div>
            <input name='pickup_location_name' placeholder='Pickup location name'>
            <textarea name='pickup_address' placeholder='Pickup address' rows='2'></textarea>
            <input name='origin' placeholder='Pickup city / state' required>
            <input name='origin_zip' placeholder='Pickup ZIP code' required>
            <input type='date' name='pickup_date'>
            <input type='time' name='pickup_time' placeholder='Pickup time'>
            <input name='delivery_location_name' placeholder='Delivery location name'>
            <textarea name='delivery_address' placeholder='Delivery address' rows='2'></textarea>
            <input name='destination' placeholder='Delivery city / state' required>
            <input name='destination_zip' placeholder='Delivery ZIP code' required>
            <input type='date' name='delivery_date'>
            <input type='time' name='delivery_time' placeholder='Delivery time'>
            <input name='optional_stop_1' placeholder='Optional Stop 1'>
            <input name='optional_stop_1_zip' placeholder='Optional Stop 1 ZIP'>
            <input name='optional_stop_2' placeholder='Optional Stop 2'>
            <input name='optional_stop_2_zip' placeholder='Optional Stop 2 ZIP'>
            <input name='rate' placeholder='Rate'>
            <select name='status'>
              {''.join(f"<option value='{s}'>{s}</option>" for s in LOAD_STATUSES)}
            </select>
            <select name='driver_id'>{driver_opts}</select>
            <select name='truck_id'>{truck_opts}</select>
            <select name='trailer_id'>{trailer_opts}</select>
          </div>
          <button>Create</button>
          <p class='muted'>To add a customer to the load, start typing the customer name and click the match you want.</p>
        </form>
      </div>
      <div class='card'>
        <h2>Load Board</h2>
        <p class='muted'>Use the filter boxes under each heading to search the load board.</p>
        <table id='load_board_table'>
          <thead>
            <tr>
              <th>Load #</th>
              <th>Broker Load #</th>
              <th>Trailer #</th>
              <th>Customer</th>
              <th>Pickup Name</th>
              <th>Pickup Address</th>
              <th>Pickup City/State</th>
              <th>Pickup ZIP</th>
              <th>Pickup Date</th>
              <th>Pickup Time</th>
              <th>Delivery Name</th>
              <th>Delivery Address</th>
              <th>Delivery City/State</th>
              <th>Delivery ZIP</th>
              <th>Delivery Date</th>
              <th>Delivery Time</th>
              <th>Rate</th>
              <th>Optional Stop 1</th>
              <th>Stop 1 ZIP</th>
              <th>Optional Stop 2</th>
              <th>Stop 2 ZIP</th>
              <th>Driver</th>
              <th>Truck</th>
              <th>Status</th>
              <th>Change Status</th>
              <th>Actions</th>
              <th>Invoice</th>
              <th>Files</th>
            </tr>
            <tr>
              <th><input class='load-filter' data-col='0' placeholder='Search'></th>
              <th><input class='load-filter' data-col='1' placeholder='Search'></th>
              <th><input class='load-filter' data-col='2' placeholder='Search'></th>
              <th><input class='load-filter' data-col='3' placeholder='Search'></th>
              <th><input class='load-filter' data-col='4' placeholder='Search'></th>
              <th><input class='load-filter' data-col='5' placeholder='Search'></th>
              <th><input class='load-filter' data-col='6' placeholder='Search'></th>
              <th><input class='load-filter' data-col='7' placeholder='Search'></th>
              <th><input class='load-filter' data-col='8' placeholder='Search'></th>
              <th><input class='load-filter' data-col='9' placeholder='Search'></th>
              <th><input class='load-filter' data-col='10' placeholder='Search'></th>
              <th><input class='load-filter' data-col='11' placeholder='Search'></th>
              <th><input class='load-filter' data-col='12' placeholder='Search'></th>
              <th><input class='load-filter' data-col='13' placeholder='Search'></th>
              <th><input class='load-filter' data-col='14' placeholder='Search'></th>
              <th><input class='load-filter' data-col='15' placeholder='Search'></th>
              <th><input class='load-filter' data-col='16' placeholder='Search'></th>
              <th><input class='load-filter' data-col='17' placeholder='Search'></th>
              <th><input class='load-filter' data-col='18' placeholder='Search'></th>
              <th><input class='load-filter' data-col='19' placeholder='Search'></th>
              <th><input class='load-filter' data-col='20' placeholder='Search'></th>
              <th><input class='load-filter' data-col='21' placeholder='Search'></th>
              <th><input class='load-filter' data-col='22' placeholder='Search'></th>
              <th><input class='load-filter' data-col='23' placeholder='Search'></th>
              <th></th>
              <th></th>
              <th></th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {tr}
          </tbody>
        </table>
      </div>
    </div>
    <script>
      const customerSearchInput = document.getElementById('customer_search');
      const customerIdInput = document.getElementById('customer_id');
      const suggestionsBox = document.getElementById('customer_suggestions');

      function hideSuggestions() {{
        suggestionsBox.style.display = 'none';
        suggestionsBox.innerHTML = '';
      }}

      function renderSuggestions(items) {{
        if (!items.length) {{
          suggestionsBox.innerHTML = '<div class="suggestion-item">No customer found</div>';
          suggestionsBox.style.display = 'block';
          return;
        }}

        suggestionsBox.innerHTML = items.map(item => `
          <div class="suggestion-item" data-id="${{item.id}}" data-name="${{item.name}}">
            <strong>${{item.name}}</strong><br>
            <span>${{item.address || ''}}</span>
          </div>
        `).join('');
        suggestionsBox.style.display = 'block';

        document.querySelectorAll('.suggestion-item[data-id]').forEach(el => {{
          el.addEventListener('click', () => {{
            customerIdInput.value = el.dataset.id;
            customerSearchInput.value = el.dataset.name;
            hideSuggestions();
          }});
        }});
      }}

      if (customerSearchInput) {{
        customerSearchInput.addEventListener('input', async () => {{
          const q = customerSearchInput.value.trim();
          customerIdInput.value = '';

          if (q.length < 2) {{
            hideSuggestions();
            return;
          }}

          try {{
            const response = await fetch(`/customers/search?q=${{encodeURIComponent(q)}}`);
            const data = await response.json();
            renderSuggestions(data);
          }} catch (error) {{
            hideSuggestions();
          }}
        }});

        document.addEventListener('click', (event) => {{
          if (!event.target.closest('.customer-search-box')) {{
            hideSuggestions();
          }}
        }});
      }}

      function filterLoadBoard() {{
        const filters = Array.from(document.querySelectorAll('.load-filter')).map(input => {{
          return {{ col: parseInt(input.dataset.col, 10), value: input.value.trim().toLowerCase() }};
        }}).filter(f => f.value.length > 0);

        document.querySelectorAll('#load_board_table tbody tr[data-load-row]').forEach(row => {{
          const cells = row.querySelectorAll('td');
          const show = filters.every(f => (cells[f.col]?.innerText || '').toLowerCase().includes(f.value));
          row.style.display = show ? '' : 'none';
        }});
      }}

      document.querySelectorAll('.load-filter').forEach(input => {{
        input.addEventListener('input', filterLoadBoard);
      }});
    </script>
    """)


@app.route("/loads/<int:load_id>/edit", methods=["GET", "POST"])
def edit_load(load_id):
    load = query_one("SELECT * FROM loads WHERE id = ?", (load_id,))
    if not load:
        return "Load not found", 404

    if request.method == "POST":
        execute(
            """
            UPDATE loads
            SET load_no = ?, broker_load_no = ?, load_trailer_no = ?, optional_stop_1 = ?, optional_stop_1_zip = ?, optional_stop_2 = ?, optional_stop_2_zip = ?,
                pickup_location_name = ?, pickup_address = ?, origin = ?, origin_zip = ?,
                delivery_location_name = ?, delivery_address = ?, destination = ?, destination_zip = ?,
                pickup_date = ?, pickup_time = ?, delivery_date = ?, delivery_time = ?, rate = ?, status = ?,
                driver_id = ?, truck_id = ?, trailer_id = ?, customer_id = ?
            WHERE id = ?
            """,
            (
                request.form.get("load_no"),
                request.form.get("broker_load_no"),
                request.form.get("load_trailer_no"),
                request.form.get("optional_stop_1"),
                request.form.get("optional_stop_1_zip"),
                request.form.get("optional_stop_2"),
                request.form.get("optional_stop_2_zip"),
                request.form.get("pickup_location_name"),
                request.form.get("pickup_address"),
                request.form.get("origin"),
                request.form.get("origin_zip"),
                request.form.get("delivery_location_name"),
                request.form.get("delivery_address"),
                request.form.get("destination"),
                request.form.get("destination_zip"),
                request.form.get("pickup_date") or None,
                request.form.get("pickup_time") or None,
                request.form.get("delivery_date") or None,
                request.form.get("delivery_time") or None,
                request.form.get("rate") or 0,
                request.form.get("status"),
                request.form.get("driver_id") or None,
                request.form.get("truck_id") or None,
                request.form.get("trailer_id") or None,
                request.form.get("customer_id") or None,
                load_id,
            ),
        )
        return redirect("/loads")

    customers = query("SELECT * FROM customers ORDER BY name")
    drivers = query("SELECT * FROM drivers ORDER BY name")
    trucks = query("SELECT * FROM trucks ORDER BY unit_no")
    trailers = query("SELECT * FROM trailers ORDER BY trailer_no")

    customer_opts = "<option value=''>Select customer</option>" + "".join(
        f"<option value='{c['id']}' {selected(c['id'], load['customer_id'])}>{c['name']}</option>"
        for c in customers
    )
    driver_opts = "<option value=''>Select driver</option>" + "".join(
        f"<option value='{d['id']}' {selected(d['id'], load['driver_id'])}>{d['name']}</option>"
        for d in drivers
    )
    truck_opts = "<option value=''>Select truck</option>" + "".join(
        f"<option value='{t['id']}' {selected(t['id'], load['truck_id'])}>{t['unit_no'] or ''}</option>"
        for t in trucks
    )
    trailer_opts = "<option value=''>Select trailer</option>" + "".join(
        f"<option value='{tr['id']}' {selected(tr['id'], load['trailer_id'])}>{tr['trailer_no'] or ''}</option>"
        for tr in trailers
    )
    status_opts = "".join(
        f"<option value='{s}' {selected(s, load['status'])}>{s}</option>"
        for s in LOAD_STATUSES
    )

    return page(f"""
    <div class='card'>
      <h1>Edit Load</h1>
      <form method='post'>
        <div class='form-grid'>
          <input name='load_no' placeholder='Load # / Internal Load #' value='{html_attr(load['load_no'])}' required>
          <input name='broker_load_no' placeholder='Broker Load #' value='{html_attr(load['broker_load_no'])}'>
          <select name='customer_id'>{customer_opts}</select>

          <input name='pickup_location_name' placeholder='Pickup location name' value='{html_attr(load['pickup_location_name'])}'>
          <textarea name='pickup_address' placeholder='Pickup address' rows='2'>{load['pickup_address'] or ''}</textarea>
          <input name='origin' placeholder='Pickup city / state' value='{html_attr(load['origin'])}' required>
          <input name='origin_zip' placeholder='Pickup ZIP code' value='{html_attr(load['origin_zip'])}' required>
          <input type='date' name='pickup_date' value='{load['pickup_date'] or ''}'>
          <input type='time' name='pickup_time' value='{load['pickup_time'] or ''}' placeholder='Pickup time'>

          <input name='delivery_location_name' placeholder='Delivery location name' value='{html_attr(load['delivery_location_name'])}'>
          <textarea name='delivery_address' placeholder='Delivery address' rows='2'>{load['delivery_address'] or ''}</textarea>
          <input name='destination' placeholder='Delivery city / state' value='{html_attr(load['destination'])}' required>
          <input name='destination_zip' placeholder='Delivery ZIP code' value='{html_attr(load['destination_zip'])}' required>
          <input type='date' name='delivery_date' value='{load['delivery_date'] or ''}'>
          <input type='time' name='delivery_time' value='{load['delivery_time'] or ''}' placeholder='Delivery time'>
          <input name='optional_stop_1' placeholder='Optional Stop 1' value='{html_attr(load['optional_stop_1'])}'>
          <input name='optional_stop_1_zip' placeholder='Optional Stop 1 ZIP' value='{html_attr(load['optional_stop_1_zip'])}'>
          <input name='optional_stop_2' placeholder='Optional Stop 2' value='{html_attr(load['optional_stop_2'])}'>
          <input name='optional_stop_2_zip' placeholder='Optional Stop 2 ZIP' value='{html_attr(load['optional_stop_2_zip'])}'>

          <input name='rate' placeholder='Rate' value='{load['rate'] or ''}'>
          <select name='status'>{status_opts}</select>
          <select name='driver_id'>{driver_opts}</select>
          <select name='truck_id'>{truck_opts}</select>
          <select name='trailer_id'>{trailer_opts}</select>
        </div>
        <button>Save Load</button>
        <a href='/loads' style='display:inline-block; margin-left:10px;'>Cancel</a>
      </form>
    </div>
    """)


@app.route("/loads/<int:load_id>/upload", methods=["POST"])
def upload_load_file(load_id):
    uploaded_file = request.files.get("file")
    if not uploaded_file or uploaded_file.filename == "":
        return redirect("/loads")

    if not allowed_file(uploaded_file.filename):
        return redirect("/loads")

    safe_name = secure_filename(uploaded_file.filename)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    stored_name = f"{load_id}_{timestamp}_{safe_name}"
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
    uploaded_file.save(save_path)

    execute(
        "INSERT INTO load_files (load_id, original_name, stored_name, uploaded_at) VALUES (?, ?, ?, ?)",
        (load_id, uploaded_file.filename, stored_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return redirect("/loads")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/load-files/<int:file_id>/delete", methods=["POST"])
def delete_load_file(file_id):
    file_row = query_one("SELECT * FROM load_files WHERE id = ?", (file_id,))

    if file_row:
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], file_row["stored_name"])

        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError:
            pass

        execute("DELETE FROM load_files WHERE id = ?", (file_id,))

    return redirect("/loads")


@app.route("/drivers/<int:driver_id>/upload", methods=["POST"])
def upload_driver_file(driver_id):
    uploaded_file = request.files.get("file")
    if not uploaded_file or uploaded_file.filename == "":
        return redirect("/drivers")

    if not allowed_file(uploaded_file.filename):
        return redirect("/drivers")

    safe_name = secure_filename(uploaded_file.filename)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    stored_name = f"driver_{driver_id}_{timestamp}_{safe_name}"
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
    uploaded_file.save(save_path)

    execute(
        "INSERT INTO driver_files (driver_id, original_name, stored_name, uploaded_at) VALUES (?, ?, ?, ?)",
        (driver_id, uploaded_file.filename, stored_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return redirect("/drivers")


@app.route("/driver-files/<int:file_id>/delete", methods=["POST"])
def delete_driver_file(file_id):
    file_row = query_one("SELECT * FROM driver_files WHERE id = ?", (file_id,))
    if file_row:
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], file_row["stored_name"])
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError:
            pass
        execute("DELETE FROM driver_files WHERE id = ?", (file_id,))
    return redirect("/drivers")


@app.route("/trucks/<int:truck_id>/upload", methods=["POST"])
def upload_truck_file(truck_id):
    uploaded_file = request.files.get("file")
    if not uploaded_file or uploaded_file.filename == "":
        return redirect("/trucks")

    if not allowed_file(uploaded_file.filename):
        return redirect("/trucks")

    safe_name = secure_filename(uploaded_file.filename)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    stored_name = f"truck_{truck_id}_{timestamp}_{safe_name}"
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
    uploaded_file.save(save_path)

    execute(
        "INSERT INTO truck_files (truck_id, original_name, stored_name, uploaded_at) VALUES (?, ?, ?, ?)",
        (truck_id, uploaded_file.filename, stored_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return redirect("/trucks")


@app.route("/truck-files/<int:file_id>/delete", methods=["POST"])
def delete_truck_file(file_id):
    file_row = query_one("SELECT * FROM truck_files WHERE id = ?", (file_id,))
    if file_row:
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], file_row["stored_name"])
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError:
            pass
        execute("DELETE FROM truck_files WHERE id = ?", (file_id,))
    return redirect("/trucks")


@app.route("/trailers/<int:trailer_id>/upload", methods=["POST"])
def upload_trailer_file(trailer_id):
    uploaded_file = request.files.get("file")
    if not uploaded_file or uploaded_file.filename == "":
        return redirect("/trucks")

    if not allowed_file(uploaded_file.filename):
        return redirect("/trucks")

    safe_name = secure_filename(uploaded_file.filename)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    stored_name = f"trailer_{trailer_id}_{timestamp}_{safe_name}"
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
    uploaded_file.save(save_path)

    execute(
        "INSERT INTO trailer_files (trailer_id, original_name, stored_name, uploaded_at) VALUES (?, ?, ?, ?)",
        (trailer_id, uploaded_file.filename, stored_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return redirect("/trucks")


@app.route("/trailer-files/<int:file_id>/delete", methods=["POST"])
def delete_trailer_file(file_id):
    file_row = query_one("SELECT * FROM trailer_files WHERE id = ?", (file_id,))
    if file_row:
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], file_row["stored_name"])
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError:
            pass
        execute("DELETE FROM trailer_files WHERE id = ?", (file_id,))
    return redirect("/trucks")


@app.route("/update-status/<int:id>", methods=["POST"])
def update_status(id):
    status = request.form["status"]
    current = query_one("SELECT delivery_date FROM loads WHERE id=?", (id,))
    current_delivery = current["delivery_date"] if current else None
    new_delivery = current_delivery

    if status == "Delivered" and not current_delivery:
        new_delivery = datetime.now().strftime("%Y-%m-%d")

    execute("UPDATE loads SET status=?, delivery_date=? WHERE id=?", (status, new_delivery, id))
    return redirect("/loads")


@app.route("/invoices", methods=["GET", "POST"])
def invoices():
    if request.method == "POST":
        customer_id = request.form.get("customer_id") or None
        bill_to_name = request.form.get("bill_to_name", "").strip()
        bill_to_address = request.form.get("bill_to_address", "").strip()
        invoice_no = request.form.get("invoice_no", "").strip()
        invoice_date = request.form.get("invoice_date") or None
        terms = request.form.get("terms", "").strip() or "NET 30"
        due_date = request.form.get("due_date") or None
        load_id = request.form.get("load_id") or None
        line_date = request.form.get("line_date") or None
        truck_no = request.form.get("truck_no", "").strip()
        load_no = request.form.get("load_no", "").strip()
        origin_text = request.form.get("origin_text", "").strip()
        destination_text = request.form.get("destination_text", "").strip()
        quantity = float(request.form.get("quantity") or 1)
        rate = float(request.form.get("rate") or 0)
        amount = float(request.form.get("amount") or (rate * quantity))
        total = float(request.form.get("total") or amount)
        notes = request.form.get("notes", "").strip()

        execute(
            """
            INSERT INTO invoices (
                invoice_no, invoice_date, terms, due_date,
                customer_id, bill_to_name, bill_to_address,
                load_id, line_date, truck_no, load_no,
                origin_text, destination_text, quantity, rate,
                amount, total, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invoice_no, invoice_date, terms, due_date,
                customer_id, bill_to_name, bill_to_address,
                load_id, line_date, truck_no, load_no,
                origin_text, destination_text, quantity, rate,
                amount, total, notes, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ),
        )
        return redirect("/invoices")

    customers = query("SELECT * FROM customers ORDER BY name")
    loads_list = query("""
        SELECT l.*, c.name AS customer_name, c.address AS customer_address, t.unit_no AS truck_unit, tr.trailer_no AS trailer_no
        FROM loads l
        LEFT JOIN customers c ON c.id = l.customer_id
        LEFT JOIN trucks t ON t.id = l.truck_id
        LEFT JOIN trailers tr ON tr.id = l.trailer_id
        ORDER BY l.id DESC
    """)
    invoice_rows = query("""
        SELECT i.*, c.name AS customer_name
        FROM invoices i
        LEFT JOIN customers c ON c.id = i.customer_id
        WHERE COALESCE(i.is_hidden, 0) = 0
        ORDER BY i.id DESC
    """)

    customer_opts = "<option value=''>Select customer</option>"
    for c in customers:
        customer_opts += (
            f"<option value='{c['id']}' "
            f"data-name='{html_attr(c['name'])}' "
            f"data-address='{html_attr(c['address'])}'>"
            f"{c['name'] or ''}</option>"
        )

    load_opts = "<option value=''>Select load</option>"
    for l in loads_list:
        load_opts += (
            f"<option value='{l['id']}' "
            f"data-customer='{html_attr(l['customer_name'])}' "
            f"data-address='{html_attr(l['customer_address'])}' "
            f"data-truck='{html_attr(' / '.join(part for part in [l['truck_unit'] or '', 'Trailer ' + l['trailer_no'] if l['trailer_no'] else ''] if part))}' "
            f"data-loadno='{html_attr(l['broker_load_no'] or l['load_no'])}' "
            f"data-origin='{html_attr(((l['pickup_location_name'] or '') + ' ' + (l['pickup_address'] or '') + ' ' + (l['origin'] or '')).strip())}' "
            f"data-destination='{html_attr(((l['delivery_location_name'] or '') + ' ' + (l['delivery_address'] or '') + ' ' + (l['destination'] or '')).strip())}' "
            f"data-rate='{l['rate'] or 0}' "
            f"data-pickup='{l['pickup_date'] or ''}'>"
            f"{l['load_no'] or 'No Load #'} - {l['customer_name'] or 'No Customer'}"
            f"</option>"
        )

    next_invoice_row = query_one("SELECT MAX(CAST(invoice_no AS INTEGER)) AS max_no FROM invoices WHERE invoice_no GLOB '[0-9]*'")
    if next_invoice_row and next_invoice_row["max_no"]:
        next_invoice_no = str(int(next_invoice_row["max_no"]) + 1)
    else:
        next_invoice_no = "30452"

    invoice_cards = ""
    for inv in invoice_rows:
        route_text = f"{inv['origin_text'] or ''} - {inv['destination_text'] or ''}"

        invoice_cards += f"""
        <div class='invoice-wrap' style='margin-top:16px;'>
          <div class='action-links'>
  <a href='/invoices/{inv["id"]}/print' target='_blank'>Print</a>
  <a href='/invoices/{inv["id"]}/pdf' target='_blank'>Invoice Packet PDF</a>

  <form method='post' action='/invoices/{inv["id"]}/clear' style='display:inline;'>
    <button type='submit'>Clear</button>
  </form>

  <form method='post' action='/invoices/{inv["id"]}/delete'
        style='display:inline;'
        onsubmit="return confirm('Delete this invoice permanently?');">
    <button type='submit' style='background:#ffdddd;'>Delete</button>
  </form>
</div>

          <div class='invoice-head'>
            <div>
              <div class='invoice-title'>INVOICE</div>
              <div class='billto-box'>
                <div><strong>Bill To :</strong></div>
                <div>{inv['bill_to_name'] or ''}</div>
                <div style='white-space:pre-line'>{inv['bill_to_address'] or ''}</div>
              </div>
            </div>
            <div class='invoice-meta'>
              <table>
                <tr><td><strong>Invoice Date</strong></td><td>{inv['invoice_date'] or ''}</td></tr>
                <tr><td><strong>Invoice #</strong></td><td>{inv['invoice_no'] or ''}</td></tr>
                <tr><td><strong>Terms</strong></td><td>{inv['terms'] or ''}</td></tr>
                <tr><td><strong>Due Date</strong></td><td>{inv['due_date'] or ''}</td></tr>
              </table>
            </div>
          </div>

          <table class='invoice-table'>
            <tr>
              <th>Date</th>
              <th>Truck #</th>
              <th>Customer</th>
              <th>Load #</th>
              <th>Origin / Destination</th>
              <th>Quantity</th>
              <th>Rate</th>
              <th>Amount</th>
            </tr>
            <tr>
              <td class='nowrap'>{inv['line_date'] or ''}</td>
              <td>{inv['truck_no'] or ''}</td>
              <td>{inv['customer_name'] or inv['bill_to_name'] or ''}</td>
              <td>{inv['load_no'] or ''}</td>
              <td>
                <strong>Origin:</strong> {inv['origin_text'] or ''}<br>
                <strong>Destination:</strong> {inv['destination_text'] or ''}
              </td>
              <td>{inv['quantity'] or ''}</td>
              <td>${float(inv['rate'] or 0):,.2f}</td>
              <td>${float(inv['amount'] or 0):,.2f}</td>
            </tr>
          </table>

          <div class='invoice-total'>
            <div class='invoice-total-box'>
              <table>
                <tr>
                  <td><strong>TOTAL :</strong></td>
                  <td class='right'><strong>${float(inv['total'] or 0):,.2f}</strong></td>
                </tr>
              </table>
            </div>
          </div>
        </div>
        """

    if not invoice_cards:
        invoice_cards = "<div class='card' style='margin-top:16px;'>No invoices yet.</div>"

    return page(f"""
    <div class='grid'>
      <div class='card'>
        <h1>Invoices</h1>
        <form method='post' id='invoice-form'>
          <div class='form-grid'>
            <input name='invoice_no' id='invoice_no' placeholder='Invoice #' value='{next_invoice_no}' required>
            <input type='date' name='invoice_date' id='invoice_date' value='{today_ymd()}' required>
            <input name='terms' id='terms' placeholder='Terms' value='NET 30'>
            <input type='date' name='due_date' id='due_date' value='{plus_30_days_ymd()}' required>

            <select name='customer_id' id='invoice_customer_id'>
              {customer_opts}
            </select>

            <textarea name='bill_to_name' id='bill_to_name' placeholder='Bill To Name' rows='2'></textarea>
            <textarea name='bill_to_address' id='bill_to_address' placeholder='Bill To Address' rows='3'></textarea>

            <select name='load_id' id='invoice_load_id'>
              {load_opts}
            </select>

            <input type='date' name='line_date' id='line_date' value='{today_ymd()}'>
            <input name='truck_no' id='truck_no' placeholder='Truck #'>
            <input name='load_no' id='load_no' placeholder='Load #'>
            <input name='origin_text' id='origin_text' placeholder='Origin'>
            <input name='destination_text' id='destination_text' placeholder='Destination'>
            <input name='quantity' id='quantity' placeholder='Quantity' value='1'>
            <input name='rate' id='rate' placeholder='Rate'>
            <input name='amount' id='amount' placeholder='Amount'>
            <input name='total' id='total' placeholder='Total'>
          </div>
          <textarea name='notes' placeholder='Notes' rows='3'></textarea>
          <button>Create Invoice</button>
          <p class='muted'>Invoice layout follows your sample format with Invoice Date, Invoice #, Terms, Due Date, Bill To, one line item row, and TOTAL.</p>
        </form>
      </div>
    </div>

    {invoice_cards}

    <script>
      const loadSelect = document.getElementById('invoice_load_id');
      const customerSelect = document.getElementById('invoice_customer_id');
      const billToName = document.getElementById('bill_to_name');
      const billToAddress = document.getElementById('bill_to_address');
      const lineDate = document.getElementById('line_date');
      const truckNo = document.getElementById('truck_no');
      const loadNo = document.getElementById('load_no');
      const originText = document.getElementById('origin_text');
      const destinationText = document.getElementById('destination_text');
      const rate = document.getElementById('rate');
      const amount = document.getElementById('amount');
      const total = document.getElementById('total');
      const quantity = document.getElementById('quantity');
      const terms = document.getElementById('terms');
      const invoiceDate = document.getElementById('invoice_date');
      const dueDate = document.getElementById('due_date');

      function recalcInvoice() {{
        const qty = parseFloat(quantity.value || 0);
        const rt = parseFloat(rate.value || 0);
        const calc = qty * rt;
        amount.value = calc.toFixed(2);
        total.value = calc.toFixed(2);
      }}

      function updateDueDateFromTerms() {{
        const invoiceDateVal = invoiceDate.value;
        const termsVal = (terms.value || '').trim().toUpperCase();

        if (!invoiceDateVal) return;

        let days = 30;
        const match = termsVal.match(/NET\\s*(\\d+)/);
        if (match) {{
          days = parseInt(match[1], 10);
        }}

        const d = new Date(invoiceDateVal + 'T00:00:00');
        d.setDate(d.getDate() + days);

        const yyyy = d.getFullYear();
        const mm = String(d.getMonth() + 1).padStart(2, '0');
        const dd = String(d.getDate()).padStart(2, '0');
        dueDate.value = `${{yyyy}}-${{mm}}-${{dd}}`;
      }}

      if (quantity) quantity.addEventListener('input', recalcInvoice);
      if (rate) rate.addEventListener('input', recalcInvoice);
      if (terms) terms.addEventListener('input', updateDueDateFromTerms);
      if (invoiceDate) invoiceDate.addEventListener('change', updateDueDateFromTerms);

      if (loadSelect) {{
        loadSelect.addEventListener('change', () => {{
          const opt = loadSelect.options[loadSelect.selectedIndex];
          if (!opt.value) return;

          billToName.value = opt.dataset.customer || '';
          billToAddress.value = opt.dataset.address || '';
          lineDate.value = opt.dataset.pickup || '';
          truckNo.value = opt.dataset.truck || '';
          loadNo.value = opt.dataset.loadno || '';
          originText.value = opt.dataset.origin || '';
          destinationText.value = opt.dataset.destination || '';
          rate.value = opt.dataset.rate || '0';
          quantity.value = '1';

          recalcInvoice();
        }});
      }}

      if (customerSelect) {{
        customerSelect.addEventListener('change', () => {{
          const opt = customerSelect.options[customerSelect.selectedIndex];
          if (opt.value) {{
            billToName.value = opt.dataset.name || opt.text || '';
            billToAddress.value = opt.dataset.address || '';
          }}
        }});
      }}
    </script>
    """)


@app.route("/invoices/<int:invoice_id>/print")
def print_invoice(invoice_id):
    inv = query_one("""
        SELECT i.*, c.name AS customer_name
        FROM invoices i
        LEFT JOIN customers c ON c.id = i.customer_id
        WHERE i.id = ?
    """, (invoice_id,))

    if not inv:
        return "Invoice not found", 404

    route_text = f"{inv['origin_text'] or ''} - {inv['destination_text'] or ''}"

    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>Invoice {inv['invoice_no'] or ''}</title>
      <style>
        body {{ font-family: Arial, sans-serif; margin: 30px; color: #111; }}
        .invoice-wrap {{ background: white; }}
        .invoice-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }}
        .invoice-title {{ font-size: 34px; font-weight: bold; margin: 0 0 10px 0; }}
        .invoice-meta {{ min-width: 280px; }}
        .invoice-meta table {{ width: 100%; border-collapse: collapse; }}
        .invoice-meta td {{ padding: 4px 8px; border: none; }}
        .billto-box {{ margin-top: 18px; margin-bottom: 18px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ padding: 10px; border: 1px solid #ddd; text-align: left; vertical-align: top; }}
        .invoice-total {{ margin-top: 12px; display: flex; justify-content: flex-end; }}
        .invoice-total-box {{ width: 260px; }}
        .right {{ text-align: right; }}
        .print-bar {{ margin-bottom: 20px; }}
        @media print {{
          .print-bar {{ display: none; }}
          body {{ margin: 0.4in; }}
        }}
      </style>
    </head>
    <body>
      <div class="print-bar">
        <button onclick="window.print()">Print Invoice</button>
      </div>

      <div class='invoice-wrap'>
        <div class='invoice-head'>
          <div>
            <div class='invoice-title'>INVOICE</div>
            <div class='billto-box'>
              <div><strong>Bill To :</strong></div>
              <div>{inv['bill_to_name'] or ''}</div>
              <div style='white-space:pre-line'>{inv['bill_to_address'] or ''}</div>
            </div>
          </div>
          <div class='invoice-meta'>
            <table>
              <tr><td><strong>Invoice Date</strong></td><td>{inv['invoice_date'] or ''}</td></tr>
              <tr><td><strong>Invoice #</strong></td><td>{inv['invoice_no'] or ''}</td></tr>
              <tr><td><strong>Terms</strong></td><td>{inv['terms'] or ''}</td></tr>
              <tr><td><strong>Due Date</strong></td><td>{inv['due_date'] or ''}</td></tr>
            </table>
          </div>
        </div>

        <table>
          <tr>
            <th>Date</th>
            <th>Truck #</th>
            <th>Customer</th>
            <th>Load #</th>
            <th>Origin / Destination</th>
            <th>Quantity</th>
            <th>Rate</th>
            <th>Amount</th>
          </tr>
          <tr>
            <td>{inv['line_date'] or ''}</td>
            <td>{inv['truck_no'] or ''}</td>
            <td>{inv['customer_name'] or inv['bill_to_name'] or ''}</td>
            <td>{inv['load_no'] or ''}</td>
            <td>
                <strong>Origin:</strong> {inv['origin_text'] or ''}<br>
                <strong>Destination:</strong> {inv['destination_text'] or ''}
              </td>
            <td>{inv['quantity'] or ''}</td>
            <td>${float(inv['rate'] or 0):,.2f}</td>
            <td>${float(inv['amount'] or 0):,.2f}</td>
          </tr>
        </table>

        <div class='invoice-total'>
          <div class='invoice-total-box'>
            <table>
              <tr>
                <td><strong>TOTAL :</strong></td>
                <td class='right'><strong>${float(inv['total'] or 0):,.2f}</strong></td>
              </tr>
            </table>
          </div>
        </div>
      </div>
    </body>
    </html>
    """




def add_invoice_packet_pages(writer, inv, include_no_docs_note=False):
    """Add one invoice page plus its attached load-board paperwork to an existing PdfWriter."""
    invoice_buffer = BytesIO()
    pdf = canvas.Canvas(invoice_buffer, pagesize=letter)
    width, height = letter

    left = 50
    y = height - 50

    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawString(left, y, "INVOICE")

    pdf.setFont("Helvetica", 10)
    pdf.drawString(380, y, f"Invoice Date  {inv['invoice_date'] or ''}")
    pdf.drawString(380, y - 18, f"Invoice #  {inv['invoice_no'] or ''}")
    pdf.drawString(380, y - 36, f"Terms  {inv['terms'] or ''}")
    pdf.drawString(380, y - 54, f"Due Date  {inv['due_date'] or ''}")

    y -= 95
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(left, y, "Bill To :")

    y -= 18
    pdf.setFont("Helvetica", 10)
    pdf.drawString(left, y, inv["bill_to_name"] or "")

    y -= 15
    bill_lines = (inv["bill_to_address"] or "").splitlines()
    for line in bill_lines:
        pdf.drawString(left, y, line[:80])
        y -= 14

    y -= 10

    headers = ["Date", "Truck #", "Customer", "Load #", "Origin / Destination", "Qty", "Rate", "Amount"]
    x_positions = [50, 100, 150, 235, 290, 455, 495, 545]

    pdf.setFont("Helvetica-Bold", 9)
    for i, h in enumerate(headers):
        pdf.drawString(x_positions[i], y, h)

    y -= 8
    pdf.line(50, y, 590, y)
    y -= 18

    route_text = f"{inv['origin_text'] or ''} - {inv['destination_text'] or ''}"
    customer_name = inv["customer_name"] or inv["bill_to_name"] or ""

    pdf.setFont("Helvetica", 9)
    pdf.drawString(50, y, str(inv["line_date"] or ""))
    pdf.drawString(100, y, str(inv["truck_no"] or ""))
    pdf.drawString(150, y, customer_name[:14])
    pdf.drawString(235, y, str(inv["load_no"] or ""))
    origin_lines = pdf_wrap_lines(inv["origin_text"] or "", max_chars=34, max_lines=2)
    destination_lines = pdf_wrap_lines(inv["destination_text"] or "", max_chars=34, max_lines=2)

    route_y = y
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(290, route_y, "Origin:")
    pdf.setFont("Helvetica", 8)
    for line in origin_lines:
        pdf.drawString(330, route_y, line)
        route_y -= 10

    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(290, route_y, "Destination:")
    pdf.setFont("Helvetica", 8)
    for line in destination_lines:
        pdf.drawString(350, route_y, line)
        route_y -= 10

    pdf.drawRightString(485, y, str(inv["quantity"] or ""))
    pdf.drawRightString(540, y, f"${float(inv['rate'] or 0):,.2f}")
    pdf.drawRightString(590, y, f"${float(inv['amount'] or 0):,.2f}")

    y -= 60
    pdf.line(430, y + 18, 590, y + 18)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(450, y, "TOTAL :")
    pdf.drawRightString(590, y, f"${float(inv['total'] or 0):,.2f}")

    pdf.showPage()
    pdf.save()
    invoice_buffer.seek(0)

    invoice_reader = PdfReader(invoice_buffer)
    for page in invoice_reader.pages:
        writer.add_page(page)

    attached_count = 0
    image_exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")

    if inv["load_id"]:
        load_files = query(
            "SELECT * FROM load_files WHERE load_id = ? ORDER BY uploaded_at ASC, id ASC",
            (inv["load_id"],)
        )

        for f in load_files:
            original_name = (f["original_name"] or "").lower()
            stored_name = f["stored_name"] or ""
            file_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)

            if not os.path.exists(file_path):
                continue

            if original_name.endswith(".pdf") or stored_name.lower().endswith(".pdf"):
                try:
                    with open(file_path, "rb") as extra_pdf:
                        extra_reader = PdfReader(extra_pdf)
                        for page in extra_reader.pages:
                            writer.add_page(page)
                    attached_count += 1
                except Exception:
                    continue

            elif original_name.endswith(image_exts) or stored_name.lower().endswith(image_exts):
                try:
                    img = Image.open(file_path).convert("RGB")
                    img_buffer = BytesIO()
                    img_width, img_height = img.size
                    page_width, page_height = letter
                    image_pdf = canvas.Canvas(img_buffer, pagesize=letter)
                    margin = 36
                    max_width = page_width - (margin * 2)
                    max_height = page_height - (margin * 2)
                    scale = min(max_width / img_width, max_height / img_height)
                    draw_width = img_width * scale
                    draw_height = img_height * scale
                    x = (page_width - draw_width) / 2
                    y_img = (page_height - draw_height) / 2
                    image_pdf.drawImage(file_path, x, y_img, width=draw_width, height=draw_height, preserveAspectRatio=True, mask="auto")
                    image_pdf.showPage()
                    image_pdf.save()
                    img_buffer.seek(0)
                    img_reader = PdfReader(img_buffer)
                    for page in img_reader.pages:
                        writer.add_page(page)
                    attached_count += 1
                except Exception:
                    continue

    if include_no_docs_note and attached_count == 0:
        note_buffer = BytesIO()
        note_pdf = canvas.Canvas(note_buffer, pagesize=letter)
        note_pdf.setFont("Helvetica-Bold", 16)
        note_pdf.drawString(50, 750, "Invoice Backup Docs")
        note_pdf.setFont("Helvetica", 11)
        note_pdf.drawString(50, 720, f"Invoice {inv['invoice_no'] or inv['id']} has no readable attached PDF or image paperwork.")
        note_pdf.showPage()
        note_pdf.save()
        note_buffer.seek(0)
        note_reader = PdfReader(note_buffer)
        for page in note_reader.pages:
            writer.add_page(page)

    return attached_count

@app.route("/invoices/<int:invoice_id>/pdf")
def invoice_pdf(invoice_id):
    inv = query_one("""
        SELECT i.*, c.name AS customer_name
        FROM invoices i
        LEFT JOIN customers c ON c.id = i.customer_id
        WHERE i.id = ?
    """, (invoice_id,))

    if not inv:
        return "Invoice not found", 404

    # Create the invoice page first
    invoice_buffer = BytesIO()
    pdf = canvas.Canvas(invoice_buffer, pagesize=letter)
    width, height = letter

    left = 50
    y = height - 50

    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawString(left, y, "INVOICE")

    pdf.setFont("Helvetica", 10)
    pdf.drawString(380, y, f"Invoice Date  {inv['invoice_date'] or ''}")
    pdf.drawString(380, y - 18, f"Invoice #  {inv['invoice_no'] or ''}")
    pdf.drawString(380, y - 36, f"Terms  {inv['terms'] or ''}")
    pdf.drawString(380, y - 54, f"Due Date  {inv['due_date'] or ''}")

    y -= 95
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(left, y, "Bill To :")

    y -= 18
    pdf.setFont("Helvetica", 10)
    pdf.drawString(left, y, inv["bill_to_name"] or "")

    y -= 15
    bill_lines = (inv["bill_to_address"] or "").splitlines()
    for line in bill_lines:
        pdf.drawString(left, y, line)
        y -= 14

    y -= 10

    headers = ["Date", "Truck #", "Customer", "Load #", "Origin / Destination", "Qty", "Rate", "Amount"]
    x_positions = [50, 100, 150, 235, 290, 455, 495, 545]

    pdf.setFont("Helvetica-Bold", 9)
    for i, h in enumerate(headers):
        pdf.drawString(x_positions[i], y, h)

    y -= 8
    pdf.line(50, y, 590, y)
    y -= 18

    route_text = f"{inv['origin_text'] or ''} - {inv['destination_text'] or ''}"
    customer_name = inv["customer_name"] or inv["bill_to_name"] or ""

    pdf.setFont("Helvetica", 9)
    pdf.drawString(50, y, str(inv["line_date"] or ""))
    pdf.drawString(100, y, str(inv["truck_no"] or ""))
    pdf.drawString(150, y, customer_name[:14])
    pdf.drawString(235, y, str(inv["load_no"] or ""))
    origin_lines = pdf_wrap_lines(inv["origin_text"] or "", max_chars=34, max_lines=2)
    destination_lines = pdf_wrap_lines(inv["destination_text"] or "", max_chars=34, max_lines=2)

    route_y = y
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(290, route_y, "Origin:")
    pdf.setFont("Helvetica", 8)
    for line in origin_lines:
        pdf.drawString(330, route_y, line)
        route_y -= 10

    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(290, route_y, "Destination:")
    pdf.setFont("Helvetica", 8)
    for line in destination_lines:
        pdf.drawString(350, route_y, line)
        route_y -= 10

    pdf.drawRightString(485, y, str(inv["quantity"] or ""))
    pdf.drawRightString(540, y, f"${float(inv['rate'] or 0):,.2f}")
    pdf.drawRightString(590, y, f"${float(inv['amount'] or 0):,.2f}")

    y -= 60
    pdf.line(430, y + 18, 590, y + 18)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(450, y, "TOTAL :")
    pdf.drawRightString(590, y, f"${float(inv['total'] or 0):,.2f}")

    pdf.showPage()
    pdf.save()
    invoice_buffer.seek(0)

    # Merge invoice + attached paperwork from the load board
    writer = PdfWriter()

    invoice_reader = PdfReader(invoice_buffer)
    for page in invoice_reader.pages:
        writer.add_page(page)

    attached_count = 0
    image_exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")

    if inv["load_id"]:
        load_files = query(
            "SELECT * FROM load_files WHERE load_id = ? ORDER BY uploaded_at ASC, id ASC",
            (inv["load_id"],)
        )

        for f in load_files:
            original_name = (f["original_name"] or "").lower()
            stored_name = f["stored_name"] or ""
            file_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)

            if not os.path.exists(file_path):
                continue

            # Attach PDFs as extra pages
            if original_name.endswith(".pdf") or stored_name.lower().endswith(".pdf"):
                try:
                    with open(file_path, "rb") as extra_pdf:
                        extra_reader = PdfReader(extra_pdf)
                        for page in extra_reader.pages:
                            writer.add_page(page)
                    attached_count += 1
                except Exception:
                    continue

            # Convert uploaded images to PDF pages and attach them
            elif original_name.endswith(image_exts) or stored_name.lower().endswith(image_exts):
                try:
                    img = Image.open(file_path)
                    img = img.convert("RGB")

                    img_buffer = BytesIO()
                    img_width, img_height = img.size
                    page_width, page_height = letter

                    image_pdf = canvas.Canvas(img_buffer, pagesize=letter)
                    margin = 36

                    max_width = page_width - (margin * 2)
                    max_height = page_height - (margin * 2)

                    scale = min(max_width / img_width, max_height / img_height)
                    draw_width = img_width * scale
                    draw_height = img_height * scale

                    x = (page_width - draw_width) / 2
                    y_img = (page_height - draw_height) / 2

                    image_pdf.drawImage(
                        file_path,
                        x,
                        y_img,
                        width=draw_width,
                        height=draw_height,
                        preserveAspectRatio=True,
                        mask="auto"
                    )
                    image_pdf.showPage()
                    image_pdf.save()
                    img_buffer.seek(0)

                    img_reader = PdfReader(img_buffer)
                    for page in img_reader.pages:
                        writer.add_page(page)

                    attached_count += 1
                except Exception:
                    continue

    # Add a note page if no backup docs were found
    if attached_count == 0:
        note_buffer = BytesIO()
        note_pdf = canvas.Canvas(note_buffer, pagesize=letter)

        note_pdf.setFont("Helvetica-Bold", 16)
        note_pdf.drawString(50, 750, "Invoice Backup Docs")

        note_pdf.setFont("Helvetica", 11)
        if inv["load_id"]:
            note_pdf.drawString(50, 720, "No readable backup docs were attached from the load board.")
            note_pdf.drawString(50, 700, f"Load ID: {inv['load_id']}")
            note_pdf.drawString(50, 680, "Attach PDF or image files to this load, then generate the Invoice Packet PDF again.")
        else:
            note_pdf.drawString(50, 720, "This invoice is not linked to a load.")
            note_pdf.drawString(50, 700, "Create the invoice from a load or select a load on the invoice form.")

        note_pdf.showPage()
        note_pdf.save()
        note_buffer.seek(0)

        note_reader = PdfReader(note_buffer)
        for page in note_reader.pages:
            writer.add_page(page)

    output_buffer = BytesIO()
    writer.write(output_buffer)
    output_buffer.seek(0)

    filename = f"invoice_{inv['invoice_no'] or invoice_id}_packet.pdf"
    return send_file(
        output_buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf"
    )

@app.route("/driver-loads")
def driver_report():
    driver_id = request.args.get("driver_id", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")

    drivers = query("SELECT * FROM drivers ORDER BY name")
    opts = "<option value=''>Select driver</option>" + "".join(
        f"<option value='{d['id']}' {selected(d['id'], driver_id)}>{d['name']}</option>" for d in drivers
    )

    rows = "<tr><td colspan='13'>Select a driver and run the report.</td></tr>"
    if driver_id:
        sql = """
        SELECT l.*, d.name AS driver_name, c.name AS customer_name
        FROM loads l
        LEFT JOIN drivers d ON d.id = l.driver_id
        LEFT JOIN customers c ON c.id = l.customer_id
        WHERE l.driver_id = ?
        """
        params = [driver_id]

        if start:
            sql += " AND COALESCE(l.delivery_date, l.pickup_date) >= ?"
            params.append(start)
        if end:
            sql += " AND COALESCE(l.delivery_date, l.pickup_date) <= ?"
            params.append(end)

        sql += " ORDER BY COALESCE(l.delivery_date, l.pickup_date) DESC, l.id DESC"
        data = query(sql, tuple(params))

        report_rows = []
        for r in data:
            total_miles = total_route_miles(
                r["origin_zip"],
                r["optional_stop_1_zip"],
                r["optional_stop_2_zip"],
                r["destination_zip"],
            )
            report_rows.append(
                f"<tr>"
                f"<td>{r['load_no'] or ''}</td>"
                f"<td>{r['customer_name'] or ''}</td>"
                f"<td>{r['origin'] or ''}</td>"
                f"<td>{r['origin_zip'] or ''}</td>"
                f"<td>{r['optional_stop_1'] or ''}</td>"
                f"<td>{r['optional_stop_1_zip'] or ''}</td>"
                f"<td>{r['optional_stop_2'] or ''}</td>"
                f"<td>{r['optional_stop_2_zip'] or ''}</td>"
                f"<td>{r['destination'] or ''}</td>"
                f"<td>{r['destination_zip'] or ''}</td>"
                f"<td>{format_miles(total_miles)}</td>"
                f"<td>${float(r['rate'] or 0):,.2f}</td>"
                f"<td>{r['status'] or ''}</td>"
                f"</tr>"
            )

        rows = "".join(report_rows) or "<tr><td colspan='13'>No loads found for that driver and date range.</td></tr>"

    return page(f"""
    <div class='card'>
      <h1>Driver Report</h1>
      <form>
        <select name='driver_id' required>{opts}</select>
        <label>Start date</label>
        <input type='date' name='start' value='{start}'>
        <label>End date</label>
        <input type='date' name='end' value='{end}'>
        <button>Run</button>
      </form>
      <table>
        <tr>
          <th>Load #</th>
          <th>Customer</th>
          <th>Origin</th>
          <th>Origin ZIP</th>
          <th>Stop 1</th>
          <th>Stop 1 ZIP</th>
          <th>Stop 2</th>
          <th>Stop 2 ZIP</th>
          <th>Destination</th>
          <th>Destination ZIP</th>
          <th>Total Miles</th>
          <th>Rate</th>
          <th>Status</th>
        </tr>
        {rows}
      </table>
    </div>
    """)

@app.route("/invoice-archive")
def invoice_archive():
    search = request.args.get("search", "").strip()
    show_cleared = request.args.get("show_cleared", "")
    start_inv = request.args.get("start_inv", "").strip()
    end_inv = request.args.get("end_inv", "").strip()

    sql = """
        SELECT i.*, c.name AS customer_name
        FROM invoices i
        LEFT JOIN customers c ON c.id = i.customer_id
        WHERE 1=1
    """
    params = []

    if start_inv.isdigit():
        sql += " AND CAST(i.invoice_no AS INTEGER) >= ?"
        params.append(int(start_inv))

    if end_inv.isdigit():
        sql += " AND CAST(i.invoice_no AS INTEGER) <= ?"
        params.append(int(end_inv))

    if not show_cleared:
        sql += " AND COALESCE(i.is_hidden, 0) = 0"

    if search:
        like = f"%{search}%"
        sql += """
        AND (
            i.invoice_no LIKE ?
            OR i.load_no LIKE ?
            OR i.bill_to_name LIKE ?
            OR c.name LIKE ?
            OR i.origin_text LIKE ?
            OR i.destination_text LIKE ?
        )
        """
        params.extend([like, like, like, like, like, like])

    sql += " ORDER BY i.id DESC"
    rows = query(sql, tuple(params))

    screen_total = sum(float(r["total"] or 0) for r in rows)
    invoice_count = len(rows)
    query_string = request.query_string.decode("utf-8")
    batch_url = "/invoice-archive/batch-pdf" + ("?" + query_string if query_string else "")

    saved_count_notice = request.args.get("saved_count", "").strip()
    saved_folder_notice = request.args.get("saved_folder", "").strip()
    saved_message = ""
    if saved_count_notice:
        saved_message = f"""
        <div class='card' style='background:#eefbea; border:1px solid #b8e6b0; margin-bottom:12px;'>
          <strong>{html_attr(saved_count_notice)}</strong> invoice packet(s) saved and cleared from this board.
          {("<br><span class='muted'>Folder: " + html_attr(saved_folder_notice) + "</span>") if saved_folder_notice else ''}
        </div>
        """

    tr = ""
    for r in rows:
        tr += f"""
        <tr>
          <td><input type='checkbox' name='invoice_ids' value='{r["id"]}'></td>
          <td>{r['invoice_no'] or ''}</td>
          <td>{r['invoice_date'] or ''}</td>
          <td>{r['due_date'] or ''}</td>
          <td>{r['customer_name'] or r['bill_to_name'] or ''}</td>
          <td>{r['load_no'] or ''}</td>
          <td>{r['origin_text'] or ''} - {r['destination_text'] or ''}</td>
          <td>${float(r['total'] or 0):,.2f}</td>
          <td>
            <a href='/invoices/{r["id"]}/print' target='_blank'>Print</a> |
            <a href='/invoices/{r["id"]}/pdf' target='_blank'>Invoice Packet PDF</a>
          </td>
        </tr>
        """

    if not tr:
        tr = "<tr><td colspan='9'>No invoices found.</td></tr>"

    checked = "checked" if show_cleared else ""

    return page(f"""
    <div class='card'>
      <h1>Invoice Archive</h1>
      {saved_message}

      <form method='get'>
        <input name='search' placeholder='Search invoice #, load #, customer, origin, destination' value='{html_attr(search)}'>
        <input name='start_inv' placeholder='From Invoice # example 23' value='{html_attr(start_inv)}'>
        <input name='end_inv' placeholder='To Invoice # example 120' value='{html_attr(end_inv)}'>
        <label>
          <input type='checkbox' name='show_cleared' value='1' {checked}>
          Show cleared invoices
        </label>
        <button>Search</button>
      </form>

      <div class='stats' style='margin-top:16px;'>
        <div class='card'><h2>{invoice_count}</h2><div>Invoices on screen</div></div>
        <div class='card'><h2>${screen_total:,.2f}</h2><div>Total of invoices on screen</div></div>
      </div>

      <div style='margin-top:16px; margin-bottom:10px;'>
        <a href='{html_attr(batch_url)}' target='_blank' style='display:inline-block; padding:10px 14px; background:#eef3ff; border-radius:8px;'>
          Download Batch Invoice Packet PDF
        </a>
      </div>

      <form method='post' action='/invoice-archive/save-selected'>
        <div class='card' style='margin-top:12px; margin-bottom:12px;'>
          <h3>Save Selected Invoice PDFs to Folder</h3>
          <input name='folder_name' placeholder='Folder name, example: Evans April 2026' required>
          <button>Save Selected</button>
          <p class='muted'>Select invoices below. If the folder already exists, the PDFs will be placed inside that folder.</p>
        </div>

        <table>
          <tr>
            <th>Select</th>
            <th>Invoice #</th>
            <th>Invoice Date</th>
            <th>Due Date</th>
            <th>Customer</th>
            <th>Load #</th>
            <th>Route</th>
            <th>Total</th>
            <th>Actions</th>
          </tr>
          {tr}
        </table>
      </form>
    </div>
    """)


@app.route("/invoice-archive/batch-pdf")
def invoice_archive_batch_pdf():
    search = request.args.get("search", "").strip()
    show_cleared = request.args.get("show_cleared", "")
    start_inv = request.args.get("start_inv", "").strip()
    end_inv = request.args.get("end_inv", "").strip()

    sql = """
        SELECT i.*, c.name AS customer_name
        FROM invoices i
        LEFT JOIN customers c ON c.id = i.customer_id
        WHERE 1=1
    """
    params = []

    if start_inv.isdigit():
        sql += " AND CAST(i.invoice_no AS INTEGER) >= ?"
        params.append(int(start_inv))

    if end_inv.isdigit():
        sql += " AND CAST(i.invoice_no AS INTEGER) <= ?"
        params.append(int(end_inv))

    if not show_cleared:
        sql += " AND COALESCE(i.is_hidden, 0) = 0"

    if search:
        like = f"%{search}%"
        sql += """
        AND (
            i.invoice_no LIKE ?
            OR i.load_no LIKE ?
            OR i.bill_to_name LIKE ?
            OR c.name LIKE ?
            OR i.origin_text LIKE ?
            OR i.destination_text LIKE ?
        )
        """
        params.extend([like, like, like, like, like, like])

    sql += " ORDER BY i.id DESC"
    rows = query(sql, tuple(params))

    writer = PdfWriter()
    screen_total = sum(float(r["total"] or 0) for r in rows)

    # Cover page with batch summary
    cover_buffer = BytesIO()
    cover = canvas.Canvas(cover_buffer, pagesize=letter)
    cover.setFont("Helvetica-Bold", 22)
    cover.drawString(50, 750, "Batch Invoice Packet")
    cover.setFont("Helvetica", 12)
    cover.drawString(50, 720, f"Invoice count: {len(rows)}")
    cover.drawString(50, 700, f"Total of invoices: ${screen_total:,.2f}")
    y_line = 680
    if search:
        cover.drawString(50, y_line, f"Search filter: {search[:80]}")
        y_line -= 20
    if start_inv or end_inv:
        cover.drawString(50, y_line, f"Invoice range: {start_inv or 'Any'} to {end_inv or 'Any'}")
        y_line -= 20
    cover.drawString(50, y_line - 10, "Each invoice is followed by its attached load-board paperwork when available.")
    cover.showPage()
    cover.save()
    cover_buffer.seek(0)
    cover_reader = PdfReader(cover_buffer)
    for page in cover_reader.pages:
        writer.add_page(page)

    for inv in rows:
        add_invoice_packet_pages(writer, inv, include_no_docs_note=False)

    output_buffer = BytesIO()
    writer.write(output_buffer)
    output_buffer.seek(0)

    filename = f"batch_invoice_packet_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return send_file(
        output_buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf"
    )

@app.route("/invoice-archive/save-selected", methods=["POST"])
def save_selected_invoices_to_folder():
    invoice_ids = request.form.getlist("invoice_ids")
    folder_name = request.form.get("folder_name", "").strip()

    if not invoice_ids:
        return page("""
        <div class='card'>
          <h1>No Invoices Selected</h1>
          <p>Please go back and check at least one invoice.</p>
          <p><a href='/invoice-archive'>Back to Invoice Archive</a></p>
        </div>
        """)

    safe_folder_name = secure_filename(folder_name)
    if not safe_folder_name:
        safe_folder_name = "saved_invoices"

    target_folder = os.path.join(SAVED_INVOICE_FOLDER, safe_folder_name)
    os.makedirs(target_folder, exist_ok=True)

    placeholders = ",".join("?" for _ in invoice_ids)
    rows = query(f"""
        SELECT i.*, c.name AS customer_name
        FROM invoices i
        LEFT JOIN customers c ON c.id = i.customer_id
        WHERE i.id IN ({placeholders})
        ORDER BY CAST(i.invoice_no AS INTEGER), i.id
    """, tuple(invoice_ids))

    saved_links = ""
    saved_count = 0

    for inv in rows:
        writer = PdfWriter()
        add_invoice_packet_pages(writer, inv, include_no_docs_note=True)

        invoice_no = secure_filename(str(inv["invoice_no"] or inv["id"]))
        filename = f"invoice_{invoice_no}_packet.pdf"
        file_path = unique_file_path(target_folder, filename)

        with open(file_path, "wb") as output_file:
            writer.write(output_file)

        saved_count += 1
        saved_filename = os.path.basename(file_path)
        download_path = f"{safe_folder_name}/{saved_filename}"
        saved_links += f"<li><a href='/saved-invoices/{download_path}' target='_blank'>{saved_filename}</a></li>"

    # Clear saved invoices from the active invoice board, but keep them in the database.
    for inv_id in invoice_ids:
        execute("UPDATE invoices SET is_hidden = 1 WHERE id = ?", (inv_id,))

    # Send user back to the archive so the cleared invoices disappear immediately.
    return redirect(f"/invoice-archive?saved_count={saved_count}&saved_folder={safe_folder_name}")


@app.route("/saved-invoices/<path:filename>")
def saved_invoice_file(filename):
    return send_from_directory(SAVED_INVOICE_FOLDER, filename, as_attachment=True)


@app.route("/loads/<int:load_id>/create-invoice", methods=["POST"])
def create_invoice_from_load(load_id):
    load = query_one("""
        SELECT l.*, c.name AS customer_name, c.address AS customer_address, t.unit_no AS truck_unit, tr.trailer_no AS trailer_no
        FROM loads l
        LEFT JOIN customers c ON c.id = l.customer_id
        LEFT JOIN trucks t ON t.id = l.truck_id
        LEFT JOIN trailers tr ON tr.id = l.trailer_id
        WHERE l.id = ?
    """, (load_id,))

    if not load:
        return "Load not found", 404

    next_invoice_row = query_one(
        "SELECT MAX(CAST(invoice_no AS INTEGER)) AS max_no FROM invoices WHERE invoice_no GLOB '[0-9]*'"
    )

    if next_invoice_row and next_invoice_row["max_no"]:
        next_invoice_no = str(int(next_invoice_row["max_no"]) + 1)
    else:
        next_invoice_no = "1"

    invoice_date = today_ymd()
    due_date = plus_30_days_ymd()
    quantity = 1
    rate = float(load["rate"] or 0)
    amount = rate * quantity

    execute("""
        INSERT INTO invoices (
            invoice_no, invoice_date, terms, due_date,
            customer_id, bill_to_name, bill_to_address,
            load_id, line_date, truck_no, load_no,
            origin_text, destination_text, quantity, rate,
            amount, total, notes, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        next_invoice_no,
        invoice_date,
        "NET 30",
        due_date,
        load["customer_id"],
        load["customer_name"] or "",
        load["customer_address"] or "",
        load_id,
        load["pickup_date"] or invoice_date,
        " / ".join(part for part in [load["truck_unit"] or "", "Trailer " + load["trailer_no"] if load["trailer_no"] else ("Trailer " + load["load_trailer_no"] if load["load_trailer_no"] else "")] if part),
        load["broker_load_no"] or load["load_no"] or "",
        " ".join(part for part in [load["pickup_location_name"] or "", load["pickup_address"] or "", load["origin"] or ""] if part),
        " ".join(part for part in [load["delivery_location_name"] or "", load["delivery_address"] or "", load["destination"] or ""] if part),
        quantity,
        rate,
        amount,
        amount,
        "",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ))

    return redirect("/invoices")


@app.route("/invoices/<int:invoice_id>/clear", methods=["POST"])
def clear_invoice(invoice_id):
    execute("UPDATE invoices SET is_hidden = 1 WHERE id = ?", (invoice_id,))
    return redirect("/invoices")


@app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
def delete_invoice(invoice_id):
    execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    return redirect("/invoices")


if __name__ == "__main__":
    init_db()

    # Listen on all network interfaces so other devices on the same Wi-Fi/LAN
    # can open the app using: http://YOUR_COMPUTER_IP:5000
    port = int(os.environ.get("PORT", 5000))
    print("Transport MVP is running.")
    print(f"Local computer: http://127.0.0.1:{port}")
    print(f"Local network:  http://YOUR_COMPUTER_IP:{port}")

    app.run(host="0.0.0.0", port=port, debug=False)