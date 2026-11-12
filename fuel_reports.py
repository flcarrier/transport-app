"""Posted WEX imports and an auditable, card-based driver fuel ledger."""
import csv
import hashlib
import re
import uuid
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.parse import urlencode

from flask import request, redirect


def card_suffix(value):
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) < 5:
        raise ValueError('Enter the last five digits of the card as shown in WEX.')
    return digits[-5:]


def iso_date(value):
    value = (value or '').strip()
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(value, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    raise ValueError('Enter valid start and end dates.')


def number(value):
    try:
        cleaned = str(value or '0').strip().replace(',', '').replace('$', '')
        if cleaned.startswith('(') and cleaned.endswith(')'):
            cleaned = '-' + cleaned[1:-1]
        result = Decimal(cleaned)
        if not result.is_finite():
            raise InvalidOperation
        return result
    except InvalidOperation:
        raise ValueError('The CSV contains an invalid amount or quantity.')


def parse_wex(data):
    """Validate the entire export before any database writes; never use driver names."""
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('Upload the WEX CSV export in UTF-8 format.')
    reader = csv.DictReader(StringIO(text))
    required = {'Transaction Date', 'Account Number', 'Card Number', 'Trans ID',
                'Net Cost', 'Units', 'Product Description'}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError('Use Download as CSV from the POSTED transactions section in WEX.')
    parsed = {}
    for line, row in enumerate(reader, 2):
        if not any(row.values()):
            continue
        transaction = (row.get('Trans ID') or '').strip()
        account = (row.get('Account Number') or '').strip()
        if not transaction or not account:
            raise ValueError(f'CSV row {line} is missing its transaction ID or account number.')
        # Non-fuel purchases are not driver fuel deductions.
        product = (row.get('Product Description') or '').strip()
        if not any(word in product.lower() for word in ('diesel', 'gasoline', 'unleaded', 'premium gas', 'regular gas')):
            raise ValueError(f'CSV row {line} is not a recognized fuel purchase. Export fuel transactions only.')
        unit = (row.get('Unit of Measure') or 'GA').strip().upper()
        if unit not in ('GA', 'GAL', 'GALLON', 'GALLONS'):
            raise ValueError(f'CSV row {line} uses an unsupported fuel unit: {unit}.')
        amount = number(row['Net Cost'])
        if amount != amount.quantize(Decimal('.01')):
            raise ValueError(f'CSV row {line} has more than two decimal places in Net Cost.')
        gallons = number(row['Units'])
        key = hashlib.sha256((account + '\0' + transaction).encode()).hexdigest()
        record = dict(id=key, account_key=hashlib.sha256(account.encode()).hexdigest(), transaction_no=transaction,
                      purchased_on=iso_date(row['Transaction Date']),
                      posted_on=iso_date(row.get('Post Date') or row['Transaction Date']),
                      card=card_suffix(row['Card Number']),
                      asset=(row.get('Custom Vehicle/Asset ID') or '').strip(),
                      merchant=(row.get('Merchant Name') or row.get('Merchant (Brand)') or '').strip(),
                      location=', '.join(filter(None, [(row.get('Merchant City') or '').strip(),
                                                     (row.get('Merchant State / Province') or '').strip()])),
                      product=product, gallons=str(gallons), cents=int(amount * 100))
        if key in parsed and parsed[key] != record:
            raise ValueError(f'Transaction {transaction} appears more than once with different values.')
        parsed[key] = record
    if not parsed:
        raise ValueError('The CSV has no fuel transactions.')
    return list(parsed.values())


def register_fuel(app, db, query, query_one, execute, normalize_sql, page, esc, postgres):
    if postgres:
        execute('ALTER TABLE drivers ADD COLUMN IF NOT EXISTS fuel_card TEXT')
    elif 'fuel_card' not in {r['name'] for r in query('PRAGMA table_info(drivers)')}:
        execute('ALTER TABLE drivers ADD COLUMN fuel_card TEXT')
    execute('''CREATE UNIQUE INDEX IF NOT EXISTS unique_driver_fuel_card
               ON drivers(fuel_card) WHERE fuel_card IS NOT NULL AND fuel_card != '' ''')
    execute('''CREATE TABLE IF NOT EXISTS fuel_transactions (
        id TEXT PRIMARY KEY, account_key TEXT NOT NULL, transaction_no TEXT NOT NULL, purchased_on TEXT NOT NULL,
        posted_on TEXT NOT NULL, card TEXT NOT NULL, asset TEXT, merchant TEXT,
        location TEXT, product TEXT, gallons TEXT NOT NULL, cents INTEGER NOT NULL,
        imported_at TEXT NOT NULL)''')
    execute('''CREATE TABLE IF NOT EXISTS fuel_import_source (
        id INTEGER PRIMARY KEY, account_key TEXT NOT NULL)''')
    execute('''CREATE TABLE IF NOT EXISTS driver_fuel_reports (
        id TEXT PRIMARY KEY, driver_id INTEGER NOT NULL, driver_name TEXT NOT NULL,
        card TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
        kind TEXT NOT NULL, saved_at TEXT NOT NULL, voided INTEGER DEFAULT 0)''')
    execute('''CREATE TABLE IF NOT EXISTS driver_fuel_report_items (
        report_id TEXT NOT NULL, transaction_id TEXT NOT NULL, active INTEGER DEFAULT 1,
        PRIMARY KEY(report_id, transaction_id))''')
    execute('''CREATE UNIQUE INDEX IF NOT EXISTS unique_reported_fuel_purchase
               ON driver_fuel_report_items(transaction_id) WHERE active=1''')

    def run(conn, sql, args=()):
        cur = conn.cursor()
        cur.execute(normalize_sql(sql) if postgres else sql, args)
        return cur

    def error(message, code=400):
        return page(f"<div class='card'><h1>Fuel report</h1><p>{esc(message)}</p>"
                    "<a href='/fuel'>Back to Fuel Reports</a></div>"), code

    def driver_fields(driver_id, value):
        value = value or ''
        return f"""<form method='post' action='/drivers/{driver_id}/fuel-card'>
          <label>Fuel card — last 5 digits shown in WEX
          <input name='fuel_card' inputmode='numeric' pattern='[0-9]{{5}}' maxlength='5'
                 value='{esc(value)}' placeholder='Example: 01531'></label>
          <button type='submit'>Save Fuel Card</button>
          <small>Match reports by card, not driver name. Clear this field before transferring a card to another driver.</small>
        </form>"""

    @app.route('/drivers/<int:driver_id>/fuel-card', methods=['POST'])
    def save_driver_fuel_card(driver_id):
        driver = query_one('SELECT id FROM drivers WHERE id=? AND COALESCE(is_deleted,0)=0', (driver_id,))
        if not driver:
            return error('Driver not found.', 404)
        raw = request.form.get('fuel_card', '').strip()
        try:
            card = card_suffix(raw) if raw else None
        except ValueError as exc:
            return error(str(exc))
        if card and query_one('SELECT id FROM drivers WHERE fuel_card=? AND id != ?', (card, driver_id)):
            return error('That card is already assigned to another driver. Clear the old assignment first.')
        try:
            execute('UPDATE drivers SET fuel_card=? WHERE id=?', (card, driver_id))
        except Exception:
            app.logger.exception('Could not save fuel card assignment')
            return error('The card assignment could not be saved. Refresh and check whether it is already assigned.', 409)
        return redirect('/drivers')

    def eligible(card, end):
        return query('''SELECT f.* FROM fuel_transactions f
                        WHERE f.card=? AND f.purchased_on<=?
                          AND NOT EXISTS (SELECT 1 FROM driver_fuel_report_items i WHERE i.transaction_id=f.id AND i.active=1)
                        ORDER BY f.purchased_on, f.transaction_no''', (card, end))

    def table(rows, start, selectable=False):
        body = ''
        for row in rows:
            older = row['purchased_on'] < start
            checkbox = (f"<td><input style='width:auto;' type='checkbox' name='transaction_ids' "
                        f"value='{row['id']}' checked aria-label='Include transaction {esc(row['transaction_no'])}'></td>"
                        if selectable else '')
            body += (f"<tr>{checkbox}<td>{esc(row['purchased_on'])}</td><td>{esc(row['posted_on'])}</td>"
                     f"<td>{esc(row['transaction_no'])}</td><td>****{esc(row['card'])}</td>"
                     f"<td>{esc(row['asset'])}</td><td>{esc(row['merchant'])}<br>{esc(row['location'])}</td>"
                     f"<td>{esc(row['gallons'])}</td><td>${row['cents']/100:,.2f}</td>"
                     f"<td>{'Earlier unreported fuel' if older else 'Within date range'}</td></tr>")
        heads = '<th>Include</th>' if selectable else ''
        return ("<div class='fuel-table' style='overflow:auto;'><table><tr>" + heads +
                '<th>Purchase date</th><th>Posted date</th><th>Transaction</th><th>Card</th><th>Vehicle ID</th>'
                '<th>Merchant</th><th>Gallons</th><th>Net cost</th><th>Coverage</th></tr>' + body + '</table></div>')

    def section(driver_id, start, end):
        if not driver_id or not start or not end:
            return "<div class='card'><h2>Driver fuel report</h2><p>Select a driver and both dates to check fuel.</p><a href='/fuel'>Import WEX fuel and view saved reports</a></div>"
        try:
            start, end = iso_date(start), iso_date(end)
            if start > end:
                raise ValueError('Start date must not be after end date.')
        except ValueError as exc:
            return f"<p>{esc(str(exc))}</p>"
        driver = query_one('SELECT * FROM drivers WHERE id=?', (driver_id,))
        if not driver or not driver['fuel_card']:
            return "<div class='card'><h2>Driver fuel report</h2><p>Add this driver's fuel card on the Drivers page first.</p><a href='/drivers'>Set driver fuel card</a></div>"
        card = driver['fuel_card']
        rows = eligible(card, end)
        earlier = sum(r['cents'] for r in rows if r['purchased_on'] < start)
        current = sum(r['cents'] for r in rows if r['purchased_on'] >= start)
        gallons = sum((Decimal(r['gallons']) for r in rows), Decimal('0'))
        saved = query('SELECT * FROM driver_fuel_reports WHERE driver_id=? ORDER BY saved_at DESC', (driver_id,))
        links = ''.join(f"<li><a href='/fuel/reports/{r['id']}'>{esc(r['start_date'])} through {esc(r['end_date'])} — {'VOIDED — ' if r['voided'] else ''}{'Previous report coverage' if r['kind']=='prior' else 'Saved fuel report'}</a></li>" for r in saved)
        bounds = query_one('SELECT MIN(purchased_on) AS first_date, MAX(purchased_on) AS last_date, COUNT(*) AS n FROM fuel_transactions WHERE card=?', (card,))
        info = (f"Imported purchases for this card: {esc(bounds['first_date'])} through {esc(bounds['last_date'])}. "
                'This is only the history imported, not proof that every purchase is available.' if bounds['n'] else 'No transactions imported for this card.')
        form = ''
        if rows:
            form = f"""<form method='post' action='/fuel/report/save'>
              <input type='hidden' name='driver_id' value='{driver['id']}'>
              <input type='hidden' name='start' value='{start}'><input type='hidden' name='end' value='{end}'>
              <input type='hidden' name='report_id' value='{uuid.uuid4().hex}'>
              {table(rows, start, True)}
              <label>Save selected purchases as
              <select name='kind'><option value='new'>This fuel report</option>
              <option value='prior'>Already included in a previous report</option></select></label>
              <p>For an older report, select only the purchases actually included in that report. Use its original dates above.</p>
              <label><input type='checkbox' name='reviewed' value='yes' required style='width:auto;'>
              I checked the card assignment and prior reports. The selected purchases belong in this report.</label>
              <button>Save Selected Fuel to Report</button>
            </form>"""
        else:
            form = '<p>No unreported fuel purchases through the end date were found in the imported history.</p>'
        return f"""<div class='card' style='margin-top:16px;'>
          <h2>Driver fuel report — card ****{esc(card)}</h2>
          <p>{esc(info)}</p><p><a href='/fuel'>Import posted WEX transactions</a></p>
          <p>Before the first new report, record purchases covered by your previous reports. Earlier purchases are not automatically assumed paid or covered.</p>
          <p>Within dates: <strong>${current/100:,.2f}</strong> · Earlier unreported: <strong>${earlier/100:,.2f}</strong>
          · Available total: <strong>${(current+earlier)/100:,.2f}</strong> · Gallons: {gallons:,.3f}</p>
          <p>Amounts use WEX Net Cost, including transaction fees. Posted purchases only; purchases after {end} are excluded.
          Previewing does not mark anything reported. Only saving does.</p>{form}
          <h3>Previously saved fuel reports</h3><ul>{links or '<li>No prior fuel report coverage recorded yet.</li>'}</ul>
        </div>"""

    @app.route('/fuel', methods=['GET'])
    def fuel_home():
        cards = query('''SELECT card, COUNT(*) AS n, MIN(purchased_on) AS first_date, MAX(purchased_on) AS last_date
                         FROM fuel_transactions GROUP BY card ORDER BY card''')
        drivers = query('SELECT id,name,fuel_card FROM drivers WHERE COALESCE(is_deleted,0)=0 ORDER BY name')
        card_rows = ''
        for row in cards:
            owner = next((d for d in drivers if d['fuel_card'] == row['card']), None)
            card_rows += f"<tr><td>****{esc(row['card'])}</td><td>{esc(owner['name']) if owner else 'Unassigned — set this card on Drivers'}</td><td>{row['n']}</td><td>{row['first_date']} through {row['last_date']}</td></tr>"
        history = query('SELECT * FROM driver_fuel_reports ORDER BY saved_at DESC')
        reports = ''.join(f"<li><a href='/fuel/reports/{r['id']}'>{'VOIDED — ' if r['voided'] else ''}{esc(r['driver_name'])} — {r['start_date']} through {r['end_date']} — card ****{r['card']}</a></li>" for r in history)
        notice = ''
        if request.args.get('imported', '').isdigit() and request.args.get('skipped', '').isdigit():
            notice = f"<p role='status'>Imported {int(request.args['imported'])} purchases; skipped {int(request.args['skipped'])} duplicates.</p>"
        return page(f"""<div class='card'><h1>Fuel Reports</h1>{notice}
          <p>Export <strong>POSTED</strong> fuel transactions from WEX as CSV, including older dates to catch missed fuel and late-posted purchases.
          Reimporting the same purchases will not duplicate them.</p>
          <p>This import does not connect automatically to WEX. Refresh the export before preparing each driver report.</p>
          <form method='post' action='/fuel/import' enctype='multipart/form-data'>
            <label>Posted WEX transactions CSV<input type='file' name='file' accept='.csv' required></label>
            <label><input style='width:auto;' type='checkbox' name='posted_only' value='yes' required> This file contains posted fuel transactions.</label>
            <button>Import Fuel Transactions</button></form>
          <p><a href='/drivers'>Assign fuel cards to drivers</a> · <a href='/driver-loads'>Run driver and fuel report</a></p>
          <div style='overflow:auto;'><table><tr><th>Card</th><th>Driver</th><th>Purchases imported</th><th>Purchase dates available</th></tr>{card_rows}</table></div>
          <h2>Saved fuel reports</h2><ul>{reports or '<li>No saved reports yet. Record earlier report coverage before preparing your first new report.</li>'}</ul></div>""")

    @app.route('/fuel/import', methods=['POST'])
    def import_fuel():
        uploaded = request.files.get('file')
        if not uploaded or request.form.get('posted_only') != 'yes':
            return error('Choose a posted WEX CSV and confirm it contains posted purchases.')
        data = uploaded.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            return error('The export is too large. Import CSV files smaller than 10 MB.')
        try:
            rows = parse_wex(data)
            accounts = {row['account_key'] for row in rows}
            if len(accounts) != 1:
                raise ValueError('Import one WEX account at a time. Multiple accounts could have matching masked card numbers.')
            inserted = skipped = 0
            with closing(db()) as conn:
                try:
                    source = run(conn, 'SELECT account_key FROM fuel_import_source WHERE id=1').fetchone()
                    if source and source['account_key'] not in accounts:
                        raise ValueError('This export is from a different WEX account. Import the same account so masked card numbers are not mixed.')
                    if not source:
                        run(conn, 'INSERT INTO fuel_import_source(id,account_key) VALUES (1,?)', (rows[0]['account_key'],))
                    existing_by_id = {}
                    for offset in range(0, len(rows), 500):
                        chunk = rows[offset:offset + 500]
                        placeholders = ','.join('?' for _ in chunk)
                        existing_by_id.update((r['id'], r) for r in run(conn,
                            f'SELECT * FROM fuel_transactions WHERE id IN ({placeholders})',
                            tuple(r['id'] for r in chunk)).fetchall())
                    pending = []
                    for row in rows:
                        existing = existing_by_id.get(row['id'])
                        if existing:
                            if any(str(existing[k]) != str(v) for k, v in row.items()):
                                raise ValueError(f"Transaction {row['transaction_no']} conflicts with an existing import. No rows from this file were imported.")
                            skipped += 1
                            continue
                        pending.append(row)
                    columns = list(rows[0]) + ['imported_at']
                    imported_at = datetime.now().isoformat()
                    for offset in range(0, len(pending), 100):
                        chunk = pending[offset:offset + 100]
                        placeholders = ','.join('(' + ','.join('?' for _ in columns) + ')' for _ in chunk)
                        values = tuple(value for row in chunk for value in (*row.values(), imported_at))
                        run(conn, f"INSERT INTO fuel_transactions ({','.join(columns)}) VALUES {placeholders}", values)
                    inserted = len(pending)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except ValueError as exc:
            return error(str(exc))
        except Exception:
            app.logger.exception('Fuel import failed')
            return error('The import could not be saved. Nothing from this file was added. Try again.', 409)
        return redirect('/fuel?' + urlencode(dict(imported=inserted, skipped=skipped)))

    @app.route('/fuel/report/save', methods=['POST'])
    def save_fuel_report():
        try:
            driver_id = int(request.form.get('driver_id', ''))
            start, end = iso_date(request.form.get('start')), iso_date(request.form.get('end'))
            if start > end:
                raise ValueError('Start date must not be after end date.')
            kind = request.form.get('kind')
            report_id = request.form.get('report_id', '')
            if kind not in ('new', 'prior') or not re.fullmatch('[a-f0-9]{32}', report_id):
                raise ValueError('Refresh the report preview and try again.')
            if request.form.get('reviewed') != 'yes':
                raise ValueError('Review the card and prior report coverage before saving.')
            ids = sorted(set(request.form.getlist('transaction_ids')))
            if not ids:
                raise ValueError('Select at least one fuel purchase.')
            driver = query_one('SELECT * FROM drivers WHERE id=?', (driver_id,))
            if not driver or not driver['fuel_card']:
                raise ValueError('Assign the driver a fuel card first.')
            with closing(db()) as conn:
                try:
                    existing = run(conn, 'SELECT * FROM driver_fuel_reports WHERE id=?', (report_id,)).fetchone()
                    if existing:
                        existing_ids = sorted(r['transaction_id'] for r in run(conn, 'SELECT transaction_id FROM driver_fuel_report_items WHERE report_id=?', (report_id,)).fetchall())
                        if existing['driver_id'] != driver_id or existing['start_date'] != start or existing['end_date'] != end or ids != existing_ids or existing['kind'] != kind:
                            raise ValueError('This report was already saved with different selections. Refresh the preview.')
                        return redirect('/fuel/reports/' + report_id)
                    rows = run(conn, '''SELECT f.* FROM fuel_transactions f WHERE f.card=? AND f.purchased_on<=?
                        AND NOT EXISTS (SELECT 1 FROM driver_fuel_report_items i WHERE i.transaction_id=f.id AND i.active=1)''', (driver['fuel_card'], end)).fetchall()
                    valid = {r['id'] for r in rows}
                    if not set(ids).issubset(valid):
                        raise ValueError('A selected purchase is already reported, belongs to another card, or is after the end date. Refresh the preview.')
                    run(conn, '''INSERT INTO driver_fuel_reports
                        (id,driver_id,driver_name,card,start_date,end_date,kind,saved_at) VALUES (?,?,?,?,?,?,?,?)''',
                        (report_id, driver_id, driver['name'] or '', driver['fuel_card'], start, end, kind, datetime.now().isoformat()))
                    for offset in range(0, len(ids), 100):
                        chunk = ids[offset:offset + 100]
                        values = tuple(value for transaction_id in chunk for value in (report_id, transaction_id))
                        run(conn, 'INSERT INTO driver_fuel_report_items(report_id,transaction_id) VALUES ' +
                            ','.join('(?,?)' for _ in chunk), values)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except (ValueError, TypeError) as exc:
            return error(str(exc))
        except Exception:
            app.logger.exception('Fuel report save failed')
            return error('The report could not be saved; another report may have included these purchases. Refresh and try again.', 409)
        return redirect('/fuel/reports/' + report_id)

    @app.route('/fuel/reports/<report_id>')
    def saved_fuel_report(report_id):
        report = query_one('SELECT * FROM driver_fuel_reports WHERE id=?', (report_id,))
        if not report:
            return error('Fuel report not found.', 404)
        rows = query('''SELECT f.* FROM fuel_transactions f JOIN driver_fuel_report_items i ON i.transaction_id=f.id
                        WHERE i.report_id=? ORDER BY f.purchased_on,f.transaction_no''', (report_id,))
        total = sum(r['cents'] for r in rows)
        gallons = sum((Decimal(r['gallons']) for r in rows), Decimal('0'))
        earlier = sum(r['cents'] for r in rows if r['purchased_on'] < report['start_date'])
        args = urlencode(dict(driver_id=report['driver_id'], start=report['start_date'], end=report['end_date']))
        coverage = ('This report was voided. Its purchases are available for a corrected report; this copy is for history only.' if report['voided'] else
                    'These purchases are recorded as included and will not be included again in another report.')
        undo = '' if report['voided'] else f"""<form class='no-print' method='post' action='/fuel/reports/{report_id}/void'
            onsubmit="return confirm('Void this fuel report and make its purchases available for a corrected report? The original report will stay in history.');">
            <button>Void Report / Correct Selection</button></form>"""
        return page(f"""<style>@media print {{ .nav, body > hr, .no-print {{display:none!important;}} body {{margin:12px;background:white;}}
          .fuel-table {{overflow:visible!important;}} .fuel-table table {{font-size:8px;table-layout:fixed;}}
          .fuel-table th,.fuel-table td {{overflow-wrap:anywhere;padding:4px;}} .card {{box-shadow:none;}} }}</style>
          <div class='card'><h1>{'VOIDED — ' if report['voided'] else ''}{'Previous Fuel Report Coverage' if report['kind']=='prior' else 'Saved Driver Fuel Report'}</h1>
          <h2>{esc(report['driver_name'])} — card ****{esc(report['card'])}</h2>
          <p>{report['start_date']} through {report['end_date']} · Saved {esc(report['saved_at'][:10])}</p>
          <p>{len(rows)} purchase(s) · Gallons: {gallons:,.3f} · Total net cost: <strong>${total/100:,.2f}</strong>
          · Includes earlier unreported fuel: <strong>${earlier/100:,.2f}</strong></p>
          <p>{coverage} Net cost includes transaction fees.</p>
          <div class='no-print'><button type='button' onclick='window.print()'>Print / Save as PDF</button>
          <a href='/driver-loads?{args}'>Back to driver loads for these dates</a> · <a href='/fuel'>All fuel reports</a></div>
          {table(rows, report['start_date'])}{undo}</div>""")

    @app.route('/fuel/reports/<report_id>/void', methods=['POST'])
    def void_fuel_report(report_id):
        if not query_one('SELECT id FROM driver_fuel_reports WHERE id=?', (report_id,)):
            return error('Fuel report not found.', 404)
        with closing(db()) as conn:
            try:
                run(conn, 'UPDATE driver_fuel_reports SET voided=1 WHERE id=?', (report_id,))
                run(conn, 'UPDATE driver_fuel_report_items SET active=0 WHERE report_id=?', (report_id,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return redirect('/fuel/reports/' + report_id)

    return driver_fields, section
