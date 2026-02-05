from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware

from typing import List, Tuple, Optional
from datetime import date
from io import BytesIO
import math
import statistics
import json
import os

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from db import init_db, get_conn
from auth import get_or_create_user_id, login_user, logout_user, require_login

app = FastAPI()

# Render'da ENV olarak set etmen önerilir
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
SHARED_PASSWORD = os.environ.get("SHARED_PASSWORD", "123456")  # herkesin ortak şifresi

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)


@app.on_event("startup")
def startup():
    init_db()


# -----------------------
# Date helpers
# -----------------------
def last_n_calendar_months(n: int) -> List[Tuple[int, int]]:
    today = date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


def month_label(y: int, m: int) -> str:
    return f"{y}-{m:02d}"


# -----------------------
# Calculation engine
# -----------------------
def compute_from_last_months(
    lead_time: int,
    z: float,
    monthly_units: List[int],
    fba_stock: int,
    inbound_stock: int
) -> dict:
    if not monthly_units:
        return {"daily_velocity": 0.0, "std_daily": 0.0, "safety_stock": 0.0, "rop": 0.0, "order_qty": 0.0}

    mean_month = sum(monthly_units) / len(monthly_units)
    daily_velocity = mean_month / 30.0

    if len(monthly_units) < 2:
        std_daily = 0.0
    else:
        std_daily = statistics.stdev(monthly_units) / 30.0

    safety_stock = z * std_daily * math.sqrt(max(1, lead_time))
    rop = daily_velocity * lead_time + safety_stock
    order_qty = max(0.0, daily_velocity * 60 + safety_stock - (fba_stock + inbound_stock))

    return {
        "daily_velocity": daily_velocity,
        "std_daily": std_daily,
        "safety_stock": safety_stock,
        "rop": rop,
        "order_qty": order_qty,
    }


# -----------------------
# Excel helpers
# -----------------------
def autosize_columns(ws):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val = "" if cell.value is None else str(cell.value)
            max_len = max(max_len, len(val))
        ws.column_dimensions[col_letter].width = min(45, max(10, max_len + 2))


def workbook_to_stream(wb: Workbook) -> BytesIO:
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio


# -----------------------
# DB ops (OWNER-AWARE)
# -----------------------
def upsert_product(cur, owner_id: int, sku: str, name: str, lead_time_days: int, z_value: float, fba_stock: int, inbound_stock: int):
    cur.execute("""
    INSERT INTO products(owner_id, sku, name, lead_time_days, z_value, fba_stock, inbound_stock)
    VALUES(?,?,?,?,?,?,?)
    ON CONFLICT(owner_id, sku) DO UPDATE SET
      name=excluded.name,
      lead_time_days=excluded.lead_time_days,
      z_value=excluded.z_value,
      fba_stock=excluded.fba_stock,
      inbound_stock=excluded.inbound_stock,
      updated_at=CURRENT_TIMESTAMP;
    """, (owner_id, sku, name, lead_time_days, z_value, fba_stock, inbound_stock))


def upsert_monthly_sales(cur, owner_id: int, sku: str, years: List[int], months: List[int], units_sold: List[int]):
    for y, m, u in zip(years, months, units_sold):
        cur.execute("""
        INSERT INTO monthly_sales(owner_id, sku, year, month, units_sold)
        VALUES(?,?,?,?,?)
        ON CONFLICT(owner_id, sku, year, month) DO UPDATE SET
          units_sold=excluded.units_sold,
          created_at=CURRENT_TIMESTAMP;
        """, (owner_id, sku, int(y), int(m), int(u)))


def fetch_product(cur, owner_id: int, sku: str):
    return cur.execute("SELECT * FROM products WHERE owner_id=? AND sku=?", (owner_id, sku)).fetchone()


def fetch_month_units(cur, owner_id: int, sku: str, y: int, m: int) -> Optional[int]:
    row = cur.execute("""
      SELECT units_sold FROM monthly_sales
      WHERE owner_id=? AND sku=? AND year=? AND month=?
    """, (owner_id, sku, y, m)).fetchone()
    return None if row is None else int(row["units_sold"])


