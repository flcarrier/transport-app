"""Conservative confirmation suggestions; OCR runs in the browser, never saves a load."""
import base64
from datetime import datetime
from io import BytesIO
import re
from flask import request, jsonify
from PIL import Image, ImageOps
from pypdf import PdfReader

MAX_BYTES = 12 * 1024 * 1024
MAX_PAGES = 6
FIELDS = {
    'broker_load_no': 'Broker Load #', 'pickup_number': 'PU #',
    'pickup_location_name': 'Pickup location name', 'pickup_address': 'Pickup address',
    'origin': 'Pickup city / state', 'origin_zip': 'Pickup ZIP',
    'pickup_date': 'Pickup date', 'pickup_time': 'Pickup time',
    'delivery_location_name': 'Delivery location name', 'delivery_address': 'Delivery address',
    'destination': 'Delivery city / state', 'destination_zip': 'Delivery ZIP',
    'delivery_date': 'Delivery date', 'delivery_time': 'Delivery time',
    'optional_stop_1': 'Additional stop 1', 'optional_stop_1_zip': 'Stop 1 ZIP',
    'optional_stop_2': 'Additional stop 2', 'optional_stop_2_zip': 'Stop 2 ZIP', 'rate': 'Load rate ($)',
    'customer_name': 'Customer / broker name',
}


def read_upload(upload):
    if not upload or not upload.filename:
        raise ValueError('Choose a confirmation PDF or photo first.')
    data = upload.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise ValueError('Choose a confirmation under 12 MB.')
    upload.stream.seek(0)
    return data


def document_pages(data):
    """Return per-page embedded text plus previews for OCR/review."""
    pages = []
    if data.startswith(b'%PDF-'):
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise ValueError('Upload an unlocked PDF.')
        if not 1 <= len(reader.pages) <= MAX_PAGES:
            raise ValueError('Use one confirmation with at most 6 pages.')
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(data)
        try:
            for i, page in enumerate(reader.pages):
                pdf_page = doc[i]
                bitmap = None
                try:
                    width, height = pdf_page.get_size()
                    bitmap = pdf_page.render(scale=min(2.0, 1800 / max(width, height)))
                    image = bitmap.to_pil().convert('RGB')
                    pages.append(image_page(image, (page.extract_text() or '')[:40000]))
                finally:
                    if bitmap is not None:
                        bitmap.close()
                    pdf_page.close()
        finally:
            doc.close()
    else:
        # Include HEIC photos from phones, just as receipt uploads do.
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        with Image.open(BytesIO(data)) as source:
            if getattr(source, 'n_frames', 1) > MAX_PAGES:
                raise ValueError('Use at most 6 image pages.')
            for i in range(getattr(source, 'n_frames', 1)):
                source.seek(i)
                image = ImageOps.exif_transpose(source).convert('RGB')
                image.thumbnail((1800, 1800))
                pages.append(image_page(image, ''))
    return pages


def image_page(image, text):
    output = BytesIO()
    image.save(output, 'JPEG', quality=86)
    return {'image': 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode('ascii'), 'text': text}


