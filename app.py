from flask import Flask, request, redirect, render_template_string, jsonify, send_from_directory, send_file
import os
import sqlite3
from driver_statement import mileage_input
from load_confirmation import CONFIRMATION_UI, register_confirmation, read_upload, document_pages

# PostgreSQL driver is imported lazily inside db().
# This avoids Render startup issues where a top-level import can fail silently.
import textwrap
from contextlib import closing
from datetime import datetime, timedelta
from io import BytesIO
import math
import re
import base64
import uuid
import mimetypes
from PIL import ImageSequence, ImageOps
from reportlab.lib.utils import ImageReader

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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR") or BASE_DIR)
DB = os.path.join(DATA_DIR, "transport_mvp.db")
# To use a shared PostgreSQL database, set DATABASE_URL in your environment.
# If DATABASE_URL is not set, the app keeps using the local SQLite file above.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
if os.environ.get("RENDER") == "true" and not USE_POSTGRES:
    raise RuntimeError("Set DATABASE_URL in the Render web service's Environment settings before deploying.")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
SAVED_INVOICE_FOLDER = os.path.join(DATA_DIR, "saved_invoice_batches")
ALLOWED_EXTENSIONS = {
    "pdf", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "doc", "docx", "xls", "xlsx", "txt", "csv"
}
LOAD_STATUSES = ["Planned", "Dispatched", "In Transit", "Delivered", "Closed", "Cancelled"]

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
    .equipment-grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr)); }
    .equipment-grid > .card { min-width: 0; }
    .equipment-list { grid-column: 1 / -1; overflow-x: auto; }
    .equipment-list table { min-width: 600px; }
    .equipment-list .file-list { overflow-wrap: anywhere; }
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
    .customer-list-card { overflow-x: auto; grid-column: 1 / -1; min-width: 0; }

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
    <a href="/company-report">Company Report</a>
    <a href="/fuel">Fuel Reports</a>
  </div>
  <hr>
  {{ body|safe }}
<script>
(() => {
 const forms={'/driver-loads':'driver-report-form','/company-report':'company-report-form'};
 const path=location.pathname,id=forms[path];if(!id)return;
 const form=document.getElementById(id);if(!form)return;
 const key='transport-report-entry-v1:'+path;
 let clearing=false;
 function read(){try{return JSON.parse(sessionStorage.getItem(key)||'null');}catch(e){return null;}}
 function fields(){return [...form.elements].filter(el=>el.name&&!['file','password','submit','button'].includes(el.type));}
 function values(){const result={};fields().forEach(el=>result[el.name]=el.type==='checkbox'?el.checked:el.value);return result;}
 function queryFor(data){const params=new URLSearchParams();Object.entries(data).forEach(([name,value])=>{if(!name.startsWith('empty_miles_')&&value!==''&&typeof value==='string')params.set(name,value);});return params.toString();}
 function save(){if(clearing)return;try{const data=values();sessionStorage.setItem(key,JSON.stringify({values:data,query:queryFor(data)}));}catch(e){}}
 const url=new URL(location.href),previous=read();
 if(url.searchParams.get('clear_entry')==='1'){
   try{sessionStorage.removeItem(key);}catch(e){}history.replaceState(null,'',path);
 }else if(!url.search&&previous&&previous.values?.driver_id&&previous.values?.start&&previous.values?.end){
   location.replace(path+'?'+previous.query);return;
 }else if(previous?.values){
   const data=previous.values,current=values();
   const explicit=url.searchParams.has('driver_id')||url.searchParams.has('start')||url.searchParams.has('end');
   const same=!explicit||['driver_id','start','end'].every(name=>(current[name]||'')===(data[name]||''));
   if(same)fields().forEach(el=>{if(Object.prototype.hasOwnProperty.call(data,el.name)){if(el.type==='checkbox')el.checked=!!data[el.name];else el.value=data[el.name];}});
 }
 form.addEventListener('input',save);form.addEventListener('change',save);form.addEventListener('submit',save);
 // Mileage inputs are associated with the form but may sit outside it.
 document.addEventListener('input',event=>{if(event.target.form===form)save();});
 window.addEventListener('pagehide',save);
 document.querySelectorAll('.nav a').forEach(link=>link.addEventListener('click',save));
 form.querySelector('[data-clear-report]')?.addEventListener('click',()=>{
   clearing=true;try{sessionStorage.removeItem(key);}catch(e){}
   location.assign(path+'?clear_entry=1');
 });
})();
</script>

