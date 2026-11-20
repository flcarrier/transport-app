"""Receipt upload, browser OCR, required review, and driver-assigned fuel."""
import base64
import hashlib
import json
import re
import threading
import uuid
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from io import BytesIO

from flask import request, redirect, jsonify, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

PDF_LOCK = threading.Lock()


def receipt_fields(text):
    """Suggestions only: do not infer a driver from a name printed on a receipt."""
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text[:30000].splitlines() if line.strip()]
    result = dict(purchased_on='', merchant='', gallons='', total='', card='', reference='')
    dates = []
    for line in lines:
        for token in re.findall(r'\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/.-]\d{1,2}[/.-](?:20\d{2}|\d{2}))\b', line):
            for fmt in ('%Y-%m-%d','%Y/%m/%d','%m/%d/%Y','%m/%d/%y','%m-%d-%Y','%m-%d-%y','%m.%d.%Y'):
                try:
                    dates.append(datetime.strptime(token, fmt).strftime('%Y-%m-%d'))
                    break
                except ValueError:
                    pass
    if dates:
        result['purchased_on'] = dates[0]
    merchant_candidates = [line for line in lines[:8] if re.search('[a-zA-Z]{3}', line) and
                           not re.search(r'RECEIPT|THANK|CUSTOMER COPY|MERCHANT COPY|TRANSACTION', line, re.I)]
    if merchant_candidates:
        result['merchant'] = merchant_candidates[0][:150]
    cards = set()
    for line in lines:
        masked = re.findall(r'(?:\*|[xX]|•){2,}[\s-]*(\d{4,5})(?!\d)', line)
        cards.update(masked)
        if re.search(r'CARD|VISA|MASTER|AMEX|ACCOUNT', line, re.I):
            ending = re.search(r'(?:ENDING|LAST\s*[45]|CARD\s*(?:NO|NUMBER|#)?\s*[:.]?)\s*(\d{4,5})\b', line, re.I)
            if ending:
                cards.add(ending.group(1))
        if not re.search(r'PRICE|PER GAL|UNIT COST', line, re.I):
            qty = re.search(r'(?:GALLONS?|VOLUME|QTY|QUANTITY)\s*[:=]?\s*(\d+(?:\.\d{1,3})?)', line, re.I)
            qty = qty or re.search(r'(\d+\.\d{1,3})\s*(?:GAL\b|GALLONS?\b)', line, re.I)
            if qty and not result['gallons']:
                result['gallons'] = qty.group(1)
        if re.search(r'\b(?:GRAND TOTAL|TOTAL SALE|SALE TOTAL|AMOUNT DUE|TOTAL)\b', line, re.I) and not re.search(r'SUBTOTAL|TAX TOTAL', line, re.I):
            values = re.findall(r'(?<![\d.])\$?\s*(\d{1,7}(?:,\d{3})*\.\d{2})(?!\d)', line)
            if values:
                result['total'] = values[-1].replace(',', '')
        ref = re.search(r'(?:RECEIPT|TRANS(?:ACTION)?|TICKET)\s*(?:NO\.?|NUMBER|#|ID)?\s*[:#-]\s*([A-Z0-9-]{3,40})', line, re.I)
        if ref:
            result['reference'] = ref.group(1)
    if len(cards) == 1:
        result['card'] = cards.pop()
    return result