def delete_product_and_sales(cur, owner_id: int, sku: str):
    cur.execute("DELETE FROM monthly_sales WHERE owner_id=? AND sku=?", (owner_id, sku))
    cur.execute("DELETE FROM products WHERE owner_id=? AND sku=?", (owner_id, sku))


def delete_single_month_sale(cur, owner_id: int, sku: str, year: int, month: int):
    cur.execute("""
      DELETE FROM monthly_sales
      WHERE owner_id=? AND sku=? AND year=? AND month=?
    """, (owner_id, sku, year, month))


def compute_for_sku(cur, owner_id: int, sku: str):
    prod = fetch_product(cur, owner_id, sku)
    if prod is None:
        return None

    last3 = last_n_calendar_months(3)
    monthly_units_found: List[int] = []
    for y, m in last3:
        u = fetch_month_units(cur, owner_id, sku, y, m)
        if u is not None:
            monthly_units_found.append(u)

    res = compute_from_last_months(
        lead_time=int(prod["lead_time_days"]),
        z=float(prod["z_value"]),
        monthly_units=monthly_units_found,
        fba_stock=int(prod["fba_stock"]),
        inbound_stock=int(prod["inbound_stock"])
    )
    return prod, last3, monthly_units_found, res


# -----------------------
# UI
# -----------------------
def page_shell(title: str, body_html: str) -> str:
    return f"""
<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <title>{title}</title>
</head>
<body class="min-h-screen bg-gradient-to-br from-slate-950 via-slate-900 to-slate-950 text-slate-100">
  <div class="max-w-6xl mx-auto px-6 py-10">
    <div class="flex items-start justify-between gap-6 flex-wrap">
      <div>
        <h1 class="text-3xl md:text-4xl font-bold tracking-tight">{title}</h1>
        <p class="text-slate-300 mt-2">Ortak şifreli login + kullanıcıya özel veriler + silme işlemleri.</p>
      </div>
      <div class="flex gap-2 flex-wrap">
        <a class="px-4 py-2 rounded-2xl text-sm border bg-slate-900/40 border-slate-700/60 hover:bg-slate-900/60" href="/login">Giriş</a>
        <a class="px-4 py-2 rounded-2xl text-sm border bg-slate-900/40 border-slate-700/60 hover:bg-slate-900/60" href="/">Ekle</a>
        <a class="px-4 py-2 rounded-2xl text-sm border bg-slate-900/40 border-slate-700/60 hover:bg-slate-900/60" href="/products">SKU</a>
        <a class="px-4 py-2 rounded-2xl text-sm border bg-slate-900/40 border-slate-700/60 hover:bg-slate-900/60" href="/plan">Plan</a>
        <a class="px-4 py-2 rounded-2xl text-sm border bg-slate-900/40 border-slate-700/60 hover:bg-slate-900/60" href="/logout">Çıkış</a>
      </div>
    </div>

    <div class="mt-8">
      {body_html}
    </div>
  </div>
</body>
</html>
"""


def build_default_rows_html() -> str:
    defaults = last_n_calendar_months(3)
    rows = []
    for y, m in defaults:
        rows.append(f"""
        <tr class="border-b border-slate-700/50">
          <td class="py-2"><input class="w-full rounded-xl bg-slate-900/60 border border-slate-700 px-3 py-2" name="years" type="number" value="{y}" required></td>
          <td class="py-2"><input class="w-full rounded-xl bg-slate-900/60 border border-slate-700 px-3 py-2" name="months" type="number" min="1" max="12" value="{m}" required></td>
          <td class="py-2"><input class="w-full rounded-xl bg-slate-900/60 border border-slate-700 px-3 py-2" name="units_sold" type="number" min="0" placeholder="Adet" required></td>
          <td class="py-2 text-right">
            <button type="button" class="remove-row text-sm px-3 py-2 rounded-xl bg-rose-500/15 text-rose-200 border border-rose-500/30 hover:bg-rose-500/25">Sil</button>
          </td>
        </tr>
        """)
    return "\n".join(rows)


