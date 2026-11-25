"""Internal company view of driver pay, load confirmation amounts and fuel."""
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from urllib.parse import urlencode

from flask import request, redirect, send_file
from driver_statement import amount, calculate, render_statement


def company_totals(loads, fuel, start):
    confirmations=[]
    for load in loads:
        value=amount(load['rate'],optional=True)
        confirmations.append(dict(load_no=load['load_no'],date=load['delivery_date'] or load['pickup_date'],
                                  amount=value.quantize(Decimal('.01'),rounding=ROUND_HALF_UP) if value is not None else None))
    complete=all(r['amount'] is not None for r in confirmations)
    total=sum((r['amount'] for r in confirmations if r['amount'] is not None),Decimal('0'))
    earlier=[r for r in fuel if r['purchased_on']<start]
    within=[r for r in fuel if r['purchased_on']>=start]
    receipts=[r for r in within if r.get('receipt_id')]
    return dict(confirmations=confirmations,confirmation_total=total if complete else None,
                within_gallons=sum((Decimal(r['gallons']) for r in within),Decimal('0')),
                within_cost=Decimal(sum(r['cents'] for r in within))/100,
                receipt_count=len(receipts),receipt_gallons=sum((Decimal(r['gallons']) for r in receipts),Decimal('0')),
                receipt_cost=Decimal(sum(r['cents'] for r in receipts))/100,
                fuel=fuel,gallons=sum((Decimal(r['gallons']) for r in fuel),Decimal('0')),
                fuel_cost=Decimal(sum(r['cents'] for r in fuel))/100,
                earlier_gallons=sum((Decimal(r['gallons']) for r in earlier),Decimal('0')),
                earlier_cost=Decimal(sum(r['cents'] for r in earlier))/100)


