"""Driver statement PDF layout; payment uses entered miles and pay rate only."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from xml.sax.saxutils import escape
from datetime import datetime
from urllib.parse import urlencode

from flask import request, redirect, send_file

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether


def amount(value, optional=False, nonnegative=False):
    if value is None or str(value).strip() == '':
        if optional:
            return None
        raise ValueError('Enter a numeric amount.')
    try:
        result = Decimal(str(value).strip().replace(',', '').replace('$', ''))
    except InvalidOperation:
        raise ValueError('Enter a valid numeric amount.')
    if not result.is_finite() or (nonnegative and result < 0) or abs(result) > Decimal('100000000'):
        raise ValueError('Enter a valid amount within the supported range.')
    return result


def calculate(trips, rate, adjustments):
    rate = amount(rate, optional=True, nonnegative=True)
    complete = rate is not None
    loaded_total = empty_total = miles_total = pay_total = Decimal('0')
    items = []
    for trip in trips:
        loaded = amount(trip.get('loaded_miles'), optional=True, nonnegative=True)
        empty = amount(trip.get('empty_miles'), optional=True, nonnegative=True)
        total = loaded + empty if loaded is not None and empty is not None else None
        pay = (total * rate).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) if total is not None and rate is not None else None
        complete = complete and pay is not None
        if loaded is not None:
            loaded_total += loaded
        if empty is not None:
            empty_total += empty
        if total is not None:
            miles_total += total
        if pay is not None:
            pay_total += pay
        items.append(dict(trip, loaded=loaded, empty=empty, total_miles=total, pay=pay))
    credits = sum((amount(a['amount']) for a in adjustments), Decimal('0'))
    return dict(items=items, rate=rate, complete=complete,
                loaded=loaded_total, empty=empty_total, miles=miles_total,
                pay=pay_total if complete else None, adjustments=credits,
                check=(pay_total + credits).quantize(Decimal('.01')) if complete else None)


def mileage_input(value):
    value = amount(value, optional=True, nonnegative=True)
    if value is not None and value != value.quantize(Decimal('.01')):
        raise ValueError('Enter miles with no more than two decimal places.')
    return str(value) if value is not None else None


def register_statements(app, query, query_one, execute, page, esc, postgres):
    for table, columns in [('drivers', ['payee_name','mailing_address','pay_rate_per_mile']),
                           ('loads', ['loaded_miles','empty_miles'])]:
        existing = set() if postgres else {r['name'] for r in query(f'PRAGMA table_info({table})')}
        for column in columns:
            if postgres:
                execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} TEXT')
            elif column not in existing:
                execute(f'ALTER TABLE {table} ADD COLUMN {column} TEXT')

    def error(message):
        return page(f"<div class='card'><h1>Driver Statement</h1><p>{esc(message)}</p><a href='/driver-loads'>Back to Driver Reports</a></div>"),400

    def fields(driver):
        return f"""<details><summary>Pay Rate per Mile / Statement Details</summary>
          <form method='post' action='/drivers/{driver['id']}/statement-settings'>
          <label>Payee / company name (optional)<input name='payee_name' value='{esc(driver['payee_name'])}'></label>
          <label>Mailing address<textarea name='mailing_address'>{esc(driver['mailing_address'])}</textarea></label>
          <label>Driver pay per mile ($)<input name='pay_rate_per_mile' type='number' min='0' step='0.0001' value='{esc(driver['pay_rate_per_mile'])}' placeholder='Enter agreed pay rate'></label>
          <button>Save Statement Details</button></form></details>"""

    @app.route('/drivers/<int:driver_id>/statement-settings',methods=['POST'])
    def save_statement_settings(driver_id):
        if not query_one('SELECT id FROM drivers WHERE id=?',(driver_id,)):
            return 'Driver not found',404
        try:
            rate=amount(request.form.get('pay_rate_per_mile'),optional=True,nonnegative=True)
            if rate is not None and rate != rate.quantize(Decimal('.0001')):
                raise ValueError('Enter a pay rate with no more than four decimal places.')
        except ValueError as exc:
            return error(str(exc))
        execute('UPDATE drivers SET payee_name=?,mailing_address=?,pay_rate_per_mile=? WHERE id=?',
                (request.form.get('payee_name','').strip(),request.form.get('mailing_address','').strip(),
                 str(rate) if rate is not None else None,driver_id))
        return redirect('/drivers')

    def controls():
        return """<details><summary>PDF statement options</summary>
          <p>The PDF uses the driver's saved payment details and the loaded/empty miles entered on each load.</p>
          <label>Statement number (optional)<input name='statement_no'></label>
          <label>Company address (optional)<textarea name='company_address'></textarea></label>
          <label>Remit-to information (optional)<textarea name='remit_to'></textarea></label>
          <p>The PDF uses the driver's saved rate per mile and entered loaded/empty miles.</p>
          <h3>Credits and deductions (optional)</h3>
          <label>Description<input name='adjustment_description' placeholder='Example: Expense reimbursement'></label>
          <label>Amount ($)<input type='number' min='0' step='0.01' name='adjustment_amount'></label>
          <label>Type<select name='adjustment_type'><option value='credit'>Credit — add to check</option><option value='deduction'>Deduction — subtract from check</option></select></label>
          </details>"""

    def report_context():
        from fuel_reports import iso_date
        driver_id=int(request.values.get('driver_id',''))
        start,end=iso_date(request.values.get('start')),iso_date(request.values.get('end'))
        if start>end:
            raise ValueError('Start date must not be after end date.')
        driver=query_one('SELECT * FROM drivers WHERE id=?',(driver_id,))
        if not driver:
            raise ValueError('Choose an existing driver.')
        loads=[dict(r) for r in query('''SELECT l.*, t.unit_no AS truck_unit FROM loads l LEFT JOIN trucks t ON t.id=l.truck_id
                       WHERE l.driver_id=? AND COALESCE(l.is_deleted,0)=0
                         AND COALESCE(NULLIF(l.delivery_date,''),l.pickup_date)>=?
                         AND COALESCE(NULLIF(l.delivery_date,''),l.pickup_date)<=?
                       ORDER BY COALESCE(NULLIF(l.delivery_date,''),l.pickup_date),l.id''',(driver_id,start,end))]
        return driver,start,end,loads

    def save_report_miles(driver, loads, persist=True):
        changes=[]
        valid_ids={r['id'] for r in loads}
        for key in request.form:
            for prefix in ('loaded_miles_','empty_miles_'):
                if key.startswith(prefix):
                    try:
                        load_id=int(key[len(prefix):])
                    except ValueError:
                        raise ValueError('Invalid trip in the mileage form. Reload the trip list.')
                    if load_id not in valid_ids:
                        raise ValueError('The mileage form includes a trip outside this driver and date range. Reload the trip list.')
        for row in loads:
            loaded_key,empty_key=f"loaded_miles_{row['id']}",f"empty_miles_{row['id']}"
            if loaded_key not in request.form and empty_key not in request.form:
                continue
            loaded=mileage_input(request.form.get(loaded_key,row['loaded_miles']))
            empty=mileage_input(request.form.get(empty_key,row['empty_miles']))
            changes.append((row['id'],loaded,empty))
            row['loaded_miles'],row['empty_miles']=loaded,empty
        if changes and persist:
            cases=' '.join('WHEN ? THEN ?' for _ in changes)
            placeholders=','.join('?' for _ in changes)
            args=tuple(v for row in changes for v in (row[0],row[1]))
            args+=tuple(v for row in changes for v in (row[0],row[2]))
            args+=(driver['id'],)+tuple(row[0] for row in changes)
            execute(f'''UPDATE loads SET loaded_miles=CASE id {cases} ELSE loaded_miles END,
                        empty_miles=CASE id {cases} ELSE empty_miles END
                        WHERE driver_id=? AND id IN ({placeholders}) AND COALESCE(is_deleted,0)=0''',args)

    @app.route('/driver-loads/miles',methods=['POST'])
    def save_driver_report_miles():
        try:
            driver,start,end,loads=report_context()
            save_report_miles(driver,loads)
        except (ValueError,TypeError) as exc:
            return error(str(exc))
        return redirect('/driver-loads?'+urlencode(dict(driver_id=driver['id'],start=start,end=end,miles_saved='yes')))

    @app.route('/driver-loads/pdf',methods=['GET','POST'])
    def driver_statement_pdf():
        try:
            driver,start,end,loads=report_context()
            driver_id=driver['id']
            rate=amount(driver['pay_rate_per_mile'],optional=True,nonnegative=True)
            if rate is not None and rate != rate.quantize(Decimal('.0001')):
                raise ValueError('Enter a pay rate with no more than four decimal places.')
            if request.method=='POST':
                save_report_miles(driver,loads,persist=False)
            trips=[dict(load_no=r['load_no'],date=r['delivery_date'] or r['pickup_date'],
                        route=' - '.join(filter(None,[r['origin'],r['optional_stop_1'],r['optional_stop_2'],r['destination']])),
                        loaded_miles=r['loaded_miles'],empty_miles=r['empty_miles']) for r in loads]
            adjustments=[]
            adjustment=amount(request.values.get('adjustment_amount'),optional=True,nonnegative=True)
            if adjustment is not None:
                description=request.values.get('adjustment_description','').strip()
                kind=request.values.get('adjustment_type')
                if not description or kind not in ('credit','deduction') or adjustment!=adjustment.quantize(Decimal('.01')):
                    raise ValueError('Enter a description, type, and amount with up to two decimal places for the adjustment.')
                adjustments.append(dict(description=description,date=end,amount=adjustment if kind=='credit' else -adjustment))
            info=dict(company_name='Florida Carrier & Brokerage Service Inc',
                      company_address=request.values.get('company_address','').strip(),remit_to=request.values.get('remit_to','').strip(),
                      driver_name=driver['name'],payee_name=driver['payee_name'],address=driver['mailing_address'],
                      statement_no=request.values.get('statement_no','').strip() or f'DR-{driver_id}-{start.replace("-","")}-{end.replace("-","")}',
                      statement_date=datetime.now().strftime('%m/%d/%Y'),start=start,end=end,pay_rate=rate,
                      trucks=', '.join(sorted({r['truck_unit'] for r in loads if r['truck_unit']})),
                      notes='')
            result=render_statement(info,trips,adjustments)
            if request.method=='POST':
                save_report_miles(driver,loads)
        except (ValueError,TypeError) as exc:
            return error(str(exc))
        return send_file(BytesIO(result),mimetype='application/pdf',as_attachment=True,
                         download_name=f'driver_{driver_id}_statement_{start}_to_{end}.pdf')

    return fields,controls


def render_statement(info, trips, adjustments):
    result = calculate(trips, info.get('pay_rate'), adjustments)
    stream = BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=letter, rightMargin=30, leftMargin=30,
                            topMargin=28, bottomMargin=40, title='Driver Statement',
                            author=info['company_name'])
    styles = {
        'normal': ParagraphStyle('normal', fontName='Helvetica', fontSize=8, leading=10),
        'small': ParagraphStyle('small', fontName='Helvetica', fontSize=7, leading=9),
        'header': ParagraphStyle('header', fontName='Helvetica-Bold', fontSize=10, leading=13),
        'right': ParagraphStyle('right', fontName='Helvetica', fontSize=9, leading=12, alignment=TA_RIGHT),
        'warning': ParagraphStyle('warning', fontName='Helvetica-Bold', fontSize=8, leading=11, textColor=colors.HexColor('#8b260d')),
    }

    def p(text, style='normal'):
        return Paragraph(escape(str(text or '')).replace('\n', '<br/>'), styles[style])

    def money(value):
        return f'${value:,.2f}' if value is not None else 'Not set'

    def miles(value):
        return f'{value:,.2f}' if value is not None else 'Not set'

    def grid(data, widths, header=True):
        table = Table(data, colWidths=widths, repeatRows=1 if header else 0, hAlign='LEFT')
        table.setStyle(TableStyle([
            ('GRID', (0,0), (-1,-1), .45, colors.HexColor('#555555')),
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#eeeeee')),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING', (0,0), (-1,-1), 4), ('RIGHTPADDING', (0,0), (-1,-1), 4),
            ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ]))
        return table

    company = [p(info['company_name'], 'header')]
    if info.get('company_address'):
        company.append(p(info['company_address'], 'small'))
    if info.get('remit_to'):
        company.append(p('Remit to: ' + info['remit_to'], 'small'))
    statement = f"Statement #{info['statement_no']}\n{info['driver_name']}\nDate: {info['statement_date']}\nTruck(s): {info.get('trucks') or 'Not assigned'}"
    header = Table([[company, p(statement, 'right')]], colWidths=[290,262])
    header.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]))
    story = [header, Spacer(1,32),
             p(info.get('payee_name') or info['driver_name'], 'header')]
    if info.get('address'):
        story.append(p(info['address']))
    rate_text=f"${result['rate']:.4f} per mile" if result['rate'] is not None else 'Not set in driver profile'
    story += [Spacer(1,8), p(f"Report period: {info['start']} through {info['end']}"),
              p('Driver pay rate: '+rate_text), Spacer(1,12)]
    if not result['complete']:
        missing=[]
        if result['rate'] is None:
            missing.append('driver pay rate in the profile')
        if any(r['loaded'] is None for r in result['items']):
            missing.append('loaded miles')
        if any(r['empty'] is None for r in result['items']):
            missing.append('empty miles')
        story += [p('INCOMPLETE: Missing ' + ', '.join(missing) + '. Enter the missing values on the Driver Report tab. The check amount has not been calculated.', 'warning'), Spacer(1,8)]
    story.append(p('Trips:', 'header'))
    data = [[p(h, 'small') for h in ['Date','Trip #','Route','Description','Loaded\nMiles','Empty\nMiles','Total\nMiles','Amount']]]
    for row in result['items']:
        desc = (f"{miles(row['total_miles'])} total miles @ {result['rate']:.4f}" if row['total_miles'] is not None and result['rate'] is not None else
                f"Mileage not entered; pay rate ${result['rate']:.4f} per mile" if result['rate'] is not None else 'Pay rate not entered in driver profile')
        data.append([p(row.get('date'), 'small'), p(row.get('load_no'), 'small'),
                     p(row.get('route'), 'small'), p(desc, 'small'), p(miles(row['loaded']), 'small'),
                     p(miles(row['empty']), 'small'), p(miles(row['total_miles']), 'small'), p(money(row['pay']), 'small')])
    if not trips:
        data.append([p('No trips found in the selected date range.', 'small')] + [''] * 7)
    data.append(['','','',p('Total:', 'small'),p(miles(result['loaded']), 'small'),
                 p(miles(result['empty']), 'small'),p(miles(result['miles']), 'small'),p(money(result['pay']), 'small')])
    story += [grid(data, [44,42,135,172,36,36,39,48]), Spacer(1,12)]
    credits = [[p('Credits / Deductions', 'header'),p('Date','small'),p('Amount','small')]]
    for entry in adjustments:
        credits.append([p(entry['description'],'small'),p(entry.get('date'),'small'),p(money(amount(entry['amount'])),'small')])
    if not adjustments:
        credits.append([p('None','small'),' ',p('$0.00','small')])
    credits.append([p('Total adjustments:','small'),'',p(money(result['adjustments']),'small')])
    story.append(KeepTogether([grid(credits,[382,80,90]),Spacer(1,10),
                              Table([['',p('Check Amount:', 'header'),p(money(result['check']), 'header')]],colWidths=[342,110,100])]))
    story += [Spacer(1,10),p('Report preview. Downloading this PDF does not finalize a payment.', 'small')]
    if info.get('notes'):
        story += [Spacer(1,12),p(info['notes'],'small')]

    def footer(c, doc):
        c.setFont('Helvetica',6)
        c.drawString(30,22,info['company_name'][:70])
        c.drawRightString(582,22,f'Page {doc.page}')
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    return stream.getvalue()