</body>
</html>
"""


def db():
    """Return a database connection.

    Local computer: uses SQLite when DATABASE_URL is not set.
    Render/online: uses PostgreSQL when DATABASE_URL is set.
    Supports psycopg v3 first, then psycopg2 as a fallback.
    """
    if USE_POSTGRES:
        try:
            import psycopg
            from psycopg.rows import dict_row
            return psycopg.connect(DATABASE_URL, row_factory=dict_row)
        except ImportError:
            try:
                import psycopg2
                from psycopg2.extras import RealDictCursor
                return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            except ImportError as exc:
                raise RuntimeError("PostgreSQL mode requires psycopg. Install with: pip install 'psycopg[binary]'") from exc

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


def driver_trip_total_miles(load):
    miles = total_route_miles(load["origin_zip"], load["optional_stop_1_zip"],
                              load["optional_stop_2_zip"], load["destination_zip"])
    # Match the Total Miles value displayed in Trip Details exactly.
    return float(f"{miles:.1f}") if miles is not None else None


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


FOLDER_SAVE_SCRIPT = r'''<script>
(() => {
let rememberedFolder = null;
let database = null;
const locationReady = new Promise(resolve => {
  if (typeof indexedDB === 'undefined') return resolve();
  const opening = indexedDB.open('billing-save-location', 1);
  opening.onupgradeneeded = () => opening.result.createObjectStore('settings');
  opening.onerror = () => resolve();
  opening.onsuccess = () => {
    database = opening.result;
    const reading = database.transaction('settings').objectStore('settings').get('folder');
    reading.onsuccess = () => {rememberedFolder = reading.result || null; resolve();};
    reading.onerror = () => resolve();
  };
});
async function rememberLocation(folder) {
  rememberedFolder = folder;
  if (!database) return false;
  return new Promise(resolve => {
    try {
      const transaction = database.transaction('settings', 'readwrite');
      transaction.objectStore('settings').put(folder, 'folder');
      transaction.oncomplete = () => resolve(true);
      transaction.onerror = () => resolve(false);
      transaction.onabort = () => resolve(false);
    } catch (_) {resolve(false);}
  });
}
document.querySelectorAll('form[data-billing-folder]').forEach(form => {
  const status = document.createElement('p');
  status.setAttribute('role', 'status');
  form.appendChild(status);
  const changeLocation = document.createElement('button');
  changeLocation.type = 'button';
  changeLocation.textContent = 'Change billing save location';
  form.appendChild(changeLocation);
  locationReady.then(() => {
    if (!status.textContent) status.textContent = rememberedFolder ? `Billing folders will be created inside ${rememberedFolder.name}.` : 'On the first save, approve where billing folders will be created. This location will be remembered when your browser allows it.';
  });
  changeLocation.addEventListener('click', async () => {
    if (!window.showDirectoryPicker) {
      status.textContent = 'To save directly into a folder, open this page in desktop Chrome or Edge.';
      return;
    }
    try {
      const folder = await window.showDirectoryPicker({id: 'billing-save-location', startIn: 'downloads', mode: 'readwrite'});
      const persisted = await rememberLocation(folder);
      status.textContent = `Billing folders will be created inside ${folder.name}.` + (persisted ? '' : ' This browser could not remember the location for future visits.');
    } catch (error) {
      status.textContent = error.name === 'AbortError' ? 'Save location unchanged.' : 'Could not change save location.';
    }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (form.dataset.saving === 'true') return;
    if (!window.showDirectoryPicker) {
      status.textContent = 'To save directly into a folder, open this page in desktop Chrome or Edge.';
      return;
    }
    const data = new FormData(form);
    if (event.submitter && event.submitter.name) data.set(event.submitter.name, event.submitter.value);
    if (data.get('billing_scope') !== 'all' && form.method.toLowerCase() === 'post' && !data.getAll('invoice_ids').length) {
      status.textContent = 'Select at least one invoice first.';
      return;
    }
    form.dataset.saving = 'true';
    let saved = 0;
    let total = 0;
    try {
      await locationReady;
      let chosen = rememberedFolder;
      let persisted = true;
      if (chosen) {
        const permission = {mode: 'readwrite'};
        if (await chosen.queryPermission(permission) !== 'granted' && await chosen.requestPermission(permission) !== 'granted') {
          throw new Error('Approve access to the remembered billing location, or use Change billing save location.');
        }
      } else {
        chosen = await window.showDirectoryPicker({id: 'billing-save-location', startIn: 'downloads', mode: 'readwrite'});
        persisted = await rememberLocation(chosen);
      }
      status.textContent = 'Preparing invoices...';
      const method = form.method.toUpperCase();
      let url = form.action;
      const options = {method};
      if (method === 'GET') url += '?' + new URLSearchParams(data);
      else options.body = data;
      const response = await fetch(url, options);
      if (!response.ok) throw new Error(await response.text());
      const batch = await response.json();
      total = batch.invoices.length;
      const directory = await chosen.getDirectoryHandle(batch.folder_name, {create: true});
      for (const invoice of batch.invoices) {
        status.textContent = `Saving invoice ${saved + 1} of ${total}...`;
        const pdf = await fetch(invoice.url);
        if (!pdf.ok || !(pdf.headers.get('content-type') || '').includes('application/pdf')) {
          throw new Error(`Could not open ${invoice.filename}. Please try again.`);
        }
        const blob = await pdf.blob();
        let name = invoice.filename;
        let suffix = 1;
        while (true) {
          try {
            await directory.getFileHandle(name);
            name = invoice.filename.replace(/\.pdf$/i, ` (${suffix++}).pdf`);
          } catch (error) {
            if (error.name !== 'NotFoundError') throw error;
            break;
          }
        }
        const handle = await directory.getFileHandle(name, {create: true});
        const writer = await handle.createWritable();
        try {
          await writer.write(blob);
          await writer.close();
        } catch (error) {
          await writer.abort().catch(() => {});
          throw error;
        }
        saved++;
      }
      status.textContent = `Created ${directory.name} inside ${chosen.name} and saved ${saved} separate invoice PDF(s). Existing files were kept. Invoices have not been cleared.` + (persisted ? '' : ' This browser could not remember the location for future visits.');
    } catch (error) {
      if (error.name === 'AbortError') status.textContent = saved ? `Saved ${saved} of ${total} invoice PDF(s) before cancellation.` : 'Folder selection cancelled. No files saved.';
      else status.textContent = `Saved ${saved} of ${total} invoice PDF(s). ${error.message || 'The folder could not be saved.'}`;
    } finally {
      form.dataset.saving = 'false';
    }
  });
});
})();

</script>'''


def page(body):
    return render_template_string(BASE_HTML, body=body + FOLDER_SAVE_SCRIPT)


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


def uninvoiced_loads_total():
    result = query_one("""
        SELECT COUNT(*) AS load_count, COALESCE(SUM(l.rate), 0) AS total
        FROM loads l
        WHERE COALESCE(l.is_deleted, 0) = 0 AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.load_id = l.id)
    """)
    from decimal import Decimal
    total = Decimal(str(result['total'] or 0))
    return f"""<div class='card' id='uninvoiced-loads-total' style='margin-bottom:16px;'>
      <h2>Total amount of loads not yet invoiced</h2>
      <strong class='uninvoiced-total'>${total:,.2f}</strong> &middot; {result['load_count']} load(s)
      <p class='muted'>All load statuses included. Each load leaves this total as soon as an invoice is linked to it, including invoices later cleared to the archive.</p>
    </div>"""


def closed_loads_awaiting_invoices(show_loads=False):
    """Include closed loads only until a linked invoice exists, even if archived."""
    pending = query("""
        SELECT l.id, l.load_no, l.rate, c.name AS customer_name
        FROM loads l
        LEFT JOIN customers c ON c.id = l.customer_id
        WHERE COALESCE(l.is_deleted, 0) = 0 AND l.status = 'Closed'
          AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.load_id = l.id)
        ORDER BY l.id DESC
    """)
    from decimal import Decimal
    total = sum((Decimal(str(row["rate"] or 0)) for row in pending), Decimal("0"))
    details = ""
    if show_loads:
        rows = "".join(
            f"""<tr>
              <td>{html_attr(row['load_no'] or '')}</td>
              <td>{html_attr(row['customer_name'] or '')}</td>
              <td>Closed</td><td>${Decimal(str(row['rate'] or 0)):,.2f}</td>
              <td><form method='post' action='/loads/{row['id']}/create-invoice' class='inline-form'>
                <button>Create Invoice</button>
              </form></td>
            </tr>""" for row in pending
        ) or "<tr><td colspan='5'>No closed loads awaiting invoices.</td></tr>"
        details = f"""<div style='overflow-x:auto;'><table>
          <thead><tr><th>Load #</th><th>Customer</th><th>Status</th><th>Amount</th><th>Invoice</th></tr></thead>
          <tbody>{rows}</tbody></table></div>"""
    return f"""<div class='card' style='margin-bottom:16px;' id='closed-uninvoiced'>
      <h2>Closed loads awaiting invoices</h2>
      <strong class='pending-total'>${total:,.2f}</strong> &middot; {len(pending)} load(s)
      <p class='muted'>Total load charges for closed loads not yet invoiced. Each load leaves this total as soon as an invoice is created for it.</p>
      {details}
    </div>"""


@app.route("/")
def dashboard():
    load_count = query_one("SELECT COUNT(*) AS count FROM loads WHERE COALESCE(is_deleted, 0) = 0")["count"]
    driver_count = query_one("SELECT COUNT(*) AS count FROM drivers WHERE COALESCE(is_deleted, 0) = 0")["count"]
    truck_count = query_one("SELECT COUNT(*) AS count FROM trucks")["count"]
    customer_count = query_one("SELECT COUNT(*) AS count FROM customers WHERE COALESCE(is_deleted,0)=0")["count"]
    delivered_count = query_one("SELECT COUNT(*) AS count FROM loads WHERE status='Delivered' AND COALESCE(is_deleted, 0) = 0")["count"]
    file_count = query_one("SELECT COUNT(*) AS count FROM load_files")["count"]
    invoice_count = query_one("SELECT COUNT(*) AS count FROM invoices")["count"]

    return page(f"""
    {closed_loads_awaiting_invoices()}
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

    rows = query("SELECT * FROM customers WHERE COALESCE(is_deleted,0)=0 ORDER BY name")
    tr = ""
    for r in rows:
        tr += f"""
        <tr>
            <td><textarea form='customer-edit-{r['id']}' name='name' required>{html_attr(r['name'])}</textarea></td>
            <td><textarea form='customer-edit-{r['id']}' name='address'>{html_attr(r['address'])}</textarea></td>
            <td><textarea form='customer-edit-{r['id']}' name='phone'>{html_attr(r['phone'])}</textarea></td>
            <td><textarea form='customer-edit-{r['id']}' name='fax'>{html_attr(r['fax'])}</textarea></td>
            <td><textarea form='customer-edit-{r['id']}' name='email'>{html_attr(r['email'])}</textarea></td>
            <td>
              <form id='customer-edit-{r['id']}' method='post' action='/customers/{r['id']}/edit'><button type='submit'>Save Edit</button></form>
              <form method='post' action='/customers/{r['id']}/delete' onsubmit="return confirm('Delete this customer from the customer list? Existing loads and invoices will be kept.');">
                <button type='submit' style='background:#ffdddd;'>Delete Customer</button>
              </form>
            </td>
        </tr>
        """

    if not tr:
        tr = "<tr><td colspan='6'>No customers yet.</td></tr>"

    return page(f"""
    {deleted_records_panel('customers')}
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


@app.route("/customers/<int:customer_id>/delete", methods=["POST"])
def delete_customer(customer_id):
    if not query_one("SELECT id FROM customers WHERE id=?", (customer_id,)):
        return "Customer not found", 404
    execute("UPDATE customers SET is_deleted=1 WHERE id=?", (customer_id,))
    return redirect("/customers")


@app.route("/customers/<int:customer_id>/restore", methods=["POST"])
def restore_customer(customer_id):
    if not query_one("SELECT id FROM customers WHERE id=?", (customer_id,)):
        return "Customer not found", 404
    execute("UPDATE customers SET is_deleted=0 WHERE id=?", (customer_id,))
    return redirect("/customers")


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
        "SELECT id, name, address, phone, fax, email FROM customers WHERE COALESCE(is_deleted,0)=0 AND name LIKE ? ORDER BY name LIMIT 10",
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


def deleted_records_panel(kind):
    field = "load_no" if kind == "loads" else "name"
    label = "load" if kind == "loads" else "driver"
    records = query(f"SELECT id, {field} AS label FROM {kind} WHERE is_deleted = 1 ORDER BY id DESC")
    if not records:
        return ""
    items = "".join(
        f"<li>{html_attr(row['label'] or row['id'])} "
        f"<form method='post' action='/{kind}/{row['id']}/restore' style='display:inline;'>"
        "<button type='submit' style='width:auto;'>Undo Delete</button></form></li>"
        for row in records)
    return f"<details class='card'><summary>Deleted {kind} ({len(records)})</summary><ul>{items}</ul></details>"


@app.route("/loads/<int:load_id>/delete", methods=["POST"])
def delete_load(load_id):
    if not query_one("SELECT id FROM loads WHERE id=?", (load_id,)):
        return "Load not found", 404
    execute("UPDATE loads SET is_deleted=1 WHERE id=?", (load_id,))
    return redirect("/loads")


@app.route("/loads/<int:load_id>/restore", methods=["POST"])
def restore_load(load_id):
    load=query_one('SELECT id,load_no FROM loads WHERE id=?',(load_id,))
    if not load:
        return 'Load not found',404
    try:
        validate_load_number(load['load_no'],load_id)
        execute_load_write('UPDATE loads SET is_deleted=0 WHERE id=?',(load_id,))
    except ValueError as exc:
        return page(f"<p role='alert'>{html_attr(str(exc))}</p><a href='/loads'>Back to Loads</a>"),400
    return redirect('/loads')


@app.route("/drivers/<int:driver_id>/delete", methods=["POST"])
def delete_driver(driver_id):
    if not query_one("SELECT id FROM drivers WHERE id=?", (driver_id,)):
        return "Driver not found", 404
    execute("UPDATE drivers SET is_deleted=1 WHERE id=?", (driver_id,))
    return redirect("/drivers")


@app.route("/drivers/<int:driver_id>/restore", methods=["POST"])
def restore_driver(driver_id):
    if not query_one("SELECT id FROM drivers WHERE id=?", (driver_id,)):
        return "Driver not found", 404
    execute("UPDATE drivers SET is_deleted=0 WHERE id=?", (driver_id,))
    return redirect("/drivers")


@app.route("/drivers", methods=["GET", "POST"])
def drivers():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            raw_card = request.form.get("fuel_card", "").strip()
            try:
                from fuel_reports import card_suffix
                from driver_statement import amount
                from decimal import Decimal
                card = card_suffix(raw_card) if raw_card else None
                pay_rate = amount(request.form.get("pay_rate_per_mile"), optional=True, nonnegative=True)
                if pay_rate is not None and pay_rate != pay_rate.quantize(Decimal('.0001')):
                    raise ValueError("Enter a pay rate with no more than four decimal places.")
            except ValueError as exc:
                return page(f"<p>{html_attr(str(exc))}</p><a href='/drivers'>Back to Drivers</a>"), 400
            if card and query_one("SELECT id FROM drivers WHERE fuel_card=?", (card,)):
                return page("<p>That fuel card is already assigned to another driver.</p><a href='/drivers'>Back to Drivers</a>"), 400
            execute("INSERT INTO drivers (name, fuel_card, pay_rate_per_mile) VALUES (?, ?, ?)", (name, card, str(pay_rate) if pay_rate is not None else None))
        return redirect("/drivers")

    rows = query("SELECT * FROM drivers WHERE COALESCE(is_deleted, 0) = 0 ORDER BY name")

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
            {fuel_driver_fields(r['id'], r['fuel_card'])}
            {statement_driver_fields(r)}
          </td>
          <td>
            <form method='post' action='/drivers/{r['id']}/upload' enctype='multipart/form-data'>
              <input type='file' name='file' required>
              <button>Upload Driver File</button>
            </form>
            {file_html}
          </td>
          <td>
            <form method='post' action='/drivers/{r['id']}/delete' onsubmit="return confirm('Delete this driver from the driver list? Existing load history will be kept.');">
              <button type='submit' style='background:#ffdddd;'>Delete Driver</button>
            </form>
          </td>
        </tr>
        """

    if not tr:
        tr = "<tr><td colspan='3'>No drivers yet.</td></tr>"

    return page(f"""
    {deleted_records_panel('drivers')}
    <div class='grid equipment-grid'>
      <div class='card'>
        <h1>Drivers</h1>
        <form method='post'>
          <input name='name' placeholder='Driver name' required>
          <label>Driver pay per mile ($)<input name='pay_rate_per_mile' type='number' min='0' step='0.0001' placeholder='Optional: enter agreed pay rate'></label>
          <label>Fuel card — last 4 or 5 digits<input name='fuel_card' inputmode='numeric' pattern='[0-9]{{4,5}}' maxlength='5' placeholder='Optional: 01531'></label>
          <button>Add</button>
        </form>
      </div>
      <div class='card equipment-list'>
        <h2>Driver List</h2>
        <table>
          <tr><th>Name</th><th>Files</th><th>Actions</th></tr>
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
                  <span>{html_attr(f['original_name'])}</span> <a href='/truck-files/{f['id']}/view' target='_blank' rel='noopener'>View</a> <a href='/truck-files/{f['id']}/view?download=1'>Download</a>
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
                  <span>{html_attr(f['original_name'])}</span> <a href='/trailer-files/{f['id']}/view' target='_blank' rel='noopener'>View</a> <a href='/trailer-files/{f['id']}/view?download=1'>Download</a>
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
    <div class='grid equipment-grid'>
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
      <div class='card equipment-list'>
        <h2>Truck List</h2>
        <table>
          <tr><th>Truck / Unit</th><th>Files</th><th>Actions</th></tr>
          {truck_tr}
        </table>
      </div>
      <div class='card equipment-list'>
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


def validate_load_number(value, exclude_id=0):
    number=str(value or '').strip()
    if not number:
        raise ValueError('Enter an internal load number.')
    if query_one("SELECT id FROM loads WHERE lower(trim(load_no))=lower(?) AND id!=?",(number,exclude_id)):
        raise ValueError(f'Internal load number {number} already exists, including deleted load history. Enter a different number.')
    return number


def execute_load_write(sql, params, confirmation=None):
    try:
        if confirmation is None:
            execute(sql,params)
        else:
            # Create the load and its original confirmation in one transaction.
            original_name, data = confirmation
            with closing(db()) as conn:
                cur=conn.cursor()
                try:
                    cur.execute(normalize_sql(sql),params)
                    cur.execute(normalize_sql('SELECT id FROM loads WHERE load_no=?'),(params[0],))
                    row=cur.fetchone()
                    load_id=row['id']
                    stored_name=f"{load_id}_{uuid.uuid4().hex}_{secure_filename(original_name)}"
                    now=datetime.now().isoformat()
                    cur.execute(normalize_sql("INSERT INTO billing_attachments (stored_name,original_name,load_id,uploaded_at,data_base64) VALUES (?,?,?,?,?)"),
                        (stored_name,original_name,load_id,now,base64.b64encode(data).decode('ascii')))
                    cur.execute(normalize_sql("INSERT INTO load_files (load_id,original_name,stored_name,uploaded_at) VALUES (?,?,?,?)"),
                        (load_id,original_name,stored_name,now))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
    except Exception as exc:
        if isinstance(exc,sqlite3.IntegrityError) or getattr(exc,'pgcode',None)=='23505' or getattr(exc,'sqlstate',None)=='23505':
            raise ValueError('That internal load number was just used by another load. Refresh and choose a different number.') from exc
        raise


def next_internal_load_number():
    rows=query('SELECT load_no FROM loads ORDER BY id DESC')
    used={str(r['load_no'] or '').strip().lower() for r in rows}
    for row in rows:
        value=str(row['load_no'] or '').strip()
        if re.fullmatch(r'[0-9]+',value):
            candidate=str(int(value)+1).zfill(len(value))
            while candidate.lower() in used:
                candidate=str(int(candidate)+1).zfill(len(value))
            return candidate
    return ''


def parse_owner_operator_amount(value):
    from driver_statement import amount
    from decimal import Decimal
    payout=amount(value,optional=True,nonnegative=True)
    if payout is not None and payout!=payout.quantize(Decimal('.01')):
        raise ValueError('Enter the owner operator amount with no more than two decimal places.')
    return str(payout.quantize(Decimal('.01'))) if payout is not None else None


@app.route("/loads", methods=["GET", "POST"])

def loads():
    if request.method == "POST":
        try:
            loaded_miles = mileage_input(request.form.get("loaded_miles"))
            empty_miles = mileage_input(request.form.get("empty_miles"))
            owner_operator_amount = parse_owner_operator_amount(request.form.get("owner_operator_amount"))
            load_number = validate_load_number(request.form.get("load_no"))
            confirmation=None
            source=request.files.get('confirmation_file')
            if source and source.filename:
                if request.form.get('confirmation_reviewed')!='1':
                    raise ValueError('Review the uploaded confirmation before creating the load.')
                source_data=read_upload(source)
                try:
                    document_pages(source_data)
                except ValueError:
                    raise
                except Exception as exc:
                    raise ValueError('Could not read the confirmation. Use an unlocked PDF or clear photo.') from exc
                confirmation=(source.filename,source_data)
            execute_load_write(
                """
                INSERT INTO loads (
                    load_no, broker_load_no, trailer_id, optional_stop_1, optional_stop_1_zip, optional_stop_2, optional_stop_2_zip,
                    pickup_location_name, pickup_address, origin, origin_zip,
                    delivery_location_name, delivery_address, destination, destination_zip,
                    pickup_date, pickup_time, delivery_date, delivery_time, rate, status, driver_id, truck_id, customer_id, loaded_miles, empty_miles, owner_operator_amount, pickup_number
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    load_number,
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
                    loaded_miles, empty_miles, owner_operator_amount, request.form.get("pickup_number", "").strip(),
                ),
                confirmation=confirmation,
            )
            return redirect("/loads")

        except ValueError as exc:
            return page(f"<div class='card'><h1>Load not saved</h1><p role='alert'>{html_attr(str(exc))}</p><a href='/loads'>Back to Loads</a></div>"),400

    drivers = query("SELECT * FROM drivers WHERE COALESCE(is_deleted, 0) = 0 ORDER BY name")
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
        WHERE COALESCE(l.is_deleted, 0) = 0
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

    def detail(label,value):
        if value is None or str(value).strip()=='':
            return ''
        return f"<div class='load-detail'><strong>{html_attr(label)}:</strong> {html_attr(value)}</div>"

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
          <td data-label='Load / References'>
            {detail('Load #',l['load_no'])}{detail('Broker Load #',l['broker_load_no'])}
            {detail('PU #',l['pickup_number'])}{detail('Customer',l['customer_name'])}{invoice_cell}
          </td>
          <td data-label='Pickup'>
            {detail('Pickup name',l['pickup_location_name'])}{detail('Address',l['pickup_address'])}
            {detail('City / State',l['origin'])}{detail('ZIP',l['origin_zip'])}
            {detail('Pickup date',l['pickup_date'])}{detail('Pickup time',l['pickup_time'])}
          </td>
          <td data-label='Delivery'>
            {detail('Delivery name',l['delivery_location_name'])}{detail('Address',l['delivery_address'])}
            {detail('City / State',l['destination'])}{detail('ZIP',l['destination_zip'])}
            {detail('Delivery date',l['delivery_date'])}{detail('Delivery time',l['delivery_time'])}
          </td>
          <td data-label='Stops / Equipment'>
            {detail('Stop 1',l['optional_stop_1'])}{detail('Stop 1 ZIP',l['optional_stop_1_zip'])}
            {detail('Stop 2',l['optional_stop_2'])}{detail('Stop 2 ZIP',l['optional_stop_2_zip'])}
            {detail('Driver',l['driver_name'])}{detail('Truck',l['truck_unit'])}
            {detail('Trailer',l['trailer_no'] or l['load_trailer_no'])}
          </td>
          <td data-label='Amounts / Status'>
            {detail('Rate','$'+format(float(l['rate'] or 0),',.2f'))}
            {detail('Owner Operator Amount',('$'+format(float(l['owner_operator_amount']),',.2f')) if l['owner_operator_amount'] is not None else 'Not set')}
            {detail('Paid empty miles',l['empty_miles'])}{detail('Status',l['status'])}
            <form method='post' action='/update-status/{l['id']}' class='inline-form'>
              <select name='status' aria-label='Status for load {l['id']}'>
                {''.join(f"<option value='{status}' {'selected' if status==l['status'] else ''}>{status}</option>" for status in LOAD_STATUSES)}
              </select><button>Update</button>
            </form>
          </td>
          <td data-label='Paperwork / Actions'>
            <a href='/loads/{l['id']}/edit'>Edit</a>
            <form method='post' action='/loads/{l['id']}/delete' onsubmit="return confirm('Delete this load from the load board? Existing invoices and paperwork will be kept.');">
              <button type='submit' style='background:#ffdddd;'>Delete Load</button>
            </form>
            <div class='upload-box'><form method='post' action='/loads/{l['id']}/upload' enctype='multipart/form-data'>
              <input type='file' name='file' aria-label='Paperwork for load {l['id']}' required><button>Upload</button>
            </form>{file_html}</div>
          </td>
        </tr>
        """

    if not tr:
        tr = "<tr><td colspan='6'>No loads yet.</td></tr>"

    return page(f"""
    {deleted_records_panel('loads')}
    <style>
      .loads-page {{display:grid;grid-template-columns:minmax(0,1fr);gap:16px;max-width:100%;}}
      .loads-page .card,.loads-page .form-grid>* {{min-width:0;max-width:100%;}}
      .loads-page .form-grid {{grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));}}
      #load_board_table {{width:100%;table-layout:fixed;font-size:12px;}}
      #load_board_table th,#load_board_table td {{overflow-wrap:anywhere;word-break:normal;padding:8px;vertical-align:top;}}
      #load_board_table input,#load_board_table select,#load_board_table button {{max-width:100%;min-width:0;width:100%;}}
      #load_board_table .upload-box {{min-width:0;max-width:100%;}}
      #load_board_table ul {{padding-left:15px;margin:6px 0;}}
      #load_board_table .inline-form {{display:block;}}
      .load-detail {{margin-bottom:6px;white-space:normal;}}
      @media(max-width:950px) {{
        #load_board_table thead {{display:none;}}
        #load_board_table,#load_board_table tbody,#load_board_table tr,#load_board_table td {{display:block;box-sizing:border-box;width:100%;}}
        #load_board_table tr[data-load-row] {{margin-bottom:18px;border:1px solid #ddd;border-radius:8px;overflow:hidden;}}
        #load_board_table td[data-label]::before {{content:attr(data-label);display:block;font-weight:bold;font-size:14px;margin-bottom:8px;}}
      }}
    </style>
    <div class='loads-page'>
      <div class='card'>
        <h1>Loads</h1>
        {uninvoiced_loads_total()}
        {closed_loads_awaiting_invoices()}
        <form method='post' id='load-form' enctype='multipart/form-data'>
          {CONFIRMATION_UI}
          <div class='form-grid'>
            <label>Internal Load # (editable)<input name='load_no' placeholder='Load # / Internal Load #' value='{html_attr(next_internal_load_number())}' required></label>
            <input name='broker_load_no' placeholder='Broker Load #'>
            <label>PU #<input name='pickup_number' maxlength='150' placeholder='Pickup number (optional)'></label>
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
            <label>Owner Operator Amount ($)<input name='owner_operator_amount' type='number' min='0' max='100000000' step='0.01' placeholder='Agreed payout for this load (optional)'></label>
            <label>Paid empty miles<input type='number' min='0' step='0.01' name='empty_miles'></label>
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
        <label>Search all load information<input id='load-board-search' placeholder='Load #, PU #, customer, driver, address...'></label>
        <table id='load_board_table'>
          <thead><tr><th>Load / References</th><th>Pickup</th><th>Delivery</th><th>Stops / Equipment</th><th>Amounts / Status</th><th>Paperwork / Actions</th></tr>
          <tr>{''.join(f"<th><input class='load-filter' data-col='{n}' placeholder='Search' aria-label='Search {name}'></th>" for n,name in enumerate(['load references','pickup','delivery','stops and equipment','amounts and status','paperwork']))}</tr></thead>
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
          const search=document.getElementById('load-board-search').value.trim().toLowerCase();
          const show = row.innerText.toLowerCase().includes(search) && filters.every(f => (cells[f.col]?.innerText || '').toLowerCase().includes(f.value));
          row.style.display = show ? '' : 'none';
        }});
      }}

      document.getElementById('load-board-search').addEventListener('input',filterLoadBoard);
      document.querySelectorAll('.load-filter').forEach(input => {{
        input.addEventListener('input', filterLoadBoard);
      }});
    </script>
    """)


@app.route("/loads/<int:load_id>/edit", methods=["GET", "POST"])
def edit_load(load_id):
    load = query_one("SELECT * FROM loads WHERE id = ? AND COALESCE(is_deleted, 0) = 0", (load_id,))
    if not load:
        return "Load not found", 404

    if request.method == "POST":
        try:
            loaded_miles = mileage_input(request.form.get("loaded_miles", load["loaded_miles"]))
            empty_miles = mileage_input(request.form.get("empty_miles", load["empty_miles"]))
            owner_operator_amount = parse_owner_operator_amount(request.form.get("owner_operator_amount", load["owner_operator_amount"]))
            load_number = validate_load_number(request.form.get("load_no"),load_id)
            execute_load_write(
                """
                UPDATE loads
                SET load_no = ?, broker_load_no = ?, load_trailer_no = ?, optional_stop_1 = ?, optional_stop_1_zip = ?, optional_stop_2 = ?, optional_stop_2_zip = ?,
                    pickup_location_name = ?, pickup_address = ?, origin = ?, origin_zip = ?,
                    delivery_location_name = ?, delivery_address = ?, destination = ?, destination_zip = ?,
                    pickup_date = ?, pickup_time = ?, delivery_date = ?, delivery_time = ?, rate = ?, status = ?,
                    driver_id = ?, truck_id = ?, trailer_id = ?, customer_id = ?, loaded_miles = ?, empty_miles = ?, owner_operator_amount = ?, pickup_number = ?
                WHERE id = ?
                """,
                (
                    load_number,
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
                    loaded_miles, empty_miles, owner_operator_amount, request.form.get("pickup_number", load["pickup_number"] or "").strip(),
                    load_id,
                ),
            )
            return redirect("/loads")

        except ValueError as exc:
            return page(f"<div class='card'><h1>Load not saved</h1><p role='alert'>{html_attr(str(exc))}</p><a href='/loads'>Back to Loads</a></div>"),400

    customers = query("SELECT * FROM customers WHERE COALESCE(is_deleted,0)=0 OR id=? ORDER BY name", (load["customer_id"],))
    drivers = query("SELECT * FROM drivers WHERE COALESCE(is_deleted, 0) = 0 OR id = ? ORDER BY name", (load["driver_id"],))
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
          <label>PU #<input name='pickup_number' maxlength='150' value='{html_attr(load['pickup_number'])}' placeholder='Pickup number (optional)'></label>
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
          <label>Owner Operator Amount ($)<input name='owner_operator_amount' type='number' min='0' max='100000000' step='0.01' value='{html_attr(load["owner_operator_amount"])}' placeholder='Agreed payout for this load (optional)'></label>
          <label>Paid empty miles<input type='number' min='0' step='0.01' name='empty_miles' value='{html_attr(load["empty_miles"])}'></label>
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
    with open(save_path, "rb") as saved:
        execute("""INSERT INTO billing_attachments
                   (stored_name, original_name, load_id, uploaded_at, data_base64)
                   VALUES (?, ?, ?, ?, ?)""",
                (stored_name, uploaded_file.filename, load_id, datetime.now().isoformat(),
                 base64.b64encode(saved.read()).decode("ascii")))

    execute(
        "INSERT INTO load_files (load_id, original_name, stored_name, uploaded_at) VALUES (?, ?, ?, ?)",
        (load_id, uploaded_file.filename, stored_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return redirect("/loads")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    stored = query_one("SELECT original_name, data_base64 FROM billing_attachments WHERE stored_name = ?", (filename,))
    if stored:
        return send_file(BytesIO(base64.b64decode(stored["data_base64"])),
                         download_name=stored["original_name"],
                         mimetype=mimetypes.guess_type(stored["original_name"])[0] or "application/octet-stream")
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
    with open(save_path, "rb") as saved:
        execute("""INSERT INTO billing_attachments
            (stored_name, original_name, uploaded_at, data_base64) VALUES (?, ?, ?, ?)""",
            (stored_name, uploaded_file.filename, datetime.now().isoformat(),
             base64.b64encode(saved.read()).decode("ascii")))


    execute(
        "INSERT INTO truck_files (truck_id, original_name, stored_name, uploaded_at) VALUES (?, ?, ?, ?)",
        (truck_id, uploaded_file.filename, stored_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return redirect("/trucks")


@app.route("/truck-files/<int:file_id>/view")
def view_truck_file(file_id):
    record = query_one("SELECT * FROM truck_files WHERE id = ?", (file_id,))
    if not record:
        return "File not found.", 404
    stored = query_one("SELECT data_base64 FROM billing_attachments WHERE stored_name = ?", (record["stored_name"],))
    options = dict(download_name=record["original_name"],
                   as_attachment=request.args.get("download") == "1",
                   mimetype=mimetypes.guess_type(record["original_name"])[0] or "application/octet-stream")
    if stored:
        return send_file(BytesIO(base64.b64decode(stored["data_base64"])), **options)
    path = os.path.join(app.config["UPLOAD_FOLDER"], record["stored_name"])
    if not os.path.isfile(path):
        return page("<h1>File unavailable</h1><p>This older upload is no longer on the server. Please upload it again.</p><a href='/trucks'>Back to Trucks and Trailers</a>"), 404
    return send_file(path, **options)


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
    with open(save_path, "rb") as saved:
        execute("""INSERT INTO billing_attachments
            (stored_name, original_name, uploaded_at, data_base64) VALUES (?, ?, ?, ?)""",
            (stored_name, uploaded_file.filename, datetime.now().isoformat(),
             base64.b64encode(saved.read()).decode("ascii")))


    execute(
        "INSERT INTO trailer_files (trailer_id, original_name, stored_name, uploaded_at) VALUES (?, ?, ?, ?)",
        (trailer_id, uploaded_file.filename, stored_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return redirect("/trucks")


@app.route("/trailer-files/<int:file_id>/view")
def view_trailer_file(file_id):
    record = query_one("SELECT * FROM trailer_files WHERE id = ?", (file_id,))
    if not record:
        return "File not found.", 404
    stored = query_one("SELECT data_base64 FROM billing_attachments WHERE stored_name = ?", (record["stored_name"],))
    options = dict(download_name=record["original_name"],
                   as_attachment=request.args.get("download") == "1",
                   mimetype=mimetypes.guess_type(record["original_name"])[0] or "application/octet-stream")
    if stored:
        return send_file(BytesIO(base64.b64decode(stored["data_base64"])), **options)
    path = os.path.join(app.config["UPLOAD_FOLDER"], record["stored_name"])
    if not os.path.isfile(path):
        return page("<h1>File unavailable</h1><p>This older upload is no longer on the server. Please upload it again.</p><a href='/trucks'>Back to Trucks and Trailers</a>"), 404
    return send_file(path, **options)


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


def new_invoice_number_for_load(load):
    number = str(load["load_no"] or "").strip()
    if not number:
        raise ValueError("Enter an internal load number on the load before creating its invoice.")
    if query_one("SELECT id FROM invoices WHERE invoice_no=?", (number,)):
        raise ValueError("An invoice already uses this internal load number. Open the existing invoice or correct the internal load number before creating another invoice.")
    return number


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
        if load_id:
            linked_load = query_one("SELECT id,load_no FROM loads WHERE id=? AND COALESCE(is_deleted,0)=0", (load_id,))
            if not linked_load:
                return "Select an existing load.", 400
            try:
                invoice_no = new_invoice_number_for_load(linked_load)
            except ValueError as exc:
                return page(f"<p>{html_attr(str(exc))}</p><a href='/invoices'>Back to Invoices</a>"), 400
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

    customers = query("SELECT * FROM customers WHERE COALESCE(is_deleted,0)=0 ORDER BY name")
    loads_list = query("""
        SELECT l.*, c.name AS customer_name, c.address AS customer_address, t.unit_no AS truck_unit, tr.trailer_no AS trailer_no
        FROM loads l
        LEFT JOIN customers c ON c.id = l.customer_id
        LEFT JOIN trucks t ON t.id = l.truck_id
        LEFT JOIN trailers tr ON tr.id = l.trailer_id
        WHERE COALESCE(l.is_deleted, 0) = 0
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
            f"data-internalno='{html_attr(l['load_no'])}' "
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
        paperwork = invoice_packet_attachments(inv)
        paperwork_links = "".join(
            f"<li><a href='/uploads/{html_attr(item['stored_name'])}' target='_blank'>{html_attr(item['original_name'])}</a></li>"
            for item in paperwork
        ) or "<li>No paperwork attached yet.</li>"

        invoice_cards += f"""
        <div class='invoice-wrap' id='invoice-{inv["id"]}' style='margin-top:16px;'>
          <div class='action-links'>
  <label><input style='width:auto;' type='checkbox' form='billing-batch-form' name='invoice_ids' value='{inv["id"]}'> Include in billing batch</label>
  <details><summary>Attached paperwork ({len(paperwork)})</summary><ul>{paperwork_links}</ul></details>
  {invoice_load_link_form(inv, loads_list, '/invoices')}
  <form method='post' action='/invoices/{inv["id"]}/upload' enctype='multipart/form-data'>
    <label>Attach invoice paperwork (PDF or image)<input type='file' name='file' accept='.pdf,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff' required></label>
    <button>Upload Paperwork</button>
  </form>
  <a href='/invoices/{inv["id"]}/print' target='_blank'>Open / Print with Paperwork</a>
  <a href='/invoices/{inv["id"]}/pdf?download=1' target='_blank'>Download Invoice Packet</a>

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
    {closed_loads_awaiting_invoices(show_loads=True)}
    <div class='grid'>
      <div class='card'>
        <h1>Invoices</h1>
        <form method='post' id='invoice-form'>
          <div class='form-grid'>
            <label>Invoice # (uses the linked load's internal number)<input name='invoice_no' id='invoice_no' placeholder='Invoice #' value='{next_invoice_no}' required></label>
            <input type='date' name='invoice_date' id='invoice_date' value='{today_ymd()}' required>
            <input name='terms' id='terms' placeholder='Terms' value='NET 30'>
            <input type='date' name='due_date' id='due_date' value='{plus_30_days_ymd()}' required>

            <select name='customer_id' id='invoice_customer_id'>
              {customer_opts}
            </select>

            <textarea name='bill_to_name' id='bill_to_name' placeholder='Bill To Name' rows='2'></textarea>
            <textarea name='bill_to_address' id='bill_to_address' placeholder='Bill To Address' rows='3'></textarea>

            <label for='invoice_load_id'>Linked load for paperwork</label>
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

    <div class='card' style='margin-top:16px;'>
      <h2>Billing Batch</h2>
      <p>Create a folder using your batch name and save a separate PDF for each invoice and its paperwork. Your billing save location is remembered after the first approval. Includes active invoices; cleared invoices remain in the archive.</p>
      <form id='billing-batch-form' data-billing-folder method='post' action='/invoices/billing-batch'>
        <label>Batch folder name<input name='batch_name' required placeholder='Example: Evans October Billing'></label>
        <button name='billing_scope' value='all'>Save All Billing to Folder</button>
        <button name='billing_scope' value='selected'>Save Selected Billing to Folder</button>
      </form>
      <p class='muted'>Downloading does not clear invoices. Clear them after you finish billing.</p>
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
          document.getElementById('invoice_no').value = opt.dataset.internalno || '';
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
    return redirect(f"/invoices/{invoice_id}/pdf")


def invoice_load_link_form(inv, choices, return_to):
    options = "<option value=''>No linked load</option>" + "".join(
        f"<option value='{load['id']}' {'selected' if load['id'] == inv['load_id'] else ''}>"
        f"{html_attr(load['load_no'] or 'No load number')} | Broker: {html_attr(load['broker_load_no'] or '')} | ID {load['id']}</option>"
        for load in choices
    )
    return f"""<form method='post' action='/invoices/{inv['id']}/link-load'>
      <input type='hidden' name='return_to' value='{return_to}'>
      <label>Linked load for paperwork<select name='load_id'>{options}</select></label>
      <button>Save Load Link</button>
    </form>"""


@app.route('/invoices/<int:invoice_id>/link-load', methods=['POST'])
def link_invoice_load(invoice_id):
    if not query_one('SELECT id FROM invoices WHERE id=?', (invoice_id,)):
        return 'Invoice not found', 404
    load_id = request.form.get('load_id', '').strip()
    if load_id and (not load_id.isdigit() or not query_one('SELECT id FROM loads WHERE id=? AND COALESCE(is_deleted, 0) = 0', (load_id,))):
        return 'Select an existing load.', 400
    execute('UPDATE invoices SET load_id=? WHERE id=?', (int(load_id) if load_id else None, invoice_id))
    return_to = request.form.get('return_to')
    if return_to == '/invoice-archive':
        return redirect('/invoice-archive?show_cleared=1')
    return redirect('/invoices')


def invoice_packet_attachments(inv):
    attachments = query("""
        SELECT DISTINCT f.original_name, f.stored_name, f.uploaded_at
        FROM load_files f
        WHERE f.load_id = ? OR f.id IN (
            SELECT load_file_id FROM invoice_files WHERE invoice_id = ?
        )
        ORDER BY f.uploaded_at, f.stored_name
    """, (inv["load_id"], inv["id"]))
    attachments += query("""
        SELECT original_name, stored_name, uploaded_at FROM billing_attachments
        WHERE invoice_id = ? ORDER BY uploaded_at, stored_name
    """, (inv["id"],))
    return attachments


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
    attachments = invoice_packet_attachments(inv)
    for attachment in attachments:
        name = attachment["original_name"] or attachment["stored_name"]
        try:
            data = read_billing_attachment(attachment["stored_name"])
            if data is None:
                raise ValueError("File is missing. Please upload this document again.")
            if name.lower().endswith('.pdf'):
                reader = PdfReader(BytesIO(data))
                if reader.is_encrypted and not reader.decrypt(""):
                    raise ValueError("Password-protected PDF. Upload an unlocked copy.")
                if not reader.pages:
                    raise ValueError("PDF has no pages.")
                # Validate the entire attachment before adding any of its pages.
                pages = list(reader.pages)
                for extra_page in pages:
                    writer.add_page(extra_page)
            else:
                image_buffer = BytesIO()
                image_pdf = canvas.Canvas(image_buffer, pagesize=letter)
                with Image.open(BytesIO(data)) as image:
                    for frame in ImageSequence.Iterator(image):
                        frame = ImageOps.exif_transpose(frame).convert("RGB")
                        iw, ih = frame.size
                        scale = min((letter[0] - 72) / iw, (letter[1] - 72) / ih)
                        dw, dh = iw * scale, ih * scale
                        image_pdf.drawImage(ImageReader(frame), (letter[0] - dw) / 2,
                                            (letter[1] - dh) / 2, width=dw, height=dh)
                        image_pdf.showPage()
                image_pdf.save()
                image_buffer.seek(0)
                for extra_page in PdfReader(image_buffer).pages:
                    writer.add_page(extra_page)
            attached_count += 1
        except Exception as error:
            app.logger.warning("Invoice %s attachment %s could not be included: %s", inv["id"], name, error)
            add_packet_notice(writer, inv, name,
                              "Document could not be included. It may be missing, unreadable, password-protected, or an unsupported format. Upload a readable PDF or image before billing.")
    if include_no_docs_note and not attachments:
        if not inv['load_id']:
            add_packet_notice(writer, inv, "Invoice has no linked load",
                              "On Invoices or Invoice Archive, choose the load under Linked load for paperwork and click Save Load Link. Files uploaded on Loads appear only when that load is linked to this invoice. You can also upload paperwork directly to the invoice.")
        else:
            add_packet_notice(writer, inv, "No paperwork attached to the linked load",
                              f"This invoice uses load ID {inv['load_id']}. Upload paperwork to that load or correct Linked load for paperwork on the invoice, then reopen the packet.")
    return attached_count


def add_packet_notice(writer, inv, name, message):
    buffer = BytesIO()
    notice = canvas.Canvas(buffer, pagesize=letter)
    notice.setFont("Helvetica-Bold", 16)
    notice.drawString(50, 740, "Attachment needs attention")
    notice.setFont("Helvetica", 11)
    y = 710
    for line in textwrap.wrap(f"Invoice {inv['invoice_no'] or inv['id']}: {name}", 85):
        notice.drawString(50, y, line)
        y -= 16
    for line in textwrap.wrap(message, 85):
        notice.drawString(50, y, line)
        y -= 16
    notice.save()
    buffer.seek(0)
    writer.add_page(PdfReader(buffer).pages[0])


def read_billing_attachment(stored_name):
    stored = query_one("SELECT data_base64 FROM billing_attachments WHERE stored_name = ?", (stored_name,))
    if stored:
        return base64.b64decode(stored["data_base64"])
    # Older uploads may still exist on the application's local disk.
    if os.path.basename(stored_name) != stored_name:
        return None
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
    if os.path.isfile(file_path):
        with open(file_path, "rb") as file:
            return file.read()
    return None


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

    writer = PdfWriter()
    add_invoice_packet_pages(writer, inv, include_no_docs_note=True)
    output_buffer = BytesIO()
    writer.write(output_buffer)
    output_buffer.seek(0)
    return send_file(output_buffer, as_attachment=request.args.get("download") == "1",
                     download_name=f"invoice_{secure_filename(str(inv['invoice_no'] or invoice_id))}_packet.pdf",
                     mimetype="application/pdf")


@app.route("/driver-loads")
def driver_report():
    driver_id = request.args.get("driver_id", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")

    drivers = query("SELECT * FROM drivers WHERE COALESCE(is_deleted,0)=0 OR id=? ORDER BY name,id", (int(driver_id) if driver_id.isdigit() else -1,))
    opts = "<option value=''>Select driver</option>" + "".join(
        f"<option value='{d['id']}' {selected(d['id'], driver_id)}>{html_attr(d['name'])}{' — Deleted profile (historical)' if d['is_deleted'] else ''}</option>" for d in drivers
    )

    rows = "<tr><td colspan='13'>Select a driver and run the report.</td></tr>"
    mileage_rows = "<tr><td colspan='5'>Select a driver and dates, then click Load Trips / Enter Miles.</td></tr>"
    if driver_id:
        sql = """
        SELECT l.*, d.name AS driver_name, c.name AS customer_name
        FROM loads l
        LEFT JOIN drivers d ON d.id = l.driver_id
        LEFT JOIN customers c ON c.id = l.customer_id
        WHERE l.driver_id = ? AND COALESCE(l.is_deleted, 0) = 0
        """
        params = [driver_id]

        if start:
            sql += " AND COALESCE(NULLIF(l.delivery_date,''), l.pickup_date) >= ?"
            params.append(start)
        if end:
            sql += " AND COALESCE(NULLIF(l.delivery_date,''), l.pickup_date) <= ?"
            params.append(end)

        sql += " ORDER BY COALESCE(NULLIF(l.delivery_date,''), l.pickup_date) DESC, l.id DESC"
        data = query(sql, tuple(params))

        report_rows = []
        mileage_items = []
        for r in data:
            total_miles = driver_trip_total_miles(r)
            empty_value = r["empty_miles"] if r["empty_miles"] is not None else "0"
            mileage_hint = "From Trip Details Total Miles" if total_miles is not None else "Trip Details miles unavailable; check the trip ZIP codes"
            mileage_items.append(
                f"<tr><td>{html_attr(r['load_no'])}</td><td>{html_attr(r['delivery_date'] or r['pickup_date'])}</td>"
                f"<td>{html_attr(r['origin'])} to {html_attr(r['destination'])}<br><small>{mileage_hint}</small></td>"
                f"<td>{format_miles(total_miles)}</td>"
                f"<td><input form='driver-report-form' type='number' min='0' step='0.01' style='min-width:90px;' name='empty_miles_{r['id']}' value='{html_attr(empty_value)}' aria-label='Empty miles for load {r['id']}'></td></tr>")
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

        mileage_rows = "".join(mileage_items) or "<tr><td colspan='5'>No loads found for this driver and date range.</td></tr>"
        rows = "".join(report_rows) or "<tr><td colspan='13'>No loads found for that driver and date range.</td></tr>"

    current_driver = next((d for d in drivers if str(d['id']) == str(driver_id)), None)
    profile_rate = current_driver['pay_rate_per_mile'] if current_driver else None
    rate_message = f"Driver pay rate: ${float(profile_rate):.4f} per mile (from driver profile)." if profile_rate is not None else "Select a driver with a saved pay rate per mile."
    saved_message = "Miles saved." if request.args.get('miles_saved') == 'yes' else ""
    return page(f"""
    <div class='card'>
      <h1>Driver Report</h1>
      <form id='driver-report-form' method='post' action='/driver-loads/pdf'>
        <select name='driver_id' required>{opts}</select>
        <label>Start date</label>
        <input type='date' name='start' value='{html_attr(start)}' required>
        <label>End date</label>
        <input type='date' name='end' value='{html_attr(end)}' required>
        {statement_report_controls()}
        <button>Run / Download PDF</button>
        <button formaction='/driver-loads' formmethod='get'>Load Trips / Enter Miles</button>
        <button formaction='/driver-loads/miles' formmethod='post'>Save Miles</button>
        <button type='button' data-clear-report>Clear Entry</button>
      </form>
      <p><strong>{rate_message}</strong></p><p role='status'>{saved_message}</p>
      <p>Load trips for the selected dates, enter empty miles below, then Save Miles or Run / Download PDF. Empty miles default to 0; enter additional empty miles when applicable. Loaded miles automatically come from Trip Details Total Miles.</p>
      <h2>Paid Miles for This Report</h2>
      <div style='overflow-x:auto;'><table><tr><th>Trip #</th><th>Date</th><th>Route</th><th>Loaded Miles</th><th>Empty Miles</th></tr>{mileage_rows}</table></div>
      <h2>Trip Details</h2>
      <div style='overflow-x:auto;'><table>
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
          <th>Load Amount</th>
          <th>Status</th>
        </tr>
        {rows}
      </table></div>
    </div>
    {fuel_report_section(driver_id, start, end)}
    """)


@app.route("/invoices/<int:invoice_id>/upload", methods=["POST"])
def upload_invoice_paperwork(invoice_id):
    if not query_one("SELECT id FROM invoices WHERE id=?", (invoice_id,)):
        return "Invoice not found", 404
    file = request.files.get("file")
    if not file or not file.filename or not file.filename.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")):
        return "Choose a PDF or image file.", 400
    data = file.read()
    if not data:
        return "The uploaded file is empty.", 400
    execute("""INSERT INTO billing_attachments
               (stored_name, original_name, invoice_id, uploaded_at, data_base64)
               VALUES (?, ?, ?, ?, ?)""",
            (f"invoice_{invoice_id}_{uuid.uuid4().hex}_{secure_filename(file.filename)}",
             file.filename, invoice_id, datetime.now().isoformat(), base64.b64encode(data).decode("ascii")))
    return redirect(f"/invoices#invoice-{invoice_id}")


def billing_pdf_filename(default_prefix, extension='pdf'):
    name = request.values.get('batch_name', '').strip()
    if name.lower().endswith(('.pdf', '.zip')):
        name = name[:-4]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:120].rstrip(' .')
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name):
        name = 'Billing_' + name
    if not name:
        name = f"{default_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return name + '.' + extension


def billing_folder_manifest(rows):
    if not rows:
        return 'No invoices found for this billing folder.', 400
    folder = billing_pdf_filename('billing_batch')[:-4]
    invoices = []
    for inv in rows:
        number = secure_filename(str(inv['invoice_no'] or inv['id'])) or str(inv['id'])
        invoices.append({'filename': f'invoice_{number}_id_{inv["id"]}.pdf',
                         'url': f'/invoices/{inv["id"]}/pdf?download=1'})
    return jsonify(folder_name=folder, invoices=invoices)


@app.route("/invoices/billing-batch", methods=["GET", "POST"])
def billing_batch():
    sql = """SELECT i.*, c.name AS customer_name FROM invoices i
             LEFT JOIN customers c ON c.id=i.customer_id
             WHERE COALESCE(i.is_hidden, 0)=0"""
    params = []
    if request.method == "POST" and request.form.get('billing_scope') != 'all':
        ids = list(dict.fromkeys(request.form.getlist("invoice_ids")))
        if not ids or any(not value.isdigit() for value in ids):
            return "Select at least one invoice for the billing batch.", 400
        sql += " AND i.id IN (" + ",".join("?" for _ in ids) + ")"
        params = ids
    rows = query(sql + " ORDER BY i.id", tuple(params))
    if not rows:
        return "No active invoices to bill.", 400
    return billing_folder_manifest(rows)


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

    load_choices = query('SELECT id, load_no, broker_load_no FROM loads WHERE COALESCE(is_deleted, 0) = 0 ORDER BY id DESC')
    tr = ""
    for r in rows:
        tr += f"""
        <tr>
          <td><input type='checkbox' form='archive-save-form' name='invoice_ids' value='{r["id"]}'></td>
          <td>{r['invoice_no'] or ''}</td>
          <td>{r['invoice_date'] or ''}</td>
          <td>{r['due_date'] or ''}</td>
          <td>{r['customer_name'] or r['bill_to_name'] or ''}</td>
          <td>{r['load_no'] or ''}</td>
          <td>{r['origin_text'] or ''} - {r['destination_text'] or ''}</td>
          <td>${float(r['total'] or 0):,.2f}</td>
          <td>
            <a href='/invoices/{r["id"]}/print' target='_blank'>Open / Print with Paperwork</a> |
            <a href='/invoices/{r["id"]}/pdf?download=1' target='_blank'>Download Invoice Packet</a>
            {invoice_load_link_form(r, load_choices, '/invoice-archive')}
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
        <form data-billing-folder method='get' action='/invoice-archive/batch-pdf'>
          <input type='hidden' name='search' value='{html_attr(search)}'>
          <input type='hidden' name='start_inv' value='{html_attr(start_inv)}'>
          <input type='hidden' name='end_inv' value='{html_attr(end_inv)}'>
          <input type='hidden' name='show_cleared' value='{html_attr(show_cleared)}'>
          <label>Batch folder name<input name='batch_name' required placeholder='Example: Evans October Billing'></label>
          <button>Save Billing to Folder</button>
        </form>
      </div>

      <form id='archive-save-form' data-billing-folder method='post' action='/invoice-archive/save-selected'>
        <div class='card' style='margin-top:12px; margin-bottom:12px;'>
          <h3>Save Selected Invoice PDFs to Folder</h3>
          <label>Batch folder name<input name='batch_name' placeholder='Example: Evans April 2026' required></label>
          <button>Save Selected</button>
          <p class='muted'>Select invoices below. A folder with your batch name is created in your remembered billing save location. Existing files are kept.</p>
        </div>
      </form>

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

    return billing_folder_manifest(rows)