def register_company_report(app,query,query_one,page,esc,controls,statement_data,save_miles):
    def error(message):
        return page(f"<div class='card'><h1>Company Report</h1><p role='alert'>{esc(message)}</p><a href='/company-report'>Back to Company Report</a></div>"),400

    def context():
        driver,start,end,loads,info,trips,adjustments=statement_data()
        fuel=[dict(r) for r in query('''SELECT f.* FROM fuel_transactions f
            WHERE (f.driver_id=? OR (f.driver_id IS NULL AND f.card=? AND f.card!=''))
              AND f.purchased_on<=? AND (f.purchased_on>=? OR NOT EXISTS
                (SELECT 1 FROM driver_fuel_report_items i WHERE i.transaction_id=f.id AND i.active=1))
            ORDER BY f.purchased_on,f.transaction_no,f.id''',(driver['id'],driver['fuel_card'] or '',end,start))]
        company=company_totals(loads,fuel,start)
        pay=calculate(trips,info['pay_rate'],adjustments)
        company['total_costs']=(pay['check']+company['fuel_cost']) if pay['check'] is not None else None
        company['balance']=(company['confirmation_total']-company['total_costs']) if company['confirmation_total'] is not None and company['total_costs'] is not None else None
        info['report_title']='Company Report'
        info['company_report']=company
        if driver['is_deleted']:
            info['notes']='Historical report for a deleted driver profile. Fuel saved to a current driver profile is not included. Select the current driver profile for current receipts.'
        if not request.values.get('statement_no','').strip():
            info['statement_no']=f"CO-{driver['id']}-{start.replace('-','')}-{end.replace('-','')}"
        return driver,start,end,loads,info,trips,adjustments,company

    def money(value):
        return f'${value:,.2f}' if value is not None else 'Not set — incomplete'

    @app.route('/company-report')
    def company_report():
        driver_id=request.args.get('driver_id','')
        start=request.args.get('start','');end=request.args.get('end','')
        drivers=query('SELECT id,name,is_deleted FROM drivers WHERE COALESCE(is_deleted,0)=0 OR id=? ORDER BY name,id',(int(driver_id) if driver_id.isdigit() else -1,))
        opts="<option value=''>Select driver</option>"+''.join(f"<option value='{d['id']}' {'selected' if str(d['id'])==driver_id else ''}>{esc(d['name'])}{' — Deleted profile (historical)' if d['is_deleted'] else ''}</option>" for d in drivers)
        notice=''
        chosen=next((d for d in drivers if str(d['id'])==driver_id),None)
        if chosen and chosen['is_deleted']:
            alternatives=[d for d in drivers if not d['is_deleted'] and d['name'].strip().lower()==chosen['name'].strip().lower()]
            links=' '.join(f"<a href='/company-report?{urlencode(dict(driver_id=d['id'],start=start,end=end))}'>Use current profile for {esc(d['name'])}</a>" for d in alternatives)
            notice=f"<p role='alert'>This is a deleted driver profile. Current approved receipts belong to the current profile. {links}</p>"
        report=''
        if driver_id and start and end:
            try:
                driver,start,end,loads,info,trips,adjustments,company=context()
                pay=calculate(trips,info['pay_rate'],adjustments)
            except (ValueError,TypeError) as exc:
                return error(str(exc))
            summary=[('Total confirmation amounts',money(company['confirmation_total'])),
                     ('Driver mileage pay',money(pay['pay'])),('Credits / deductions',money(pay['adjustments'])),
                     ('Driver check amount',money(pay['check'])),('Fuel gallons (purchased)',f"{company['gallons']:,.3f}"),
                     ('Total fuel cost',money(company['fuel_cost'])),
                     ('Fuel gallons in selected dates',f"{company['within_gallons']:,.3f}"),
                     ('Fuel cost in selected dates',money(company['within_cost'])),
                     ('Earlier unreported fuel cost',money(company['earlier_cost'])),
                     ('Total costs (driver check + fuel)',money(company['total_costs'])),
                     ('Company balance after costs',money(company['balance']))]
            totals=''.join(f'<tr><th>{label}</th><td>{value}</td></tr>' for label,value in summary)
            miles=''
            for load,trip in zip(loads,pay['items']):
                fmt=lambda x:f'{x:,.2f}' if x is not None else 'Not set'
                miles+=f"""<tr><td>{esc(load['load_no'])}</td><td>{esc(trip['date'])}</td><td>{esc(trip['route'])}</td>
                  <td>{fmt(trip['loaded'])}</td><td><input form='company-report-form' name='empty_miles_{load['id']}'
                  aria-label='Empty miles for load {load['id']}' type='number' min='0' step='0.01' value='{esc(load['empty_miles'] or '0')}'></td>
                  <td>{fmt(trip['total_miles'])}</td><td>{money(trip['pay'])}</td></tr>"""
            customers={r['id']:r['name'] for r in query('SELECT id,name FROM customers')}
            detail=''.join('<tr>'+''.join(f'<td>{esc(load[key])}</td>' for key in
                ['load_no','customer_name','origin','origin_zip','optional_stop_1','optional_stop_1_zip','optional_stop_2','optional_stop_2_zip','destination','destination_zip'])+
                f"<td>{money(amount(load['rate'],optional=True))}</td><td>{esc(load['status'])}</td></tr>" for load in
                [dict(load,customer_name=customers.get(load['customer_id'],'')) for load in loads])
            fuel_rows=''
            for row in company['fuel']:
                receipt=f"<a href='/fuel/receipts/{esc(row['receipt_id'])}'>View receipt</a>" if row['receipt_id'] else 'Imported fuel'
                fuel_rows+=f"<tr><td>{esc(row['purchased_on'])}</td><td>{esc(row['transaction_no'])}</td><td>{esc(row['merchant'])}<br>{esc(row['location'])}</td><td>{esc(row['gallons'])}</td><td>{money(Decimal(row['cents'])/100)}</td><td>{'Earlier unreported fuel' if row['purchased_on']<start else 'Within date range'}</td><td>{receipt}</td></tr>"
            rate=f"${pay['rate']:.4f} per mile" if pay['rate'] is not None else 'Not set in driver profile'
            report=f"""<div class='card'><h2>{esc(driver['name'])} — Company Report</h2><p>{start} through {end} · Driver rate: {rate}</p>
              <table>{totals}</table><p>Confirmation amounts use each load's saved rate; they do not indicate whether the broker has paid.</p>
              <p>Fuel totals include {money(company['earlier_cost'])} and {company['earlier_gallons']:,.3f} gallons of earlier unreported fuel.
              Fuel is shown separately and does not reduce the driver check amount.</p>
              <p>Company balance = total confirmation amounts − driver check amount − total fuel cost. The driver check already includes credits and deductions, so those adjustments are not counted twice. Other company expenses are not recorded in this report.</p>
              <h2>Paid Miles for This Report</h2><div style='overflow:auto;'><table><tr><th>Trip #</th><th>Date</th><th>Route</th><th>Loaded miles</th><th>Empty miles</th><th>Total miles</th><th>Driver pay</th></tr>{miles or '<tr><td colspan="7">No trips in the selected date range.</td></tr>'}</table></div>
              <h2>Trip Details / Confirmation Amounts</h2><div style='overflow:auto;'><table><tr><th>Load #</th><th>Customer</th><th>Origin</th><th>Origin ZIP</th><th>Stop 1</th><th>Stop 1 ZIP</th><th>Stop 2</th><th>Stop 2 ZIP</th><th>Destination</th><th>Destination ZIP</th><th>Confirmation amount</th><th>Status</th></tr>{detail}</table></div>
              <h2>Fuel Purchases</h2><p>{company['receipt_count']} approved scanned receipt(s) in selected dates: {company['receipt_gallons']:,.3f} gallons / {money(company['receipt_cost'])}.</p><p>Gallons and costs come from saved receipts/imports. Within-date purchases are shown even if already included in a fuel report. Older purchases are included only when no prior report covers them.</p>
              <div style='overflow:auto;'><table><tr><th>Purchase date</th><th>Transaction</th><th>Station / location</th><th>Gallons</th><th>Fuel cost</th><th>Coverage</th><th>Receipt</th></tr>{fuel_rows or '<tr><td colspan="7">No matching fuel purchases found.</td></tr>'}</table></div>
              <p>Previewing or downloading this company report does not mark fuel as reported. Record coverage in Fuel Reports after reviewing it.</p></div>"""
        return page(f"""<div class='card'><h1>Company Report</h1>{notice}
          <form id='company-report-form' method='post' action='/company-report/pdf'>
            <label>Driver<select name='driver_id' required>{opts}</select></label>
            <label>Start date<input type='date' name='start' value='{esc(start)}' required></label>
            <label>End date<input type='date' name='end' value='{esc(end)}' required></label>
            {controls()}<button>Run / Download Company PDF</button>
            <button formaction='/company-report' formmethod='get'>Load Trips / Enter Miles</button>
            <button formaction='/company-report/miles' formmethod='post'>Save Miles</button></form>
          <p>Uses the same driver profile, Trip Details Total Miles, empty miles, pay rate, credits and deductions as Driver Report, with confirmation and fuel totals added.</p>
          <p>Load trips to enter empty miles, then run the company PDF.</p></div>{report}""")

    @app.route('/company-report/miles',methods=['POST'])
    def company_report_miles():
        try:
            driver,start,end,loads,info,trips,adjustments,company=context()
            save_miles(driver,loads)
        except (ValueError,TypeError) as exc:
            return error(str(exc))
        return redirect('/company-report?'+urlencode(dict(driver_id=driver['id'],start=start,end=end)))

    @app.route('/company-report/pdf',methods=['GET','POST'])
    def company_report_pdf():
        try:
            driver,start,end,loads,info,trips,adjustments,company=context()
            output=render_statement(info,trips,adjustments)
            if request.method=='POST':
                save_miles(driver,loads)
        except (ValueError,TypeError) as exc:
            return error(str(exc))
        return send_file(BytesIO(output),mimetype='application/pdf',as_attachment=True,
                         download_name=f"company_report_driver_{driver['id']}_{start}_to_{end}.pdf")
