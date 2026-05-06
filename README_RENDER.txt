RENDER SETUP FOR TRANSPORT MVP

Files in this folder:
- app.py                          Main Flask app
- requirements.txt                Python packages Render installs
- migrate_sqlite_to_postgres.py   Optional one-time migration from SQLite to Neon/Postgres
- render.yaml                     Optional Render blueprint file

RENDER SETTINGS
Build Command:
    pip install -r requirements.txt

Start Command:
    python app.py

Environment Variable:
    DATABASE_URL = your Neon connection string

IMPORTANT
- Neon is your shared database.
- Render hosts the Flask website.
- File uploads on Render's free service can disappear after redeploy/restart unless you add Render Persistent Disk or use cloud file storage.

BASIC DEPLOY STEPS
1. Put these files into a GitHub repo.
2. Go to Render.com.
3. New > Web Service.
4. Connect the GitHub repo.
5. Use Python environment.
6. Build command: pip install -r requirements.txt
7. Start command: python app.py
8. Add Environment Variable DATABASE_URL with your Neon URL.
9. Deploy.