def receipt_images(data):
    """Decode uploads by content, strip metadata, and bound OCR image sizes."""
    pages = []
    if data.startswith(b'%PDF-'):
        import pypdfium2 as pdfium
        with PDF_LOCK:
            with pdfium.PdfDocument(data) as doc:
                if not 1 <= len(doc) <= 3:
                    raise ValueError('Upload one receipt at a time, with no more than three PDF pages.')
                for n in range(len(doc)):
                    page = doc[n]
                    try:
                        width, height = page.get_size()
                        if width <= 0 or height <= 0:
                            raise ValueError('This PDF page could not be read.')
                        bitmap = page.render(scale=min(2.5, 2600 / max(width,height)))
                        try:
                            pages.append(bitmap.to_pil().convert('RGB').copy())
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
        mime = 'application/pdf'
    else:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        try:
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > 36000000 or image.width * image.height == 0:
                    raise ValueError('Choose an image smaller than 36 megapixels.')
                mime = Image.MIME.get(image.format, 'application/octet-stream')
                pages.append(ImageOps.exif_transpose(image).convert('RGB').copy())
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError('Choose a readable JPG, PNG, HEIC photo, or a PDF receipt.') from exc
    images = []
    for image in pages:
        image.thumbnail((2600,3600))
        image = ImageOps.autocontrast(ImageOps.grayscale(image))
        stream = BytesIO()
        image.save(stream, format='JPEG', quality=92)
        images.append(base64.b64encode(stream.getvalue()).decode('ascii'))
        image.close()
    return images, mime