@app.route("/invoice-archive/save-selected", methods=["POST"])
def save_selected_invoices_to_folder():
    invoice_ids = list(dict.fromkeys(request.form.getlist("invoice_ids")))
    if not invoice_ids or any(not value.isdigit() for value in invoice_ids):
        return 'Select at least one invoice first.', 400
    placeholders = ','.join('?' for _ in invoice_ids)
    rows = query(f"""SELECT i.*, c.name AS customer_name FROM invoices i
        LEFT JOIN customers c ON c.id=i.customer_id
        WHERE i.id IN ({placeholders}) ORDER BY i.id""", tuple(invoice_ids))
    return billing_folder_manifest(rows)


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
        WHERE l.id = ? AND COALESCE(l.is_deleted, 0) = 0
    """, (load_id,))

    if not load:
        return "Load not found", 404

    try:
        next_invoice_no = new_invoice_number_for_load(load)
    except ValueError as exc:
        return page(f"<p>{html_attr(str(exc))}</p><a href='/loads'>Back to Loads</a>"), 400

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


init_db()
if USE_POSTGRES:
    execute('ALTER TABLE loads ADD COLUMN IF NOT EXISTS owner_operator_amount TEXT')
elif 'owner_operator_amount' not in {row['name'] for row in query('PRAGMA table_info(loads)')}:
    execute('ALTER TABLE loads ADD COLUMN owner_operator_amount TEXT')
if USE_POSTGRES:
    execute('ALTER TABLE loads ADD COLUMN IF NOT EXISTS pickup_number TEXT')
elif 'pickup_number' not in {row['name'] for row in query('PRAGMA table_info(loads)')}:
    execute('ALTER TABLE loads ADD COLUMN pickup_number TEXT')
for table in ("loads", "drivers", "customers"):
    if USE_POSTGRES:
        execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0")
    elif "is_deleted" not in {row["name"] for row in query(f"PRAGMA table_info({table})")}:
        execute(f"ALTER TABLE {table} ADD COLUMN is_deleted INTEGER DEFAULT 0")
execute("""CREATE TABLE IF NOT EXISTS invoice_files (
           invoice_id INTEGER, load_file_id INTEGER)""")
execute("""CREATE TABLE IF NOT EXISTS billing_attachments (
           stored_name TEXT PRIMARY KEY, original_name TEXT NOT NULL,
           invoice_id INTEGER, load_id INTEGER, uploaded_at TEXT NOT NULL,
           data_base64 TEXT NOT NULL)""")
execute("""CREATE UNIQUE INDEX IF NOT EXISTS unique_active_internal_load_number
           ON loads(lower(trim(load_no)))
           WHERE COALESCE(is_deleted,0)=0 AND trim(COALESCE(load_no,''))!='' """)
register_confirmation(app, query)
from fuel_reports import register_fuel
fuel_driver_fields, fuel_report_section = register_fuel(
    app, db, query, query_one, execute, normalize_sql, page, html_attr, USE_POSTGRES)
from fuel_receipts import register_receipts
register_receipts(app, db, query, query_one, execute, normalize_sql, page, html_attr, USE_POSTGRES)
from driver_statement import register_statements
statement_driver_fields, statement_report_controls, statement_data, save_statement_miles = register_statements(
    app, query, query_one, execute, page, html_attr, USE_POSTGRES, driver_trip_total_miles)
from company_report import register_company_report
register_company_report(app, query, query_one, page, html_attr, statement_report_controls, statement_data, save_statement_miles)
app.logger.warning("Database initialized: %s", "PostgreSQL" if USE_POSTGRES else "local SQLite")


if __name__ == "__main__":

    # Listen on all network interfaces so other devices on the same Wi-Fi/LAN
    # can open the app using: http://YOUR_COMPUTER_IP:5000
    port = int(os.environ.get("PORT", 5000))
    print("Transport MVP is running.")
    print(f"Local computer: http://127.0.0.1:{port}")
    print(f"Local network:  http://YOUR_COMPUTER_IP:{port}")

    app.run(host="0.0.0.0", port=port, debug=False)