# -----------------------
# LOGIN (No Register)
# -----------------------
@app.get("/login", response_class=HTMLResponse)
def login_page():
    body = f"""
<div class="max-w-lg mx-auto rounded-3xl border border-slate-700/60 bg-slate-900/40 p-6">
  <h2 class="text-xl font-semibold">Giriş</h2>
  <p class="text-slate-400 text-sm mt-2">
    Herkes aynı şifre ile giriş yapar. Sadece kullanıcı adı/e-mail farklı olur.
  </p>

  <form class="mt-4 space-y-3" method="post" action="/login">
    <input class="w-full rounded-2xl bg-slate-900/60 border border-slate-700 px-4 py-3"
           name="identifier" type="text" placeholder="E-mail veya Username" required>
    <input class="w-full rounded-2xl bg-slate-900/60 border border-slate-700 px-4 py-3"
           name="password" type="password" placeholder="Ortak Şifre" required>
    <button class="w-full rounded-2xl bg-indigo-500/90 hover:bg-indigo-500 px-5 py-3 font-semibold" type="submit">
      Giriş Yap
    </button>
  </form>
</div>
"""
    return page_shell("Login", body)


@app.post("/login", response_class=HTMLResponse)
def login_action(request: Request, identifier: str = Form(...), password: str = Form(...)):
    if password != SHARED_PASSWORD:
        return page_shell("Login", "<div class='max-w-lg mx-auto p-6 rounded-3xl border bg-slate-900/40 border-slate-700/60'>Şifre hatalı. <a class='underline' href='/login'>Tekrar dene</a></div>")

    user_id = get_or_create_user_id(identifier)
    login_user(request, user_id)
    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)