def suggestions(text):
    lines = [re.sub(r'[\t ]+', ' ', line).strip() for line in str(text)[:180000].splitlines() if line.strip()]
    result = {}
    labels = {
        'broker_load_no': r'(?:broker\s+)?load\s*(?:number|no\.?|#)|(?:shipment|order|reference)\s*(?:number|no\.?|#)',
        'pickup_number': r'(?:pick\s*up|p\.?u\.?)\s*(?:number|no\.?|#)',
        'customer_name': r'(?:customer|broker)(?:\s+name)?',
        'rate': r'(?:total\s+(?:carrier\s+)?(?:pay|rate|amount)|carrier\s+(?:pay|rate)|agreed\s+rate|load\s+rate|rate)',
        'optional_stop_1': r'(?:additional\s+)?stop\s*1',
        'optional_stop_1_zip': r'stop\s*1\s+zip(?:\s+code)?',
        'optional_stop_2': r'(?:additional\s+)?stop\s*2',
        'optional_stop_2_zip': r'stop\s*2\s+zip(?:\s+code)?',
    }
    for side, prefix in [('pickup', r'pick\s*up|shipper|origin'), ('delivery', 'delivery|deliver|consignee|destination')]:
        keys = {'location_name': r'(?:location\s+)?name', 'address': r'(?:street\s+)?address',
                'date': 'date', 'time': r'(?:appointment\s+)?time'}
        for suffix, label in keys.items():
            labels[f'{side}_{suffix}'] = rf'(?:{prefix})\s+{label}'
        labels['origin' if side == 'pickup' else 'destination'] = rf'(?:{prefix})\s+city(?:\s*[/,]\s*state)?'
        labels['origin_zip' if side == 'pickup' else 'destination_zip'] = rf'(?:{prefix})\s+zip(?:\s+code)?'
    # Require a label delimiter: avoid mistaking narrative instructions for data.
    for key, pattern in labels.items():
        for line in lines:
            match = re.match(rf'^(?:{pattern})\s*[:=]\s*(.+)$', line, re.I)
            if not match and key in ('broker_load_no', 'pickup_number'):
                match = re.match(rf'^(?:{pattern})\s+([\w][\w.-]*)$', line, re.I)
            if match:
                result[key] = match.group(1).strip()[:500]
                break
    # Prefer an explicitly labeled total over base rate / line haul.
    for line in lines:
        total=re.match(r'^total\s+(?:carrier\s+)?(?:pay|rate|amount)\s*[:=]\s*(.+)$',line,re.I)
        if total:
            result['rate']=total[1].strip()
            break
    # Common stop blocks: PICKUP / SHIPPER followed by facility, address, city ST ZIP.
    headers = []
    for i, line in enumerate(lines):
        match = re.match(r'^(pick\s*up|shipper|origin|delivery|consignee|destination)(?:\s*(?:#?\s*\d+|stop\s*\d+))?\s*:?$', line, re.I)
        if match:
            side = 'pickup' if re.match(r'pick|shipper|origin', match.group(1), re.I) else 'delivery'
            headers.append((i, side))
    seen = set()
    for position, (start, side) in enumerate(headers):
        if side in seen:
            continue
        seen.add(side)
        end = headers[position + 1][0] if position + 1 < len(headers) else min(start + 9, len(lines))
        block = lines[start + 1:end]
        for n, line in enumerate(block):
            city = re.match(r'^([A-Za-z .\'-]+),?\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$', line, re.I)
            if city:
                result.setdefault('origin' if side == 'pickup' else 'destination', f'{city[1].strip()}, {city[2].upper()}')
                result.setdefault('origin_zip' if side == 'pickup' else 'destination_zip', city[3])
                if n >= 1 and re.match(r'^\d+\s+', block[n-1]):
                    result.setdefault(f'{side}_address', block[n-1])
                    if n >= 2 and not re.search(r'[:=]', block[n-2]):
                        result.setdefault(f'{side}_location_name', block[n-2])
            for kind in ['date', 'time']:
                m = re.match(rf'^{kind}\s*:\s*(.+)$', line, re.I)
                if m:
                    result.setdefault(f'{side}_{kind}', m[1])
    for key in list(result):
        value = result[key]
        if key.endswith('_date'):
            parsed = None
            for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%m-%d-%Y', '%b %d, %Y', '%B %d, %Y'):
                try:
                    parsed = datetime.strptime(value, fmt).strftime('%Y-%m-%d')
                    break
                except ValueError:
                    pass
            if parsed:
                result[key] = parsed
            else:
                del result[key]
        elif key.endswith('_time'):
            parsed = None
            for fmt in ('%H:%M', '%I:%M %p', '%I %p', '%I:%M%p'):
                try:
                    parsed = datetime.strptime(value.upper(), fmt).strftime('%H:%M')
                    break
                except ValueError:
                    pass
            if parsed:
                result[key] = parsed
            else:
                del result[key]
        elif key == 'rate':
            match = re.fullmatch(r'\$?\s*(\d[\d,]*(?:\.\d{1,2})?)\s*(?:USD)?', value, re.I)
            if match:
                result[key] = match[1].replace(',', '')
            else:
                del result[key]
        elif key.endswith('_zip') and not re.fullmatch(r'\d{5}(?:-\d{4})?', value):
            del result[key]
    return result


def register_confirmation(app, query):
    @app.post('/loads/confirmation/read')
    def read_confirmation():
        try:
            # Limit this endpoint without changing limits for other app uploads.
            if request.content_length and request.content_length > MAX_BYTES + 1024 * 1024:
                raise ValueError('Choose a confirmation under 12 MB.')
            pages = document_pages(read_upload(request.files.get('confirmation_file')))
            return jsonify(pages=pages)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception:
            app.logger.exception('Could not read load confirmation')
            return jsonify(error='Could not read this file. Use an unlocked PDF or a clear photo.'), 400

    @app.post('/loads/confirmation/extract')
    def extract_confirmation():
        if request.content_length and request.content_length > 750000:
            return jsonify(error='Confirmation text is too long.'), 400
        body = request.get_json(silent=True) or {}
        text = body.get('text', '')
        if not isinstance(text, str) or len(text) > 180000:
            return jsonify(error='Confirmation text is too long.'), 400
        fields = suggestions(text)
        customer = fields.get('customer_name', '')
        # An exact unique match only. Never create customers or fuzzy-assign one.
        matches = query('SELECT id,name FROM customers WHERE COALESCE(is_deleted,0)=0 AND lower(trim(name))=lower(?)', (customer,)) if customer else []
        return jsonify(fields=fields, labels=FIELDS, customer=({'id': matches[0]['id'], 'name': matches[0]['name']} if len(matches) == 1 else None))