def split_receipt_pdf(data):
    """One receipt per PDF page; validate the whole upload before storing drafts."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        raise ValueError('Upload an unlocked PDF without a password.')
    if not 1 <= len(reader.pages) <= 20:
        raise ValueError('Upload a PDF with 1 to 20 pages, one receipt per page.')
    parts = []
    size = 0
    for source in reader.pages:
        writer = PdfWriter()
        writer.add_page(source)
        output = BytesIO()
        writer.write(output)
        part = output.getvalue()
        images, mime = receipt_images(part)
        size += len(part) + sum(len(image) for image in images)
        if size > 48 * 1024 * 1024:
            raise ValueError('This PDF is too complex to split at once. Upload fewer pages per file.')
        parts.append((part, images, mime))
    return parts


def register_receipts(app, db, query, query_one, execute, normalize_sql, page, esc, postgres):
    for column, sql_type in [('driver_id','INTEGER'),('receipt_id','TEXT')]:
        if postgres:
            execute(f'ALTER TABLE fuel_transactions ADD COLUMN IF NOT EXISTS {column} {sql_type}')
        elif column not in {r['name'] for r in query('PRAGMA table_info(fuel_transactions)')}:
            execute(f'ALTER TABLE fuel_transactions ADD COLUMN {column} {sql_type}')
    execute('''CREATE UNIQUE INDEX IF NOT EXISTS unique_fuel_receipt_transaction
               ON fuel_transactions(receipt_id) WHERE receipt_id IS NOT NULL''')
    execute('''CREATE TABLE IF NOT EXISTS fuel_receipts (
        id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, original_name TEXT NOT NULL,
        original_base64 TEXT NOT NULL, mime TEXT NOT NULL, images_json TEXT NOT NULL,
        suggestions_json TEXT NOT NULL, uploaded_at TEXT NOT NULL, transaction_id TEXT)''')
    if postgres:
        execute('ALTER TABLE fuel_receipts ADD COLUMN IF NOT EXISTS is_discarded INTEGER NOT NULL DEFAULT 0')
    elif 'is_discarded' not in {r['name'] for r in query('PRAGMA table_info(fuel_receipts)')}:
        execute('ALTER TABLE fuel_receipts ADD COLUMN is_discarded INTEGER NOT NULL DEFAULT 0')
    execute('''CREATE TABLE IF NOT EXISTS fuel_receipt_batches (
        id TEXT PRIMARY KEY, original_name TEXT NOT NULL, uploaded_at TEXT NOT NULL)''')
    execute('''CREATE TABLE IF NOT EXISTS fuel_receipt_batch_pages (
        batch_id TEXT NOT NULL, page_no INTEGER NOT NULL, receipt_id TEXT NOT NULL,
        PRIMARY KEY(batch_id,page_no))''')
    # Preserve the user's existing card assignments when moving to receipt entry.
    execute('''UPDATE fuel_transactions SET driver_id=(SELECT d.id FROM drivers d
               WHERE d.fuel_card=fuel_transactions.card AND COALESCE(d.is_deleted,0)=0)
               WHERE driver_id IS NULL AND receipt_id IS NULL AND EXISTS
               (SELECT 1 FROM drivers d WHERE d.fuel_card=fuel_transactions.card AND COALESCE(d.is_deleted,0)=0)''')

    def run(conn, sql, args=()):
        cur=conn.cursor()
        cur.execute(normalize_sql(sql) if postgres else sql,args)
        return cur

    def fail(message, status=400):
        return page(f"<div class='card'><h1>Fuel Receipt</h1><p>{esc(message)}</p><a href='/fuel/receipts'>Back to receipts</a></div>"),status

    def matched_driver(card):
        if not re.fullmatch(r'\d{4,5}',card or ''):
            return None
        candidates=[r for r in query('SELECT id,name,fuel_card FROM drivers WHERE COALESCE(is_deleted,0)=0')
                    if r['fuel_card'] and (str(r['fuel_card']).endswith(card) or card.endswith(str(r['fuel_card'])))]
        return candidates[0] if len(candidates)==1 else None

    def batch_rows(batch_id):
        return query('''SELECT b.page_no,r.* FROM fuel_receipt_batch_pages b
                        JOIN fuel_receipts r ON r.id=b.receipt_id WHERE b.batch_id=? ORDER BY b.page_no''',(batch_id,))

    def batch_links(receipt_id):
        source = query_one('SELECT batch_id,page_no FROM fuel_receipt_batch_pages WHERE receipt_id=? ORDER BY batch_id LIMIT 1',(receipt_id,))
        if not source:
            return ''
        rows = batch_rows(source['batch_id'])
        following = next((r for r in rows if r['page_no']>source['page_no'] and r['id']!=receipt_id and not r['transaction_id'] and not r['is_discarded']),None)
        next_link = f" · <a href='/fuel/receipts/{following['id']}'>Review Next Receipt</a>" if following else ''
        return f"<p>PDF page {source['page_no']} of {len(rows)} · <a href='/fuel/receipt-batches/{source['batch_id']}'>All Receipts in This PDF</a>{next_link}</p>"

    @app.route('/fuel/receipt-batches/<batch_id>')
    def receipt_batch(batch_id):
        batch = query_one('SELECT * FROM fuel_receipt_batches WHERE id=?',(batch_id,))
        if not batch:
            return fail('Receipt PDF not found.',404)
        rows = batch_rows(batch_id)
        saved = sum(bool(r['transaction_id']) for r in rows)
        items = ''.join(f"<tr><td>{r['page_no']}</td><td><a href='/fuel/receipts/{r['id']}'>{esc(r['original_name'])}</a></td><td>{'Saved' if r['transaction_id'] else ('Discarded draft' if r['is_discarded'] else 'Review needed')}</td></tr>" for r in rows)
        pending = next((r for r in rows if not r['transaction_id'] and not r['is_discarded']),None)
        start = f"<p><a href='/fuel/receipts/{pending['id']}'>Review Next Receipt</a></p>" if pending else ''
        return page(f"""<div class='card'><h1>Receipts From PDF</h1><p>{esc(batch['original_name'])}</p>
          <p>{len(rows)} separate receipt(s) · {saved} saved to fuel.</p>
          <p>Each page is a separate receipt. Review each driver's assignment, date, gallons and fuel amount before saving. Uploading alone does not add fuel charges.</p>
          {start}<div style='overflow:auto;'><table><tr><th>PDF page</th><th>Receipt</th><th>Status</th></tr>{items}</table></div>
          <p><a href='/fuel/receipts'>Upload More Receipts</a></p></div>""")

    def upload_pdf(data, filename):
        batch_id = hashlib.sha256(data).hexdigest()
        if query_one('SELECT id FROM fuel_receipt_batches WHERE id=?',(batch_id,)):
            return redirect('/fuel/receipt-batches/'+batch_id)
        try:
            parts = split_receipt_pdf(data)
        except Exception as exc:
            app.logger.warning('Receipt PDF could not be split: %s',type(exc).__name__)
            return fail(str(exc) if isinstance(exc,ValueError) else 'This PDF could not be split. Try an unlocked PDF or upload receipt photos.')
        with closing(db()) as conn:
            try:
                run(conn,'''INSERT INTO fuel_receipt_batches(id,original_name,uploaded_at) VALUES (?,?,?)
                            ON CONFLICT(id) DO NOTHING''',(batch_id,filename[:200],datetime.now().isoformat()))
                for number,(part,images,mime) in enumerate(parts,1):
                    digest = hashlib.sha256(part).hexdigest()
                    receipt_id = uuid.uuid4().hex
                    stem = filename.rsplit('.',1)[0][:160]
                    run(conn,'''INSERT INTO fuel_receipts
                        (id,digest,original_name,original_base64,mime,images_json,suggestions_json,uploaded_at)
                        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(digest) DO NOTHING''',
                        (receipt_id,digest,f'{stem} - page {number}.pdf',base64.b64encode(part).decode('ascii'),mime,
                         json.dumps(images),'{}',datetime.now().isoformat()))
                    actual = run(conn,'SELECT id FROM fuel_receipts WHERE digest=?',(digest,)).fetchone()
                    run(conn,'''INSERT INTO fuel_receipt_batch_pages(batch_id,page_no,receipt_id) VALUES (?,?,?)
                                ON CONFLICT(batch_id,page_no) DO NOTHING''',(batch_id,number,actual['id']))
                conn.commit()
            except Exception:
                conn.rollback()
                app.logger.exception('Could not save split receipt PDF')
                return fail('No partial upload was saved. Please try uploading this PDF again.',409)
        return redirect('/fuel/receipt-batches/'+batch_id)

    @app.route('/fuel/receipts')
    def receipt_home():
        rows=query('''SELECT r.id,r.original_name,r.uploaded_at,r.transaction_id,f.purchased_on,f.cents,d.name AS driver_name
                      FROM fuel_receipts r LEFT JOIN fuel_transactions f ON f.id=r.transaction_id
                      LEFT JOIN drivers d ON d.id=f.driver_id WHERE r.is_discarded=0 ORDER BY r.uploaded_at DESC''')
        discarded=query('SELECT id,original_name FROM fuel_receipts WHERE is_discarded=1 ORDER BY uploaded_at DESC')
        restore=''.join(f"<form method='post' action='/fuel/receipts/{r['id']}/restore'>{esc(r['original_name'])} <button>Restore Receipt Draft</button></form>" for r in discarded)
        items=''.join(f"<tr><td><a href='/fuel/receipts/{r['id']}'>{esc(r['original_name'])}</a></td>"
                      f"<td>{esc(r['driver_name'] or 'Review needed')}</td><td>{esc(r['purchased_on'] or '')}</td>"
                      f"<td>{('$'+format(r['cents']/100,',.2f')) if r['cents'] is not None else ''}</td>"
                      f"<td>{'Saved' if r['transaction_id'] else 'Not saved to fuel yet'}</td></tr>" for r in rows)
        return page(f"""<div class='card'><h1>Scan / Upload Fuel Receipt</h1>
          <p>Upload a receipt photo or a PDF with one receipt per page. Each PDF page becomes a separate receipt to review.</p>
          <p>PDFs: up to 20 pages and 12 MB. Confirm the date, station, gallons, amount and driver for each receipt before saving.</p>
          <form method='post' action='/fuel/receipts/upload' enctype='multipart/form-data'>
            <label>Choose a receipt photo or PDF<input type='file' name='file' accept='image/*,.heic,.heif,.pdf' required></label>
            <button>Upload and Review Receipt</button></form>
          <form method='post' action='/fuel/receipts/upload' enctype='multipart/form-data'>
            <label>Take a photo with your phone<input type='file' name='file' accept='image/*' capture='environment' required></label>
            <button>Scan Photo and Review</button></form>
          <p>Card digits suggest a driver only when the match is unique. Cash or unreadable cards can be assigned manually.</p>
          <p><a href='/fuel'>Fuel Reports</a> · <a href='/driver-loads'>Driver Reports</a></p>
          <div style='overflow:auto;'><table><tr><th>Receipt</th><th>Driver</th><th>Purchase date</th><th>Total</th><th>Status</th></tr>{items}</table></div>
          <details><summary>Discarded receipt drafts</summary>{restore or '<p>No discarded drafts.</p>'}</details></div>""")

    @app.route('/fuel/receipts/upload',methods=['POST'])
    def upload_receipt():
        uploaded=request.files.get('file')
        if not uploaded or not uploaded.filename:
            return fail('Choose a receipt photo or PDF.')
        data=uploaded.read(12*1024*1024+1)
        if not data or len(data)>12*1024*1024:
            return fail('Choose a non-empty receipt smaller than 12 MB.')
        if data.startswith(b'%PDF-'):
            return upload_pdf(data,uploaded.filename)
        digest=hashlib.sha256(data).hexdigest()
        existing=query_one('SELECT id FROM fuel_receipts WHERE digest=?',(digest,))
        if existing:
            execute('UPDATE fuel_receipts SET is_discarded=0 WHERE id=?',(existing['id'],))
            return redirect('/fuel/receipts/'+existing['id'])
        try:
            images,mime=receipt_images(data)
        except Exception as exc:
            app.logger.warning('Receipt image could not be decoded: %s',type(exc).__name__)
            return fail(str(exc) if isinstance(exc,ValueError) else 'This receipt could not be opened. Try a JPG or PNG photo.')
        receipt_id=uuid.uuid4().hex
        try:
            execute('''INSERT INTO fuel_receipts(id,digest,original_name,original_base64,mime,images_json,suggestions_json,uploaded_at)
                       VALUES (?,?,?,?,?,?,?,?)''',(receipt_id,digest,uploaded.filename[:200],base64.b64encode(data).decode('ascii'),mime,
                       json.dumps(images),json.dumps({}),datetime.now().isoformat()))
        except Exception:
            existing=query_one('SELECT id FROM fuel_receipts WHERE digest=?',(digest,))
            if existing:
                return redirect('/fuel/receipts/'+existing['id'])
            raise
        return redirect('/fuel/receipts/'+receipt_id)

    @app.route('/fuel/receipts/<receipt_id>/image/<int:page_no>')
    def receipt_image(receipt_id,page_no):
        receipt=query_one('SELECT images_json FROM fuel_receipts WHERE id=?',(receipt_id,))
        if not receipt:
            return 'Receipt not found',404
        images=json.loads(receipt['images_json'])
        if not 0<=page_no<len(images):
            return 'Page not found',404
        response=send_file(BytesIO(base64.b64decode(images[page_no])),mimetype='image/jpeg')
        response.headers['Cache-Control']='private, no-store'
        return response

    @app.route('/fuel/receipts/<receipt_id>/original')
    def receipt_original(receipt_id):
        receipt=query_one('SELECT * FROM fuel_receipts WHERE id=?',(receipt_id,))
        if not receipt:
            return 'Receipt not found',404
        return send_file(BytesIO(base64.b64decode(receipt['original_base64'])),mimetype=receipt['mime'],
                         as_attachment=True,download_name=receipt['original_name'])

    @app.route('/fuel/receipts/<receipt_id>/extract',methods=['POST'])
    def extract_receipt(receipt_id):
        receipt=query_one('SELECT transaction_id FROM fuel_receipts WHERE id=?',(receipt_id,))
        if not receipt:
            return jsonify(error='Receipt not found'),404
        if receipt['transaction_id']:
            return jsonify(error='This receipt was already saved.'),409
        body=request.get_json(silent=True) or {}
        text=body.get('text','')
        if not isinstance(text,str) or len(text)>30000:
            return jsonify(error='Receipt text is too large.'),400
        suggestions=receipt_fields(text)
        match=matched_driver(suggestions['card'])
        suggestions['driver_id']=str(match['id']) if match else ''
        suggestions['match_note']=('Card match suggests '+match['name']+'. Please confirm.' if match else
                                   'No unique card match. Choose the driver manually.')
        execute('UPDATE fuel_receipts SET suggestions_json=? WHERE id=? AND transaction_id IS NULL',
                (json.dumps(suggestions),receipt_id))
        return jsonify(suggestions)

    def review(receipt, values=None, message='', duplicate=False):
        fields=dict(json.loads(receipt['suggestions_json']))
        if values is not None:
            fields.update({k:values.get(k,'') for k in ('purchased_on','merchant','location','gallons','total','card','reference','driver_id')})
        saved=None
        if receipt['transaction_id']:
            saved=query_one('SELECT f.*,d.name AS driver_name FROM fuel_transactions f LEFT JOIN drivers d ON d.id=f.driver_id WHERE f.id=?',(receipt['transaction_id'],))
        drivers=query('SELECT id,name FROM drivers WHERE COALESCE(is_deleted,0)=0 ORDER BY name')
        options="<option value=''>Choose driver — required</option>"+''.join(
            f"<option value='{r['id']}' {'selected' if str(r['id'])==str(fields.get('driver_id','')) else ''}>{esc(r['name'])}</option>" for r in drivers)
        images=json.loads(receipt['images_json'])
        urls=[f"/fuel/receipts/{receipt['id']}/image/{n}" for n in range(len(images))]
        preview=''.join(f"<a href='{url}' target='_blank' rel='noopener'><img src='{url}' alt='Receipt page {n+1}' style='max-width:100%;max-height:700px;object-fit:contain;'></a>" for n,url in enumerate(urls))
        notice=f"<p role='alert'>{esc(message)}</p>" if message else ''
        if saved:
            return page(f"""<div class='card'><h1>Saved Fuel Receipt</h1>{batch_links(receipt['id'])}<p>This receipt is already saved. Uploading it again will not create another charge.</p>
              <p>Driver: <strong>{esc(saved['driver_name'])}</strong> · Date: {saved['purchased_on']} · Total: ${saved['cents']/100:,.2f}</p>
              <p><a href='/fuel/receipts'>More receipts</a> · <a href='/fuel'>Fuel Reports</a> · <a href='/fuel/receipts/{receipt['id']}/original'>Download original receipt</a></p>{preview}</div>""")
        duplicate_check=("<label><input type='checkbox' name='allow_duplicate' value='yes' style='width:auto;' required> I checked the possible duplicate and this is a different purchase.</label>" if duplicate else '')
        return page(f"""<div class='card'><h1>Review Fuel Receipt</h1>{batch_links(receipt['id'])}{notice}
          <p id='ocr-status' role='status'>Nothing has been added to fuel yet. Read the receipt or use automatic reading, then confirm the details.</p>
          <p><button type='button' id='read-receipt'>Read Receipt Automatically</button>
          <button type='button' id='stop-reading'>Enter Details Manually</button></p>
          <div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:20px;'>
          <div>{preview}<a href='/fuel/receipts/{receipt['id']}/original'>Download original receipt</a></div>
          <form id='receipt-review' method='post' action='/fuel/receipts/{receipt['id']}/save'>
            <label>Driver<select name='driver_id' required>{options}</select></label>
            <p id='card-match'>{esc(fields.get('match_note','Choose the driver manually if the receipt has no recognized card.'))}</p>
            <label>Purchase date<input type='date' name='purchased_on' value='{esc(fields.get('purchased_on',''))}' required></label>
            <label>Fuel station<input name='merchant' maxlength='150' value='{esc(fields.get('merchant',''))}' required></label>
            <label>Station city / location (optional)<input name='location' maxlength='200' value='{esc(fields.get('location',''))}'></label>
            <label>Gallons<input type='number' min='0.001' step='0.001' name='gallons' value='{esc(fields.get('gallons',''))}' required></label>
            <label>Fuel total paid ($)<input type='number' min='0.01' step='0.01' name='total' value='{esc(fields.get('total',''))}' required></label>
            <label>Card last 4 or 5 digits (optional; leave blank for cash)<input name='card' inputmode='numeric' pattern='[0-9]{{4,5}}' maxlength='5' value='{esc(fields.get('card',''))}'></label>
            <label>Receipt / transaction number (optional)<input name='reference' maxlength='80' value='{esc(fields.get('reference',''))}'></label>
            <p>Confirm that the amount and gallons are fuel only. Remove any store purchases from the amount.</p>
            {duplicate_check}
            <label><input style='width:auto;' type='checkbox' name='confirmed' value='yes' required> I reviewed the receipt, driver, date, gallons and fuel amount.</label>
            <button id='save-receipt'>Confirm and Save Fuel Receipt</button>
            <a href='/fuel/receipts'>Back to receipts</a>
          </form></div><form method='post' action='/fuel/receipts/{receipt['id']}/discard'><button>Discard Receipt Draft</button></form></div>
          <script>
          (() => {{
            const form=document.getElementById('receipt-review'),status=document.getElementById('ocr-status');
            const read=document.getElementById('read-receipt'),save=document.getElementById('save-receipt');
            const urls={json.dumps(urls)}; let worker=null,cancelled=false,busy=false;
            form.querySelectorAll('input,select').forEach(el=>el.addEventListener('input',()=>el.dataset.edited='yes'));
            document.getElementById('stop-reading').addEventListener('click',()=>{{
              cancelled=true; if(worker) worker.terminate().catch(()=>{{}});
              save.disabled=false; status.textContent='Enter or correct the receipt details, then confirm and save.';
            }});
            async function loadOcr() {{
              if(window.Tesseract) return;
              await new Promise((resolve,reject)=>{{
                const script=document.createElement('script');
                const timer=setTimeout(()=>reject(new Error('OCR library unavailable')),20000);
                script.src='https://cdn.jsdelivr.net/npm/tesseract.js@6.0.1/dist/tesseract.min.js';
                script.onload=()=>{{clearTimeout(timer);resolve();}};
                script.onerror=()=>{{clearTimeout(timer);reject(new Error('OCR library unavailable'));}};
                document.head.appendChild(script);
              }});
            }}
            async function scan() {{
              if(busy)return; busy=true;cancelled=false;read.disabled=true;save.disabled=true;
              const timeout=setTimeout(()=>{{cancelled=true;if(worker)worker.terminate().catch(()=>{{}});save.disabled=false;status.textContent='Automatic reading took too long. Enter the details manually.';}},90000);
              try {{
                status.textContent='Loading automatic receipt reader…'; await loadOcr();
                if(cancelled)return;
                worker=await Tesseract.createWorker('eng',1,{{logger:m=>{{if(!cancelled&&m.status==='recognizing text')status.textContent='Reading receipt: '+Math.round(m.progress*100)+'%';}}}});
                let text='';
                for(const url of urls){{ if(cancelled)return;const result=await worker.recognize(url);text+=result.data.text+'\\n';}}
                if(cancelled)return;
                const response=await fetch('/fuel/receipts/{receipt['id']}/extract',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{text}})}});
                const result=await response.json();if(!response.ok)throw new Error('Could not read receipt');
                for(const [name,value] of Object.entries(result)){{const input=form.elements.namedItem(name);if(input&&input.dataset.edited!=='yes')input.value=value||'';}}
                document.getElementById('card-match').textContent=result.match_note;
                status.textContent='Suggested details are ready. Check every field against the receipt before saving.';
              }} catch(error) {{if(!cancelled)status.textContent='Automatic reading could not finish. You can enter the details manually below.';}}
              finally {{clearTimeout(timeout);if(worker)await worker.terminate().catch(()=>{{}});worker=null;busy=false;read.disabled=false;save.disabled=false;}}
            }}
            read.addEventListener('click',scan);
            {'scan();' if not fields and not message else ''}
          }})();
          </script>""")

    @app.route('/fuel/receipts/<receipt_id>')
    def review_receipt(receipt_id):
        receipt=query_one('SELECT * FROM fuel_receipts WHERE id=?',(receipt_id,))
        return review(receipt) if receipt else fail('Receipt not found.',404)

    @app.route('/fuel/receipts/<receipt_id>/discard',methods=['POST'])
    def discard_receipt(receipt_id):
        execute('UPDATE fuel_receipts SET is_discarded=1 WHERE id=? AND transaction_id IS NULL',(receipt_id,))
        return redirect('/fuel/receipts')

    @app.route('/fuel/receipts/<receipt_id>/restore',methods=['POST'])
    def restore_receipt(receipt_id):
        execute('UPDATE fuel_receipts SET is_discarded=0 WHERE id=?',(receipt_id,))
        return redirect('/fuel/receipts/'+receipt_id)

    @app.route('/fuel/receipts/<receipt_id>/save',methods=['POST'])
    def save_receipt(receipt_id):
        from fuel_reports import iso_date, number
        receipt=query_one('SELECT * FROM fuel_receipts WHERE id=?',(receipt_id,))
        if not receipt:
            return fail('Receipt not found.',404)
        if receipt['transaction_id']:
            return redirect('/fuel/receipts/'+receipt_id)
        try:
            if request.form.get('confirmed')!='yes':
                raise ValueError('Confirm the receipt details and driver before saving.')
            driver_id=int(request.form.get('driver_id',''))
            driver=query_one('SELECT id,name FROM drivers WHERE id=? AND COALESCE(is_deleted,0)=0',(driver_id,))
            if not driver:
                raise ValueError('Choose an active driver.')
            purchased_on=iso_date(request.form.get('purchased_on'))
            merchant=request.form.get('merchant','').strip()[:150]
            location=request.form.get('location','').strip()[:200]
            gallons=number(request.form.get('gallons'))
            total=number(request.form.get('total'))
            if not merchant or not 0<gallons<=10000 or gallons!=gallons.quantize(Decimal('.001')):
                raise ValueError('Enter the fuel station and valid gallons with up to three decimal places.')
            if not 0<total<=1000000 or total!=total.quantize(Decimal('.01')):
                raise ValueError('Enter a valid fuel total with up to two decimal places.')
            card=request.form.get('card','').strip()
            if card and not re.fullmatch(r'\d{4,5}',card):
                raise ValueError('Enter only the last four or five card digits, or leave the card blank for cash.')
            cents=int(total*100)
            duplicates=query('''SELECT f.id FROM fuel_transactions f
                WHERE f.purchased_on=? AND f.cents=? AND (f.driver_id=? OR
                (f.driver_id IS NULL AND ?!='' AND f.card!='' AND (? LIKE '%' || f.card OR f.card LIKE '%' || ?)))''',
                (purchased_on,cents,driver_id,card,card,card))
            if duplicates and request.form.get('allow_duplicate')!='yes':
                return review(receipt,request.form,'Possible duplicate: a fuel purchase for this driver or card already has this date and amount. Check it before saving another charge.',True),409
            transaction_id='receipt-'+receipt['digest']
            reference=request.form.get('reference','').strip()[:80] or 'Receipt '+receipt_id[:8]
            with closing(db()) as conn:
                try:
                    existing=run(conn,'SELECT id FROM fuel_transactions WHERE receipt_id=?',(receipt_id,)).fetchone()
                    if not existing:
                        run(conn,'''INSERT INTO fuel_transactions
                            (id,account_key,transaction_no,purchased_on,posted_on,card,asset,merchant,location,product,gallons,cents,imported_at,driver_id,receipt_id)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(transaction_id,'receipt',reference,purchased_on,purchased_on,card,'',merchant,location,
                            'Fuel receipt',str(gallons),cents,datetime.now().isoformat(),driver_id,receipt_id))
                    run(conn,'UPDATE fuel_receipts SET transaction_id=?,is_discarded=0 WHERE id=?',(transaction_id,receipt_id))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except (ValueError,TypeError) as exc:
            return review(receipt,request.form,str(exc)),400
        except Exception:
            app.logger.exception('Fuel receipt save failed')
            if query_one('SELECT id FROM fuel_transactions WHERE receipt_id=?',(receipt_id,)):
                return redirect('/fuel/receipts/'+receipt_id)
            return review(receipt,request.form,'The receipt could not be saved. Your uploaded image is still available; please try again.'),409
        return redirect('/fuel/receipts/'+receipt_id)

    return matched_driver