# -----------------------
# MAIN FORM (LOGIN REQUIRED)
# -----------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    owner_id = require_login(request)
    if not owner_id:
        return RedirectResponse(url="/login", status_code=303)

    body = f"""
<form method="post" action="/upsert" class="grid grid-cols-1 lg:grid-cols-3 gap-6">
  <div class="lg:col-span-1 rounded-3xl border border-slate-700/60 bg-slate-900/40 p-6 shadow-lg">
    <h2 class="text-xl font-semibold">Ürün Bilgileri</h2>
    <div class="mt-4 space-y-3">
      <input name="sku" required class="w-full rounded-2xl bg-slate-900/60 border border-slate-700 px-4 py-3" placeholder="SKU"/>
      <input name="name" class="w-full rounded-2xl bg-slate-900/60 border border-slate-700 px-4 py-3" placeholder="Ürün adı (ops.)"/>
      <div class="grid grid-cols-2 gap-3">
        <input name="lead_time_days" type="number" min="1" required class="w-full rounded-2xl bg-slate-900/60 border border-slate-700 px-4 py-3" placeholder="Lead time (gün)"/>
        <input name="z_value" type="number" step="0.01" required class="w-full rounded-2xl bg-slate-900/60 border border-slate-700 px-4 py-3" placeholder="Z (örn 1.65)"/>
      </div>
      <div class="grid grid-cols-2 gap-3">
        <input name="fba_stock" type="number" min="0" value="0" required class="w-full rounded-2xl bg-slate-900/60 border border-slate-700 px-4 py-3" placeholder="FBA"/>
        <input name="inbound_stock" type="number" min="0" value="0" required class="w-full rounded-2xl bg-slate-900/60 border border-slate-700 px-4 py-3" placeholder="Yoldaki"/>
      </div>
    </div>
    <button type="submit" class="mt-6 w-full rounded-2xl bg-indigo-500/90 hover:bg-indigo-500 px-5 py-3 font-semibold">Kaydet + Hesapla</button>
  </div>

  <div class="lg:col-span-2 rounded-3xl border border-slate-700/60 bg-slate-900/40 p-6 shadow-lg">
    <div class="flex items-center justify-between gap-3 flex-wrap">
      <h2 class="text-xl font-semibold">Aylık Satış (Çoklu)</h2>
      <button type="button" id="addRow" class="rounded-2xl bg-emerald-500/15 text-emerald-200 border border-emerald-500/30 hover:bg-emerald-500/25 px-4 py-2 text-sm font-semibold">+ Ay Ekle</button>
    </div>

    <div class="mt-4 overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="text-slate-300">
          <tr class="border-b border-slate-700/60">
            <th class="text-left py-2 pr-2">Yıl</th>
            <th class="text-left py-2 pr-2">Ay</th>
            <th class="text-left py-2 pr-2">Satış</th>
            <th class="text-right py-2">İşlem</th>
          </tr>
        </thead>
        <tbody id="salesBody">
          {build_default_rows_html()}
        </tbody>
      </table>
    </div>

    <p class="text-slate-400 text-sm mt-3">Not: Aynı (yıl, ay) girersen mevcut kaydı günceller.</p>
  </div>
</form>

<script>
  const salesBody = document.getElementById("salesBody");
  const addBtn = document.getElementById("addRow");

  function attachRemoveHandlers() {{
    document.querySelectorAll(".remove-row").forEach(btn => {{
      btn.onclick = (e) => {{
        const tr = e.target.closest("tr");
        if (salesBody.querySelectorAll("tr").length > 1) tr.remove();
      }};
    }});
  }}

  addBtn.addEventListener("click", () => {{
    const now = new Date();
    const y = now.getFullYear();
    const m = now.getMonth() + 1;

    const tr = document.createElement("tr");
    tr.className = "border-b border-slate-700/50";
    tr.innerHTML = `
      <td class="py-2"><input class="w-full rounded-xl bg-slate-900/60 border border-slate-700 px-3 py-2" name="years" type="number" value="${{y}}" required></td>
      <td class="py-2"><input class="w-full rounded-xl bg-slate-900/60 border border-slate-700 px-3 py-2" name="months" type="number" min="1" max="12" value="${{m}}" required></td>
      <td class="py-2"><input class="w-full rounded-xl bg-slate-900/60 border border-slate-700 px-3 py-2" name="units_sold" type="number" min="0" placeholder="Adet" required></td>
      <td class="py-2 text-right">
        <button type="button" class="remove-row text-sm px-3 py-2 rounded-xl bg-rose-500/15 text-rose-200 border border-rose-500/30 hover:bg-rose-500/25">Sil</button>
      </td>`;
    salesBody.prepend(tr);
    attachRemoveHandlers();
  }});

  attachRemoveHandlers();
</script>
"""
    return page_shell("Amazon Stok Planlama", body)


@app.post("/upsert", response_class=HTMLResponse)
def upsert(
    request: Request,
    sku: str = Form(...),
    name: str = Form(""),
    lead_time_days: int = Form(...),
    z_value: float = Form(...),
    fba_stock: int = Form(0),
    inbound_stock: int = Form(0),
    years: List[int] = Form(...),
    months: List[int] = Form(...),
    units_sold: List[int] = Form(...)
):
    owner_id = require_login(request)
    if not owner_id:
        return RedirectResponse(url="/login", status_code=303)

    sku = sku.strip()
    conn = get_conn()
    cur = conn.cursor()
    upsert_product(cur, int(owner_id), sku, name.strip(), int(lead_time_days), float(z_value), int(fba_stock), int(inbound_stock))
    upsert_monthly_sales(cur, int(owner_id), sku, years, months, units_sold)
    conn.commit()
    conn.close()

    return RedirectResponse(url=f"/product/{sku}", status_code=303)


# -----------------------
# DELETE ACTIONS (OWNER-AWARE)
# -----------------------
@app.post("/delete/product/{sku}")
def delete_product(request: Request, sku: str):
    owner_id = require_login(request)
    if not owner_id:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_conn()
    cur = conn.cursor()
    delete_product_and_sales(cur, int(owner_id), sku)
    conn.commit()
    conn.close()

    return RedirectResponse(url="/products", status_code=303)


