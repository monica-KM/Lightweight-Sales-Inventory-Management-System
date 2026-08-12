from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import sqlite3, json, html, os, sys, webbrowser, threading, time, signal
from datetime import datetime, date

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'data.db'
PID_FILE = ROOT / '.server.pid'
HOST = '127.0.0.1'
PORT = 8765


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def init_db():
    conn = db()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS products(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      spec TEXT DEFAULT '',
      unit TEXT DEFAULT '件',
      original_price REAL NOT NULL DEFAULT 0,
      discount_price REAL NOT NULL DEFAULT 0,
      retail_price REAL NOT NULL DEFAULT 0,
      stock INTEGER NOT NULL DEFAULT 0,
      enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS customers(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      phone TEXT DEFAULT '',
      address TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS handlers(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL UNIQUE
    );
    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      order_no TEXT NOT NULL UNIQUE,
      order_date TEXT NOT NULL,
      customer_id INTEGER,
      handler_id INTEGER,
      delivery_method TEXT DEFAULT '',
      summary TEXT DEFAULT '',
      total REAL NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT '有效',
      created_at TEXT NOT NULL,
      FOREIGN KEY(customer_id) REFERENCES customers(id),
      FOREIGN KEY(handler_id) REFERENCES handlers(id)
    );
    CREATE TABLE IF NOT EXISTS order_items(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      order_id INTEGER NOT NULL,
      product_id INTEGER NOT NULL,
      product_name TEXT NOT NULL,
      spec TEXT DEFAULT '',
      qty INTEGER NOT NULL,
      original_price REAL NOT NULL,
      discount_price REAL NOT NULL,
      retail_price REAL NOT NULL,
      amount REAL NOT NULL,
      FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
      FOREIGN KEY(product_id) REFERENCES products(id)
    );
    ''')
    if conn.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 0:
        conn.executemany('INSERT INTO products(name,spec,unit,original_price,discount_price,retail_price,stock) VALUES(?,?,?,?,?,?,?)', [
            ('示例商品A','100ml','瓶',58,49,69,120),
            ('示例商品B','10片/盒','盒',36,31,45,80),
            ('示例商品C','500g','袋',88,76,99,56),
        ])
    if conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 0:
        conn.executemany('INSERT INTO customers(name,phone,address) VALUES(?,?,?)', [
            ('示例客户一','13800000001','示例地址A'),('示例客户二','13800000002','示例地址B')])
    if conn.execute('SELECT COUNT(*) FROM handlers').fetchone()[0] == 0:
        conn.executemany('INSERT INTO handlers(name) VALUES(?)', [('经办人A',),('经办人B',)])
    conn.commit(); conn.close()


def esc(v): return html.escape('' if v is None else str(v))
def money(v): return f'{float(v or 0):.2f}'

def page(title, body, active=''):
    nav = [('dashboard','首页','/'),('sale','销售开单','/sale'),('orders','历史销售单','/orders'),('products','商品管理','/products'),('customers','客户管理','/customers'),('handlers','经办人管理','/handlers')]
    nav_html=''.join(f'<a class="{"active" if k==active else ""}" href="{u}">{t}</a>' for k,t,u in nav)
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} - 销售与库存管理系统</title><link rel="stylesheet" href="/static/style.css"></head><body><header><div class="brand"><span class="brand-mark">S</span><div><b>销售与库存管理系统</b><small>Portfolio Demo</small></div></div><nav>{nav_html}</nav></header><main>{body}</main><footer>本项目为个人作品展示版，所有名称、数据均为虚构示例。</footer></body></html>'''


def parse_post(handler):
    length=int(handler.headers.get('Content-Length','0') or 0)
    raw=handler.rfile.read(length).decode('utf-8')
    return parse_qs(raw, keep_blank_values=True)

class App(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass
    def send_html(self, content, code=200):
        data=content.encode('utf-8'); self.send_response(code); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def redirect(self, url):
        self.send_response(303); self.send_header('Location',url); self.end_headers()
    def do_GET(self):
        p=urlparse(self.path); path=p.path; q=parse_qs(p.query)
        if path.startswith('/static/'):
            fp=ROOT/path.lstrip('/')
            if fp.exists():
                data=fp.read_bytes(); self.send_response(200); self.send_header('Content-Type','text/css; charset=utf-8'); self.end_headers(); self.wfile.write(data); return
            self.send_error(404); return
        if path=='/': return self.dashboard()
        if path=='/sale': return self.sale()
        if path=='/orders': return self.orders(q)
        if path.startswith('/orders/') and path.endswith('/print'):
            try: oid=int(path.split('/')[2]); return self.print_order(oid)
            except: return self.send_error(404)
        if path=='/products': return self.products()
        if path=='/customers': return self.customers()
        if path=='/handlers': return self.handlers()
        self.send_error(404)
    def do_POST(self):
        path=urlparse(self.path).path; f=parse_post(self)
        try:
            if path=='/sale': return self.create_order(f)
            if path=='/products/add': return self.add_product(f)
            if path=='/customers/add': return self.add_customer(f)
            if path=='/handlers/add': return self.add_handler(f)
            if path.startswith('/orders/') and path.endswith('/void'):
                return self.void_order(int(path.split('/')[2]))
        except Exception as e:
            return self.send_html(page('操作失败',f'<section class="card"><h2>操作失败</h2><p class="error">{esc(e)}</p><a class="btn" href="javascript:history.back()">返回</a></section>'),500)
        self.send_error(404)

    def dashboard(self):
        conn=db();
        today=date.today().isoformat()
        stats={
          'products':conn.execute('select count(*) from products where enabled=1').fetchone()[0],
          'customers':conn.execute('select count(*) from customers').fetchone()[0],
          'orders':conn.execute("select count(*) from orders where status='有效'").fetchone()[0],
          'today':conn.execute("select coalesce(sum(total),0) from orders where order_date=? and status='有效'",(today,)).fetchone()[0]
        }
        low=conn.execute('select * from products where stock<60 order by stock asc limit 5').fetchall(); conn.close()
        cards=f'''<div class="hero"><div><span class="eyebrow">轻量级业务管理 Demo</span><h1>销售开单、库存联动与历史查询</h1><p>面向小型业务场景的本地 Web 管理系统，覆盖开单、客户、经办人、库存与打印。</p></div><a class="btn primary" href="/sale">新建销售单</a></div><div class="stats"><div><b>{stats['products']}</b><span>在售商品</span></div><div><b>{stats['customers']}</b><span>客户数量</span></div><div><b>{stats['orders']}</b><span>有效订单</span></div><div><b>¥{money(stats['today'])}</b><span>今日销售额</span></div></div>'''
        rows=''.join(f'<tr><td>{esc(x["name"])}</td><td>{esc(x["spec"])}</td><td>{x["stock"]}</td></tr>' for x in low) or '<tr><td colspan=3>暂无</td></tr>'
        body=cards+f'<section class="card"><div class="section-title"><h2>库存提示</h2><a href="/products">查看全部</a></div><table><thead><tr><th>商品</th><th>规格</th><th>库存</th></tr></thead><tbody>{rows}</tbody></table></section>'
        self.send_html(page('首页',body,'dashboard'))

    def sale(self):
        conn=db(); products=conn.execute('select * from products where enabled=1 order by id').fetchall(); customers=conn.execute('select * from customers order by id desc').fetchall(); handlers=conn.execute('select * from handlers order by id').fetchall(); conn.close()
        pdata=[dict(x) for x in products]
        options=''.join(f'<option value="{x["id"]}">{esc(x["name"])} / 库存 {x["stock"]}</option>' for x in products)
        body=f'''<section class="card"><div class="section-title"><h2>新建销售单</h2><span class="muted">库存将在保存订单后自动扣减</span></div><form method="post" id="saleForm"><div class="grid4"><label>开单日期<input type="date" name="order_date" value="{date.today().isoformat()}" required></label><label>客户<select name="customer_id" required><option value="">请选择</option>{''.join(f'<option value="{x["id"]}">{esc(x["name"])}</option>' for x in customers)}</select></label><label>经办人<select name="handler_id" required><option value="">请选择</option>{''.join(f'<option value="{x["id"]}">{esc(x["name"])}</option>' for x in handlers)}</select></label><label>发货方式<select name="delivery_method"><option>自提</option><option>快递</option><option>同城配送</option></select></label></div><label>摘要<input name="summary" placeholder="可填写订单备注"></label><div class="section-title"><h3>商品明细</h3><button type="button" class="btn small" onclick="addRow()">+ 添加商品</button></div><div class="table-wrap"><table id="items"><thead><tr><th>商品</th><th>数量</th><th>原价</th><th>折后价</th><th>建议零售价</th><th>小计</th><th></th></tr></thead><tbody></tbody><tfoot><tr><td colspan="5" class="right"><b>合计</b></td><td><b id="grand">¥0.00</b></td><td></td></tr></tfoot></table></div><div class="actions"><button class="btn primary" type="submit">保存销售单</button></div></form></section>
<script>const products={json.dumps(pdata,ensure_ascii=False)};let idx=0;function addRow(){{let i=idx++;let tr=document.createElement('tr');tr.innerHTML=`<td><select name="product_id" onchange="fill(this)" required><option value="">请选择商品</option>{options}</select></td><td><input name="qty" type="number" min="1" value="1" oninput="calc(this)" required></td><td><input name="original_price" type="number" step="0.01" readonly></td><td><input name="discount_price" type="number" step="0.01" oninput="calc(this)" required></td><td><input name="retail_price" type="number" step="0.01" readonly></td><td class="amount">¥0.00</td><td><button class="link danger" type="button" onclick="this.closest('tr').remove();total()">删除</button></td>`;document.querySelector('#items tbody').appendChild(tr)}}function fill(s){{let p=products.find(x=>String(x.id)===s.value),tr=s.closest('tr');if(!p)return;tr.querySelector('[name=original_price]').value=p.original_price;tr.querySelector('[name=discount_price]').value=p.discount_price;tr.querySelector('[name=retail_price]').value=p.retail_price;calc(s)}}function calc(el){{let tr=el.closest('tr'),q=+tr.querySelector('[name=qty]').value||0,p=+tr.querySelector('[name=discount_price]').value||0;tr.querySelector('.amount').textContent='¥'+(q*p).toFixed(2);total()}}function total(){{let v=0;document.querySelectorAll('#items tbody tr').forEach(tr=>v+=(+tr.querySelector('[name=qty]').value||0)*(+tr.querySelector('[name=discount_price]').value||0));document.querySelector('#grand').textContent='¥'+v.toFixed(2)}}addRow();</script>'''
        self.send_html(page('销售开单',body,'sale'))

    def create_order(self,f):
        pids=f.get('product_id',[]); qtys=f.get('qty',[]); dps=f.get('discount_price',[])
        if not pids: raise ValueError('至少添加一项商品')
        conn=db(); cur=conn.cursor(); now=datetime.now(); no=now.strftime('SO%Y%m%d%H%M%S%f')[:-3]
        items=[]; total=0
        try:
            cur.execute('BEGIN IMMEDIATE')
            for pid,q,dp in zip(pids,qtys,dps):
                prod=cur.execute('select * from products where id=?',(int(pid),)).fetchone(); q=int(q); dp=float(dp)
                if not prod: raise ValueError('商品不存在')
                if q<=0: raise ValueError('数量必须大于0')
                if prod['stock']<q: raise ValueError(f'{prod["name"]} 库存不足，当前库存 {prod["stock"]}')
                amt=q*dp; total+=amt; items.append((prod,q,dp,amt))
            cur.execute('insert into orders(order_no,order_date,customer_id,handler_id,delivery_method,summary,total,created_at) values(?,?,?,?,?,?,?,?)',(no,f['order_date'][0],int(f['customer_id'][0]),int(f['handler_id'][0]),f.get('delivery_method',[''])[0],f.get('summary',[''])[0],total,now.isoformat(timespec='seconds')))
            oid=cur.lastrowid
            for prod,q,dp,amt in items:
                cur.execute('insert into order_items(order_id,product_id,product_name,spec,qty,original_price,discount_price,retail_price,amount) values(?,?,?,?,?,?,?,?,?)',(oid,prod['id'],prod['name'],prod['spec'],q,prod['original_price'],dp,prod['retail_price'],amt))
                cur.execute('update products set stock=stock-? where id=?',(q,prod['id']))
            conn.commit()
        except: conn.rollback(); conn.close(); raise
        conn.close(); self.redirect(f'/orders/{oid}/print')

    def orders(self,q):
        start=q.get('start',[''])[0]; end=q.get('end',[''])[0]; customer=q.get('customer',[''])[0].strip(); handler=q.get('handler',[''])[0].strip()
        sql='''select o.*,c.name customer_name,h.name handler_name from orders o left join customers c on c.id=o.customer_id left join handlers h on h.id=o.handler_id where 1=1'''; args=[]
        if start: sql+=' and o.order_date>=?'; args.append(start)
        if end: sql+=' and o.order_date<=?'; args.append(end)
        if customer: sql+=' and c.name like ?'; args.append('%'+customer+'%')
        if handler: sql+=' and h.name like ?'; args.append('%'+handler+'%')
        sql+=' order by o.id desc'
        conn=db(); rows=conn.execute(sql,args).fetchall(); conn.close()
        trs=''.join(f'''<tr><td>{esc(r['order_no'])}</td><td>{esc(r['order_date'])}</td><td>{esc(r['customer_name'])}</td><td>{esc(r['handler_name'])}</td><td>¥{money(r['total'])}</td><td><span class="badge {'void' if r['status']=='作废' else ''}">{r['status']}</span></td><td><a class="link" href="/orders/{r['id']}/print">查看/打印</a>{'' if r['status']=='作废' else f'<form class="inline" method="post" action="/orders/{r["id"]}/void" onsubmit="return confirm(\'作废后将自动回补库存，确认继续？\')"><button class="link danger">作废</button></form>'}</td></tr>''' for r in rows) or '<tr><td colspan="7">暂无符合条件的销售单</td></tr>'
        body=f'''<section class="card"><div class="section-title"><h2>历史销售单</h2><span class="muted">支持日期 / 客户名称 / 经办人组合查询</span></div><form class="filters" method="get"><label>开始日期<input type="date" name="start" value="{esc(start)}"></label><label>结束日期<input type="date" name="end" value="{esc(end)}"></label><label>客户名称<input name="customer" value="{esc(customer)}" placeholder="模糊搜索"></label><label>经办人<input name="handler" value="{esc(handler)}" placeholder="模糊搜索"></label><button class="btn primary">搜索</button><a class="btn" href="/orders">重置</a></form><div class="table-wrap"><table><thead><tr><th>单据编号</th><th>日期</th><th>客户</th><th>经办人</th><th>金额</th><th>状态</th><th>操作</th></tr></thead><tbody>{trs}</tbody></table></div></section>'''
        self.send_html(page('历史销售单',body,'orders'))

    def void_order(self,oid):
        conn=db(); cur=conn.cursor(); cur.execute('BEGIN IMMEDIATE'); order=cur.execute('select * from orders where id=?',(oid,)).fetchone()
        if not order or order['status']=='作废': conn.rollback(); conn.close(); return self.redirect('/orders')
        items=cur.execute('select * from order_items where order_id=?',(oid,)).fetchall()
        for it in items: cur.execute('update products set stock=stock+? where id=?',(it['qty'],it['product_id']))
        cur.execute("update orders set status='作废' where id=?",(oid,)); conn.commit(); conn.close(); self.redirect('/orders')

    def print_order(self,oid):
        conn=db(); o=conn.execute('''select o.*,c.name customer_name,c.phone,c.address,h.name handler_name from orders o left join customers c on c.id=o.customer_id left join handlers h on h.id=o.handler_id where o.id=?''',(oid,)).fetchone(); items=conn.execute('select * from order_items where order_id=?',(oid,)).fetchall(); conn.close()
        if not o: return self.send_error(404)
        rows=''.join(f'<tr><td>{i+1}</td><td>{esc(x["product_name"])}</td><td>{esc(x["spec"])}</td><td>{x["qty"]}</td><td>{money(x["original_price"])}</td><td>{money(x["discount_price"])}</td><td>{money(x["retail_price"])}</td><td>{money(x["amount"])}</td></tr>' for i,x in enumerate(items))
        doc=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{esc(o['order_no'])}</title><link rel="stylesheet" href="/static/style.css"></head><body class="print-body"><div class="paper"><div class="receipt-head"><div><h1>销售单</h1><p>Sales Order</p></div><div class="status-mark">{esc(o['status'])}</div></div><div class="info-grid"><div><b>发货方式：</b>{esc(o['delivery_method'])}</div><div><b>开单日期：</b>{esc(o['order_date'])}</div><div><b>单据编号：</b>{esc(o['order_no'])}</div><div><b>客户名称：</b>{esc(o['customer_name'])}</div><div><b>联系电话：</b>{esc(o['phone'])}</div><div><b>经办人：</b>{esc(o['handler_name'])}</div><div class="span2"><b>摘要：</b>{esc(o['summary']) or '-'}</div><div class="span2"><b>联系地址：</b>{esc(o['address']) or '-'}</div></div><table class="receipt-table"><thead><tr><th>序号</th><th>商品名称</th><th>规格</th><th>数量</th><th>原价</th><th>折后价</th><th>建议零售价</th><th>金额</th></tr></thead><tbody>{rows}</tbody><tfoot><tr><td colspan="7" class="right"><b>合计</b></td><td><b>¥{money(o['total'])}</b></td></tr></tfoot></table><div class="receipt-foot"><span>说明：本页面为作品集演示版销售单，不包含任何真实企业信息。</span><span>打印日期：{datetime.now().strftime('%Y-%m-%d')}</span></div><div class="print-actions"><button onclick="window.print()" class="btn primary">打印 / 保存为 PDF</button><a class="btn" href="/orders">返回历史销售单</a></div></div></body></html>'''
        self.send_html(doc)

    def products(self):
        conn=db(); rows=conn.execute('select * from products order by id desc').fetchall(); conn.close()
        trs=''.join(f'<tr><td>{esc(r["name"])}</td><td>{esc(r["spec"])}</td><td>{esc(r["unit"])}</td><td>¥{money(r["original_price"])}</td><td>¥{money(r["discount_price"])}</td><td>¥{money(r["retail_price"])}</td><td>{r["stock"]}</td></tr>' for r in rows)
        body=f'''<section class="card"><div class="section-title"><h2>商品管理</h2><span class="muted">三档价格 + 实时库存</span></div><form method="post" action="/products/add" class="quick-form"><input name="name" placeholder="商品名称" required><input name="spec" placeholder="规格"><input name="unit" placeholder="单位" value="件"><input name="original_price" type="number" step="0.01" placeholder="原价" required><input name="discount_price" type="number" step="0.01" placeholder="折后价" required><input name="retail_price" type="number" step="0.01" placeholder="建议零售价" required><input name="stock" type="number" placeholder="库存" required><button class="btn primary">新增商品</button></form><div class="table-wrap"><table><thead><tr><th>商品</th><th>规格</th><th>单位</th><th>原价</th><th>折后价</th><th>建议零售价</th><th>库存</th></tr></thead><tbody>{trs}</tbody></table></div></section>'''; self.send_html(page('商品管理',body,'products'))
    def add_product(self,f):
        conn=db(); conn.execute('insert into products(name,spec,unit,original_price,discount_price,retail_price,stock) values(?,?,?,?,?,?,?)',(f['name'][0],f.get('spec',[''])[0],f.get('unit',['件'])[0],float(f['original_price'][0]),float(f['discount_price'][0]),float(f['retail_price'][0]),int(f['stock'][0]))); conn.commit(); conn.close(); self.redirect('/products')
    def customers(self):
        conn=db(); rows=conn.execute('select * from customers order by id desc').fetchall(); conn.close(); trs=''.join(f'<tr><td>{esc(r["name"])}</td><td>{esc(r["phone"])}</td><td>{esc(r["address"])}</td></tr>' for r in rows)
        body=f'''<section class="card"><div class="section-title"><h2>客户管理</h2></div><form method="post" action="/customers/add" class="quick-form three"><input name="name" placeholder="客户名称" required><input name="phone" placeholder="联系电话"><input name="address" placeholder="联系地址"><button class="btn primary">新增客户</button></form><table><thead><tr><th>客户名称</th><th>联系电话</th><th>联系地址</th></tr></thead><tbody>{trs}</tbody></table></section>'''; self.send_html(page('客户管理',body,'customers'))
    def add_customer(self,f):
        conn=db(); conn.execute('insert into customers(name,phone,address) values(?,?,?)',(f['name'][0],f.get('phone',[''])[0],f.get('address',[''])[0])); conn.commit(); conn.close(); self.redirect('/customers')
    def handlers(self):
        conn=db(); rows=conn.execute('select * from handlers order by id desc').fetchall(); conn.close(); trs=''.join(f'<tr><td>{esc(r["name"])}</td></tr>' for r in rows)
        body=f'''<section class="card"><div class="section-title"><h2>经办人管理</h2></div><form method="post" action="/handlers/add" class="quick-form one"><input name="name" placeholder="经办人姓名" required><button class="btn primary">新增经办人</button></form><table><thead><tr><th>姓名</th></tr></thead><tbody>{trs}</tbody></table></section>'''; self.send_html(page('经办人管理',body,'handlers'))
    def add_handler(self,f):
        conn=db(); conn.execute('insert or ignore into handlers(name) values(?)',(f['name'][0],)); conn.commit(); conn.close(); self.redirect('/handlers')


def run(open_browser=False):
    init_db(); PID_FILE.write_text(str(os.getpid()),encoding='utf-8')
    if open_browser:
        threading.Thread(target=lambda:(time.sleep(0.8),webbrowser.open(f'http://{HOST}:{PORT}')),daemon=True).start()
    server=ThreadingHTTPServer((HOST,PORT),App)
    print(f'Portfolio Sales Web running at http://{HOST}:{PORT}')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        if PID_FILE.exists(): PID_FILE.unlink()

if __name__=='__main__':
    run('--open-browser' in sys.argv)