CONFIRMATION_UI = r'''
<section aria-label="Load confirmation upload" style="border:1px solid #ddd;border-radius:10px;padding:14px;margin:14px 0;">
  <h2>Upload Load Confirmation</h2>
  <p class="muted">Read a PDF or photo to suggest load details. Review them before creating the load. One confirmation, up to 6 pages and 12 MB. The original will be saved with the load paperwork.</p>
  <label>Confirmation PDF or photo<input type="file" name="confirmation_file" id="confirmation-file" accept=".pdf,.jpg,.jpeg,.png,.webp,.heic,.heif,.tif,.tiff,.bmp"></label>
  <button type="button" id="read-confirmation">Read confirmation</button>
  <button type="button" id="cancel-confirmation" hidden>Cancel reading</button>
  <button type="button" id="remove-confirmation">Remove confirmation</button>
  <p id="confirmation-status" role="status" aria-live="polite"></p>
  <input type="hidden" name="confirmation_reviewed" value="">
  <div id="confirmation-review" hidden>
    <h3>Review suggested details</h3>
    <p>Compare every field with the confirmation. Blank fields could not be identified. Internal Load #, equipment, driver pay, and status are kept as entered below.</p>
    <details><summary>View original confirmation pages</summary><div id="confirmation-pages"></div></details>
    <details><summary>View text read from confirmation</summary><pre id="confirmation-text" style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:300px;overflow:auto;"></pre></details>
    <div id="confirmation-fields" class="form-grid"></div>
    <p id="confirmation-customer-note"></p>
    <label style="display:block;"><input type="checkbox" id="confirmation-checked" style="width:auto;"> I checked these details against the confirmation.</label>
    <button type="button" id="apply-confirmation">Use reviewed details</button>
  </div>
</section>
<script>
(() => {
 const form=document.getElementById('load-form'),file=document.getElementById('confirmation-file');
 const read=document.getElementById('read-confirmation'),cancel=document.getElementById('cancel-confirmation');
 const status=document.getElementById('confirmation-status'),review=document.getElementById('confirmation-review');
 const checked=document.getElementById('confirmation-checked'),confirmed=form.elements.namedItem('confirmation_reviewed');
 let controller=null,worker=null,busy=false,run=0,customer=null;
 const fieldsBox=document.getElementById('confirmation-fields');
 function invalidate(){confirmed.value='';checked.checked=false;}
 function stop(){run++;if(controller)controller.abort();if(worker)worker.terminate().catch(()=>{});worker=null;busy=false;read.disabled=false;cancel.hidden=true;}
 file.addEventListener('change',()=>{stop();invalidate();review.hidden=true;status.textContent='';fieldsBox.replaceChildren();document.getElementById('confirmation-pages').replaceChildren();});
 document.getElementById('remove-confirmation').addEventListener('click',()=>{file.value='';file.dispatchEvent(new Event('change'));status.textContent='Confirmation removed. You can enter the load manually.';});
 cancel.addEventListener('click',()=>{stop();status.textContent='Reading cancelled. Try again or remove the file to enter the load manually.';});
 fieldsBox.addEventListener('input',invalidate);
 async function loadOcr(){
   if(window.Tesseract)return;
   await new Promise((resolve,reject)=>{
     const script=document.createElement('script');
     const timer=setTimeout(()=>{script.remove();reject(new Error('Reader could not load. Try again.'));},20000);
     script.src='https://cdn.jsdelivr.net/npm/tesseract.js@6.0.1/dist/tesseract.min.js';
     script.onload=()=>{clearTimeout(timer);resolve();};
     script.onerror=()=>{clearTimeout(timer);reject(new Error('Reader could not load. Try again.'));};document.head.appendChild(script);
   });
 }
 read.addEventListener('click',async()=>{
   if(busy)return;
   if(!file.files.length){status.textContent='Choose a confirmation PDF or photo first.';return;}
   if(file.files[0].size>12*1024*1024){status.textContent='Choose a confirmation under 12 MB.';return;}
   invalidate();review.hidden=true;busy=true;read.disabled=true;cancel.hidden=false;
   const current=++run;controller=new AbortController();
   const timer=setTimeout(()=>{if(current===run){stop();status.textContent='Reading took too long. Try a clearer or smaller file, or remove the file and enter the load manually.';}},180000);
   try{
     status.textContent='Opening confirmation…';
     const data=new FormData();data.append('confirmation_file',file.files[0]);
     let response=await fetch('/loads/confirmation/read',{method:'POST',body:data,signal:controller.signal});
     let result=await response.json();if(!response.ok)throw new Error(result.error||'Could not read file.');
     if(current!==run)return;
     const pages=result.pages;
     let text='';
     for(let i=0;i<pages.length;i++){
       if(current!==run)return;
       const page=pages[i];let pageText=page.text;
       if(pageText.replace(/\s/g,'').length<60){
         status.textContent='Reading scanned page '+(i+1)+' of '+pages.length+'…';
         await loadOcr();if(current!==run)return;
         if(!worker){const created=await Tesseract.createWorker('eng',1,{logger:m=>{if(current===run&&m.status==='recognizing text')status.textContent='Reading page '+(i+1)+' of '+pages.length+': '+Math.round(m.progress*100)+'%';}});if(current!==run){await created.terminate().catch(()=>{});return;}worker=created;}
         if(current!==run){if(worker)await worker.terminate().catch(()=>{});worker=null;return;}
         const output=await worker.recognize(page.image);pageText=output.data.text;
       }
       text+=pageText+'\n\n';
     }
     if(current!==run)return;
     response=await fetch('/loads/confirmation/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text.slice(0,180000)}),signal:controller.signal});
     result=await response.json();if(!response.ok)throw new Error(result.error||'Could not suggest details.');if(current!==run)return;
     fieldsBox.replaceChildren();customer=result.customer;
     for(const [name,labelText] of Object.entries(result.labels)){
       const label=document.createElement('label');label.append(document.createTextNode(labelText));
       const input=document.createElement('input');input.dataset.field=name;input.value=result.fields[name]||'';
       input.type=name.endsWith('_date')?'date':name.endsWith('_time')?'time':'text';
       label.append(input);fieldsBox.append(label);
     }
     const previews=document.getElementById('confirmation-pages');previews.replaceChildren();
     pages.forEach((page,i)=>{const img=document.createElement('img');img.src=page.image;img.alt='Confirmation page '+(i+1);img.loading='lazy';img.style.cssText='display:block;max-width:100%;height:auto;margin:10px 0';previews.append(img);});
     document.getElementById('confirmation-text').textContent=text;
     document.getElementById('confirmation-customer-note').textContent=customer?'Matched existing customer: '+customer.name+'.':'Select the existing customer below if the name does not match exactly. No new customer will be created.';
     review.hidden=false;
     status.textContent=Object.keys(result.fields).length?'Suggested details are ready. Check each one, correct anything needed, then use the reviewed details.':'No labeled details were identified. Use the original preview and enter the details in the review fields.';
   }catch(error){if(current===run)status.textContent=error.message||'Reading failed. Try again or enter the details manually.';}
   finally{
     clearTimeout(timer);
     if(current===run){if(worker)await worker.terminate().catch(()=>{});worker=null;busy=false;read.disabled=false;cancel.hidden=true;}
   }
 });
 document.getElementById('apply-confirmation').addEventListener('click',()=>{
   if(!checked.checked){status.textContent='Check the review box before using these details.';return;}
   const values=[...fieldsBox.querySelectorAll('input')];
   const supplied=values.filter(x=>x.value.trim()&&x.dataset.field!=='customer_name');
   const overwrites=supplied.some(x=>{const target=form.elements.namedItem(x.dataset.field);return target&&target.value&&target.value!==x.value;});
   const customerName=values.find(x=>x.dataset.field==='customer_name').value.trim();
   const match=customer&&customer.name.toLowerCase().trim()===customerName.toLowerCase();
   const currentCustomer=document.getElementById('customer_id').value;
   if((overwrites||(match&&currentCustomer&&currentCustomer!==String(customer.id)))&&!window.confirm('Replace the matching details already entered in the load form with these reviewed details?'))return;
   supplied.forEach(x=>{const target=form.elements.namedItem(x.dataset.field);if(target)target.value=x.value.trim();});
   if(match){document.getElementById('customer_search').value=customer.name;document.getElementById('customer_id').value=customer.id;}
   confirmed.value='1';status.textContent='Reviewed details copied into the load form. Check the form, select your customer and equipment, then press Create to save.';
 });
 form.addEventListener('submit',event=>{
   if(file.files.length&&(busy||confirmed.value!=='1')){event.preventDefault();status.textContent=busy?'Wait for reading to finish or cancel it.':'Read the confirmation and use the reviewed details first, or remove the file to enter the load manually.';status.scrollIntoView({block:'center'});}
 });
})();
</script>
'''