@app.post("/delete/sale")
def delete_sale(
    request: Request,
    sku: str = Form(...),
    year: int = Form(...),
    month: int = Form(...)
):
    owner_id = require_login(request)
    if not owner_id:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_conn()
    cur = conn.cursor()
    delete_single_month_sale(cur, int(owner_id), sku, int(year), int(month))
    conn.commit()
    conn.close()

    return RedirectResponse(url=f"/product/{sku}", status_code=303)


# -----------------------
# SKU LIST
# -----------------------
@app.get("/products", response_class=HTMLResponse)
def products(request: Request):
    owner_id = require_login(request)
    if not owner_id:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_conn()
    cur = conn.cursor()
    skus = [r["sku"] for r in cur.execute(
        "SELECT sku FROM products WHERE owner_id=? ORDER BY updated_at DESC",
        (int(owner_id),)
    ).fetchall()]

    rows = []
    for sku in skus:
        prod, _, _, res = compute_for_sku(cur, int(owner_id), sku)
        order_qty = int(round(res["order_qty"]))
        badge = "bg-emerald-500/15 text-emerald-200 border-emerald-500/30" if order_qty <= 0 else "bg-rose-500/15 text-rose-200 border-rose-500/30"

        rows.append(f"""
        <tr class="border-b border-slate-700/50">
          <td class="py-3 pr-3 font-semibold">{sku}</td>
          <td class="py-3 pr-3 text-slate-300">{prod["name"] or ""}</td>
          <td class="py-3 pr-3 text-right">{res["daily_velocity"]:.2f}</td>
          <td class="py-3 pr-3 text-right">{res["rop"]:.2f}</td>
          <td class="py-3 pr-3 text-right"><span class="px-3 py-1 rounded-2xl border {badge}">{order_qty}</span></td>
          <td class="py-3 text-right">
            <div class="flex justify-end gap-2">
              <a class="px-4 py-2 rounded-2xl text-sm border border-slate-700/60 bg-slate-900/40 hover:bg-slate-900/60" href="/product/{sku}">Detay</a>
              <form method="post" action="/delete/product/{sku}" onsubmit="return confirm('SKU {sku} ve TÜM satış kayıtları silinsin mi?');">
                <button type="submit" class="px-4 py-2 rounded-2xl text-sm border border-rose-500/30 bg-rose-500/15 text-rose-200 hover:bg-rose-500/25">Sil</button>
              </form>
            </div>
          </td>
        </tr>
        """)

    conn.close()

    body = f"""
<div class="rounded-3xl border border-slate-700/60 bg-slate-900/40 p-6 shadow-lg">
  <div class="flex items-end justify-between flex-wrap gap-3">
    <div>
      <h2 class="text-xl font-semibold">SKU Listesi</h2>
      <p class="text-slate-400 text-sm mt-1">Sadece kendi verilerin görünür.</p>
    </div>

    <div class="flex gap-2 flex-wrap">
      <a href="/export/products.xlsx" class="rounded-2xl border border-slate-700/60 bg-slate-900/40 hover:bg-slate-900/60 px-4 py-2 text-sm font-semibold">Excel (SKU)</a>
      <a href="/export/plan.xlsx" class="rounded-2xl border border-slate-700/60 bg-slate-900/40 hover:bg-slate-900/60 px-4 py-2 text-sm font-semibold">Excel (Plan)</a>
    </div>
  </div>

  <div class="mt-5 overflow-x-auto">
    <table class="w-full text-sm">
      <thead class="text-slate-300">
        <tr class="border-b border-slate-700/60">
          <th class="text-left py-2 pr-3">SKU</th>
          <th class="text-left py-2 pr-3">Ürün</th>
          <th class="text-right py-2 pr-3">Günlük Hız</th>
          <th class="text-right py-2 pr-3">ROP</th>
          <th class="text-right py-2 pr-3">Sipariş</th>
          <th class="text-right py-2"> </th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows) if rows else '<tr><td class="py-4 text-slate-400" colspan="6">Henüz ürün yok.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>
"""
    return page_shell("SKU Listesi", body)


# -----------------------
# PLAN LIST + DELETE FROM PLAN (SKU delete)
# -----------------------
@app.get("/plan", response_class=HTMLResponse)
def plan(request: Request):
    owner_id = require_login(request)
    if not owner_id:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_conn()
    cur = conn.cursor()
    skus = [r["sku"] for r in cur.execute(
        "SELECT sku FROM products WHERE owner_id=? ORDER BY updated_at DESC",
        (int(owner_id),)
    ).fetchall()]

    rows = []
    for sku in skus:
        prod, _, _, res = compute_for_sku(cur, int(owner_id), sku)
        order_qty = int(round(res["order_qty"]))
        if order_qty <= 0:
            continue

        rows.append(f"""
        <tr class="border-b border-slate-700/50">
          <td class="py-3 pr-3 font-semibold">{sku}</td>
          <td class="py-3 pr-3 text-slate-300">{prod["name"] or ""}</td>
          <td class="py-3 pr-3 text-right">{int(prod["fba_stock"])}</td>
          <td class="py-3 pr-3 text-right">{int(prod["inbound_stock"])}</td>
          <td class="py-3 pr-3 text-right">{res["rop"]:.2f}</td>
          <td class="py-3 pr-3 text-right"><span class="px-3 py-1 rounded-2xl border bg-rose-500/15 text-rose-200 border-rose-500/30">{order_qty}</span></td>
          <td class="py-3 text-right">
            <div class="flex justify-end gap-2">
              <a class="px-4 py-2 rounded-2xl text-sm border border-slate-700/60 bg-slate-900/40 hover:bg-slate-900/60" href="/product/{sku}">Detay</a>
              <form method="post" action="/delete/product/{sku}" onsubmit="return confirm('Plan listesinden kaldırılır: SKU + tüm satış kayıtları silinir. Emin misin?');">
                <button type="submit" class="px-4 py-2 rounded-2xl text-sm border border-rose-500/30 bg-rose-500/15 text-rose-200 hover:bg-rose-500/25">Sil</button>
              </form>
            </div>
          </td>
        </tr>
        """)

    conn.close()

    body = f"""
<div class="rounded-3xl border border-slate-700/60 bg-slate-900/40 p-6 shadow-lg">
  <div class="flex items-end justify-between flex-wrap gap-3">
    <div>
      <h2 class="text-xl font-semibold">Sipariş Listesi</h2>
      <p class="text-slate-400 text-sm mt-1">Sadece kendi verilerinle hesaplanır.</p>
    </div>

    <div class="flex gap-2 flex-wrap">
      <a href="/export/plan.xlsx" class="rounded-2xl border border-slate-700/60 bg-slate-900/40 hover:bg-slate-900/60 px-4 py-2 text-sm font-semibold">Excel (Plan)</a>
    </div>
  </div>

  <div class="mt-5 overflow-x-auto">
    <table class="w-full text-sm">
      <thead class="text-slate-300">
        <tr class="border-b border-slate-700/60">
          <th class="text-left py-2 pr-3">SKU</th>
          <th class="text-left py-2 pr-3">Ürün</th>
          <th class="text-right py-2 pr-3">FBA</th>
          <th class="text-right py-2 pr-3">Yoldaki</th>
          <th class="text-right py-2 pr-3">ROP</th>
          <th class="text-right py-2 pr-3">Sipariş</th>
          <th class="text-right py-2"> </th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows) if rows else '<tr><td class="py-4 text-slate-400" colspan="7">Sipariş gereken ürün yok.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>
"""
    return page_shell("Sipariş Listesi", body)


# -----------------------
# PRODUCT DETAIL + SALES DELETE
# -----------------------
@app.get("/product/{sku}", response_class=HTMLResponse)
def product_detail(request: Request, sku: str):
    owner_id = require_login(request)
    if not owner_id:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_conn()
    cur = conn.cursor()
    computed = compute_for_sku(cur, int(owner_id), sku)
    if computed is None:
        conn.close()
        return page_shell("Ürün Bulunamadı", "<div class='rounded-3xl border border-slate-700/60 bg-slate-900/40 p-6'>SKU bulunamadı.</div>")

    prod, last3, monthly_found, res = computed

    last6 = list(reversed(last_n_calendar_months(6)))
    labels6 = [month_label(y, m) for y, m in last6]
    units6 = [(fetch_month_units(cur, int(owner_id), sku, y, m) or 0) for y, m in last6]
    conn.close()

    body = f"""
<div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
  <div class="lg:col-span-1 rounded-3xl border border-slate-700/60 bg-slate-900/40 p-6">
    <div class="text-slate-300 text-sm">SKU</div>
    <div class="text-2xl font-bold mt-1">{sku}</div>
    <div class="text-slate-400 text-sm mt-1">{prod["name"] or ""}</div>

    <div class="mt-4 text-sm text-slate-300 space-y-1">
      <div>Lead Time: <b>{int(prod["lead_time_days"])}</b> gün</div>
      <div>Z: <b>{float(prod["z_value"])}</b></div>
      <div>FBA: <b>{int(prod["fba_stock"])}</b></div>
      <div>Yoldaki: <b>{int(prod["inbound_stock"])}</b></div>
    </div>

    <div class="mt-5 rounded-2xl border border-slate-700/60 bg-slate-950/30 p-4">
      <div class="text-slate-300 text-sm">60 Gün Sipariş</div>
      <div class="text-4xl font-extrabold mt-1">{int(round(res["order_qty"]))}</div>
    </div>

    <form class="mt-5" method="post" action="/delete/product/{sku}" onsubmit="return confirm('SKU {sku} ve TÜM satış kayıtları silinsin mi?');">
      <button type="submit" class="w-full px-4 py-3 rounded-2xl text-sm border border-rose-500/30 bg-rose-500/15 text-rose-200 hover:bg-rose-500/25 font-semibold">
        SKU + Satışları Sil
      </button>
    </form>
  </div>

  <div class="lg:col-span-2 space-y-6">
    <div class="rounded-3xl border border-slate-700/60 bg-slate-900/40 p-6">
      <div class="text-slate-300 text-sm">Son 3 Ay Baz</div>
      <div class="text-slate-200 text-sm mt-1">Aylar: <b>{", ".join(month_label(y,m) for y,m in last3)}</b> | Bulunan: <b>{monthly_found if monthly_found else "yok"}</b></div>

      <div class="mt-5 grid grid-cols-1 md:grid-cols-4 gap-4">
        <div class="rounded-2xl border border-slate-700/60 bg-slate-950/30 p-4"><div class="text-slate-300 text-xs">Günlük Hız</div><div class="text-2xl font-bold mt-1">{res["daily_velocity"]:.2f}</div></div>
        <div class="rounded-2xl border border-slate-700/60 bg-slate-950/30 p-4"><div class="text-slate-300 text-xs">Std</div><div class="text-2xl font-bold mt-1">{res["std_daily"]:.2f}</div></div>
        <div class="rounded-2xl border border-slate-700/60 bg-slate-950/30 p-4"><div class="text-slate-300 text-xs">SS</div><div class="text-2xl font-bold mt-1">{res["safety_stock"]:.2f}</div></div>
        <div class="rounded-2xl border border-slate-700/60 bg-slate-950/30 p-4"><div class="text-slate-300 text-xs">ROP</div><div class="text-2xl font-bold mt-1">{res["rop"]:.2f}</div></div>
      </div>
    </div>

    <div class="rounded-3xl border border-slate-700/60 bg-slate-900/40 p-6">
      <div class="text-slate-300 text-sm">Son 6 Ay Trend</div>
      <div class="mt-4"><canvas id="salesChart" height="120"></canvas></div>
    </div>

    <div class="rounded-3xl border border-slate-700/60 bg-slate-900/40 p-6">
      <h3 class="text-lg font-semibold">Tek Ay Satış Sil</h3>
      <p class="text-slate-400 text-sm mt-1">Seçtiğin (yıl, ay) kaydı silinir.</p>

      <form class="mt-4 grid grid-cols-1 md:grid-cols-4 gap-3" method="post" action="/delete/sale"
            onsubmit="return confirm('Seçili ayın satış kaydı silinsin mi?');">
        <input type="hidden" name="sku" value="{sku}">
        <input class="rounded-2xl bg-slate-900/60 border border-slate-700 px-4 py-3" name="year" type="number" placeholder="Yıl (örn 2026)" required>
        <input class="rounded-2xl bg-slate-900/60 border border-slate-700 px-4 py-3" name="month" type="number" min="1" max="12" placeholder="Ay (1-12)" required>
        <div class="md:col-span-2">
          <button class="w-full rounded-2xl bg-rose-500/15 text-rose-200 border border-rose-500/30 hover:bg-rose-500/25 px-5 py-3 font-semibold" type="submit">
            Bu Ayı Sil
          </button>
        </div>
      </form>
    </div>
  </div>
</div>

<script>
  const labels = {json.dumps(labels6)};
  const data = {json.dumps(units6)};
  const ctx = document.getElementById('salesChart').getContext('2d');
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels,
      datasets: [{{ label: 'Aylık Satış', data, tension: 0.3 }}]
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ labels: {{ color: '#cbd5e1' }} }} }},
      scales: {{
        x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: 'rgba(148,163,184,0.15)' }} }},
        y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: 'rgba(148,163,184,0.15)' }} }}
      }}
    }}
  }});
</script>
"""
    return page_shell(f"Ürün Detay — {sku}", body)


# -----------------------
# EXPORTS (OWNER-AWARE)
# -----------------------
@app.get("/export/products.xlsx")
def export_products_xlsx(request: Request):
    owner_id = require_login(request)
    if not owner_id:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_conn()
    cur = conn.cursor()
    skus = [r["sku"] for r in cur.execute(
        "SELECT sku FROM products WHERE owner_id=? ORDER BY updated_at DESC",
        (int(owner_id),)
    ).fetchall()]

    wb = Workbook()
    ws = wb.active
    ws.title = "SKUListesi"
    ws.append(["SKU", "Ürün", "LeadTime", "Z", "FBA", "Yoldaki", "GünlükHız", "Std", "SS", "ROP", "Sipariş(60g)"])

    for sku in skus:
        prod, _, _, res = compute_for_sku(cur, int(owner_id), sku)
        ws.append([
            sku, prod["name"] or "",
            int(prod["lead_time_days"]), float(prod["z_value"]),
            int(prod["fba_stock"]), int(prod["inbound_stock"]),
            round(res["daily_velocity"], 4),
            round(res["std_daily"], 4),
            round(res["safety_stock"], 4),
            round(res["rop"], 4),
            int(round(res["order_qty"]))
        ])

    conn.close()
    autosize_columns(ws)
    bio = workbook_to_stream(wb)

    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="sku_listesi.xlsx"'}
    )


@app.get("/export/plan.xlsx")
def export_plan_xlsx(request: Request):
    owner_id = require_login(request)
    if not owner_id:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_conn()
    cur = conn.cursor()
    skus = [r["sku"] for r in cur.execute(
        "SELECT sku FROM products WHERE owner_id=? ORDER BY updated_at DESC",
        (int(owner_id),)
    ).fetchall()]

    wb = Workbook()
    ws = wb.active
    ws.title = "SiparisListesi"
    ws.append(["SKU", "Ürün", "LeadTime", "Z", "FBA", "Yoldaki", "GünlükHız", "ROP", "Sipariş(60g)"])

    for sku in skus:
        prod, _, _, res = compute_for_sku(cur, int(owner_id), sku)
        order_qty = int(round(res["order_qty"]))
        if order_qty <= 0:
            continue
        ws.append([
            sku, prod["name"] or "",
            int(prod["lead_time_days"]), float(prod["z_value"]),
            int(prod["fba_stock"]), int(prod["inbound_stock"]),
            round(res["daily_velocity"], 4),
            round(res["rop"], 4),
            order_qty
        ])

    conn.close()
    autosize_columns(ws)
    bio = workbook_to_stream(wb)

    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="siparis_listesi.xlsx"'}
    )
