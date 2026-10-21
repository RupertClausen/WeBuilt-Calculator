"""Customer-facing quotations and receipts.

The document is built from a WHITELISTED view model, never by passing the
stored quote through. The stored quote contains supplier costs
(`cost_per_kg`), an estimated direct cost and a margin — those are the shop's
numbers, and printing them would tell the customer nothing about their job
while handing over the shop's negotiating position. Anything not explicitly
copied into the view model below cannot reach the page.

The client profile name is not printed either. The customer sees its effect —
minutes of file preparation and each plate's estimated success rate — which
is the part they can check and act on.

Every customer line is taken from the engine's own line amounts, so the
document always adds up to the quoted total to the satang. A test asserts it.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, select_autoescape

_env = Environment(autoescape=select_autoescape(["html"]), trim_blocks=True,
                   lstrip_blocks=True)


def baht(x: float) -> str:
    sign = "-" if x < -0.004 else ""
    return f"{sign}฿{abs(x):,.2f}"


def duration(hours: float) -> str:
    total_min = round(hours * 60)
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def pct(x: float) -> str:
    return f"{x:.0f}%" if abs(x - round(x)) < 1e-9 else f"{x:.1f}%"


# ---------------------------------------------------------------------------
# view model
# ---------------------------------------------------------------------------

def build_view(row: dict, quote: dict, rc: dict) -> dict:
    """Turn a stored quote into what the customer is allowed to see."""
    bd = quote["breakdown"]
    rates = bd.get("rates", {})
    order = bd["order"]
    plates = bd.get("plates", [])
    amount = {l["key"]: l["amount"] for l in quote["lines"]}
    tz = ZoneInfo(rc.get("timezone", "Asia/Bangkok"))
    created = datetime.fromisoformat(row["created_at"]).astimezone(tz)
    paid = (row.get("payment_status") or "unpaid") == "paid"

    lines: list[dict] = []

    def add(key, label, detail="", sub=None):
        if key in amount and abs(amount[key]) > 0.004:
            lines.append({"key": key, "label": label, "detail": detail,
                          "amount": amount[key], "sub": sub or []})

    n = order["plates"]
    extra = order.get("additional_plates_charged", max(n - 1, 0))

    add("setup", "Order setup",
        "Receiving and checking your files, slicing, quoting, packing, "
        "and handling the first plate")
    add("extra_plates", f"Additional plates × {extra}",
        f"{baht(rates.get('additional_plate_fee', 0))} for each plate after the "
        f"first ({n} plates in this order)")
    prep_min = bd.get("client", {}).get("prep_minutes", 0)
    add("prep", "File preparation",
        f"Estimated {prep_min:.0f} min × {baht(rates.get('labour_per_hour', 0))}/h — "
        "checking and repairing the model, scaling, orientation, supports")

    # filament, itemised per material at the CHARGED price only
    by_type: dict[str, dict] = {}
    for m in bd.get("materials", []):
        t = by_type.setdefault(m["type"], {"grams": 0.0, "amount": 0.0,
                                           "price_per_kg": m["price_per_kg"],
                                           "reported": 0.0})
        t["grams"] += m["grams_billed"]
        t["reported"] += m["grams_reported"]
        t["amount"] += m["charged"]
    mat_sub = [
        f"{t}: {v['grams']:.1f} g × {baht(v['price_per_kg'])}/kg"
        + (f" (slicer {v['reported']:.1f} g + purge/brim allowance)"
           if v["grams"] - v["reported"] > 0.05 else "")
        for t, v in sorted(by_type.items(), key=lambda kv: -kv[1]["grams"])
    ]
    add("material", "Filament", f"{order['grams_total']:.1f} g from the slicer",
        mat_sub)

    def per_printer(field, unit_fmt):
        out = []
        for pl in plates:
            r = pl.get("rates", {})
            out.append(f"{pl['label']}: {duration(pl['hours'])}"
                       + (f" × {pl['copies']}" if pl["copies"] > 1 else "")
                       + f" on {pl.get('printer_label', pl['printer'])}"
                       + f" @ {unit_fmt(r)}")
        return out

    add("machine", "Machine time", f"{duration(order['hours_total'])} in total",
        per_printer("machine", lambda r: f"{baht(r.get('machine_per_hour', 0))}/h"))
    kwh = sum(pl["hours"] * pl["copies"] * pl.get("rates", {}).get("power_w", 0)
              for pl in plates) / 1000.0
    add("electricity", "Electricity",
        f"{kwh:.2f} kWh × {baht(rates.get('electricity_per_kwh', 0))}/kWh")
    add("maintenance", "Printer wear",
        "Nozzles, belts, build plates and other parts, charged per machine hour",
        per_printer("maintenance",
                    lambda r: f"{baht(r.get('maintenance_per_hour', 0))}/h"))

    pp_in = rates.get("postproc_minutes_entered", 0)
    pp_billed = rates.get("postproc_minutes_billed", pp_in)
    add("postproc", "Post-processing",
        f"Estimated {pp_billed:.0f} min × {baht(rates.get('labour_per_hour', 0))}/h — "
        "support removal and finishing, allowing for how delicate the parts are")

    risk_sub = [
        f"{pl['label']}: estimated {pl['risk']['p_success_first_try']*100:.0f}% "
        f"chance of printing right first time"
        for pl in plates
    ]
    add("risk", "Failure allowance",
        "The expected cost of reprints. Long prints and some materials are "
        "more likely to fail partway"
        + ("; this is what pays for free reprints (see terms)."
           if rc.get("free_reprints") else "."), risk_sub)

    add("headroom", "Contingency",
        f"{pct(rates.get('headroom_pct', 0))} of the subtotal, for estimate "
        "error in print time and filament use")
    add("rush", f"Rush: {rates.get('rush_label', '')}",
        f"× {rates.get('rush_multiplier', 1):g} on the subtotal")
    add("client_price", "Rate adjustment", "")
    add("delivery", "Delivery", rates.get("delivery_label", ""))
    if "rounding" in amount and abs(amount["rounding"]) > 0.004:
        if rates.get("minimum_applied"):
            add("rounding", "Minimum order",
                f"Orders start at {baht(rates.get('minimum_price', 0))}")
        else:
            add("rounding", "Rounding",
                f"Rounded up to the nearest "
                f"{baht(rates.get('rounding_step', 1)).replace('.00', '')}")

    plate_rows = [{
        "label": pl["label"],
        "printer": pl.get("printer_label", pl["printer"]),
        "materials": ", ".join(pl.get("materials", [])),
        "grams": pl["grams"],
        "time": duration(pl["hours"]),
        "copies": pl["copies"],
        "success": pl["risk"]["p_success_first_try"] * 100,
        "subtotal": pl["subtotal"],
    } for pl in plates]

    biz = rc.get("business", {})
    contact = biz.get("contact", {}) or {}
    return {
        "title": "Receipt" if paid else "Quotation",
        "paid": paid,
        "receipt_no": row.get("receipt_no") or f"Q{row['id']}",
        "date": created.strftime("%d %b %Y, %H:%M"),
        "paid_at": (datetime.fromisoformat(row["paid_at"]).astimezone(tz)
                    .strftime("%d %b %Y, %H:%M") if row.get("paid_at") else ""),
        "payment_method": row.get("payment_method") or "",
        "customer_name": row.get("customer_name") or "",
        "customer_contact": row.get("customer_contact") or "",
        "notes": row.get("notes") or "",
        "business": {
            "name": biz.get("name", ""),
            "tagline": biz.get("tagline", ""),
            "address": biz.get("address", ""),
            "contact": [f"{k.upper() if k == 'line' else k.title()}: {v}"
                        for k, v in contact.items() if v],
            "tax_id": biz.get("tax_id", ""),
        },
        "order": {
            "plates": n,
            "files": order["files"],
            "grams": order["grams_total"],
            "time": duration(order["hours_total"]),
            "rush": rates.get("rush_label", ""),
            "delivery": rates.get("delivery_label", ""),
        },
        "lines": lines,
        "plates": plate_rows,
        "total": quote["total"],
        "terms": rc.get("terms", []),
        "footer": rc.get("footer", ""),
        "show_rates": rc.get("show_rates", True),
        "show_plates": rc.get("show_per_plate_table", True) and len(plate_rows) > 0,
        "show_method": rc.get("show_method_section", True),
        "method": method_text(
            rates,
            flat_rate=all(m.get("basis", "").startswith("flat")
                          for m in bd.get("materials", [])),
            measured=all(pl.get("printer_measured", False) for pl in plates),
            free_reprints=bool(rc.get("free_reprints", False)),
        ),
    }


def method_text(rates: dict, *, flat_rate: bool, measured: bool,
                free_reprints: bool) -> list[str]:
    """Plain-language method. Each claim is conditional on it being true for
    THIS quote: the document must not overstate how the numbers were made."""
    filament = ("Filament is charged by weight from the slicer, plus a small "
                "allowance for purge and brim. ")
    filament += ("One rate applies to all standard materials (PLA, PETG, ABS)."
                 if flat_rate else
                 "Standard materials share one rate; specialty materials are "
                 "charged at their own rate.")

    risk = ("Prints sometimes fail partway, and longer prints are more likely "
            "to. We estimate each plate's chance of printing right first time ")
    risk += ("from our own printers' failure records"
             if measured else
             "from typical failure rates for this kind of printer (we are still "
             "collecting records for our own machines)")
    risk += ", and include the expected cost of reprinting. That is the failure allowance."
    if free_reprints:
        risk += " Because it is included, a reprint caused by a print failure costs you nothing."

    return [
        f"Every order starts at {baht(rates.get('setup_fee', 0))}, which includes "
        f"the first plate. Each further plate adds "
        f"{baht(rates.get('additional_plate_fee', 0))}.",
        filament,
        "Machine time, electricity and printer wear are charged per hour of "
        "printing. They grow in a straight line with print time: a print twice "
        "as long costs twice as much machine time, never more.",
        risk,
        f"A {pct(rates.get('headroom_pct', 0))} contingency covers the difference "
        "between the slicer's estimate and the real print.",
    ]


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------

def render_text(v: dict) -> str:
    """Plain text, for pasting into LINE or email."""
    out = [v["business"]["name"],
           f"{v['title'].upper()}  {v['receipt_no']}",
           v["date"]]
    if v["customer_name"]:
        out.append(f"Customer: {v['customer_name']}")
    out.append("")
    o = v["order"]
    out.append(f"{o['plates']} plate{'s' if o['plates'] != 1 else ''} · "
               f"{o['grams']:.1f} g · {o['time']}")
    out.append("-" * 32)
    for l in v["lines"]:
        out.append(f"{l['label']}")
        out.append(f"  {baht(l['amount']):>28}")
        if v["show_rates"] and l["detail"] and l["key"] not in ("risk", "setup"):
            out.append(f"  {l['detail']}")
    out.append("-" * 32)
    out.append(f"TOTAL {baht(v['total']):>26}")
    if v["paid"]:
        out.append(f"PAID {v['paid_at']}"
                   + (f" via {v['payment_method']}" if v["payment_method"] else ""))
    else:
        out.append("Not yet paid")
    if v["plates"] and len(v["plates"]) > 1:
        out.append("")
        out.append("Plates:")
        for p in v["plates"]:
            out.append(f"- {p['label']}: {p['grams']:.1f} g, {p['time']}"
                       + (f" ×{p['copies']}" if p["copies"] > 1 else "")
                       + f", {p['success']:.0f}% first-time success")
    if v["terms"]:
        out.append("")
        out.append("Terms:")
        out += [f"- {t}" for t in v["terms"]]
    if v["footer"]:
        out += ["", v["footer"]]
    return "\n".join(out)


HTML = _env.from_string(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ v.title }} {{ v.receipt_no }}</title>
<style>
  :root { --ink:#16181c; --muted:#5f6670; --line:#dfe2e6; --soft:#f5f6f8;
          --accent:#1d4ed8; --ok:#15803d; --warn:#b45309; }
  * { box-sizing: border-box; }
  html { background:#e9ebee; }
  body { margin:0; color:var(--ink); background:#e9ebee;
         font:14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI",
         "Noto Sans Thai", "Leelawadee UI", Tahoma, sans-serif; }
  .bar { max-width:820px; margin:16px auto 0; padding:0 16px; display:flex;
         gap:8px; flex-wrap:wrap; }
  .bar button, .bar a { font:inherit; font-size:13px; padding:7px 12px;
         border-radius:8px; border:1px solid var(--line); background:#fff;
         color:var(--ink); cursor:pointer; text-decoration:none; }
  .bar select { font:inherit; font-size:13px; padding:6px 8px; border-radius:8px;
                border:1px solid var(--line); background:#fff; }
  .bar .primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  .paper { max-width:820px; margin:12px auto 40px; background:#fff;
           padding:40px 44px; border-radius:6px;
           box-shadow:0 1px 3px rgba(0,0,0,.08); }
  header { display:flex; justify-content:space-between; gap:24px;
           flex-wrap:wrap; border-bottom:2px solid var(--ink); padding-bottom:18px; }
  .biz h1 { margin:0; font-size:20px; letter-spacing:-.01em; }
  .biz div { color:var(--muted); font-size:12px; }
  .doc { text-align:right; }
  .doc .t { font-size:22px; font-weight:700; letter-spacing:.06em;
            text-transform:uppercase; }
  .doc .no { font-family:ui-monospace, Menlo, monospace; font-size:14px; }
  .doc div { color:var(--muted); font-size:12px; }
  .stamp { display:inline-block; margin-top:6px; padding:2px 10px;
           border-radius:999px; font-size:11px; font-weight:700;
           letter-spacing:.08em; text-transform:uppercase; }
  .stamp.paid { color:var(--ok); border:1.5px solid var(--ok); }
  .stamp.unpaid { color:var(--warn); border:1.5px solid var(--warn); }
  .meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
          gap:12px 24px; margin:18px 0 6px; }
  .meta .k { font-size:11px; color:var(--muted); text-transform:uppercase;
             letter-spacing:.06em; }
  .meta .v { font-size:14px; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em;
       color:var(--muted); margin:26px 0 6px; font-weight:600; }
  table { width:100%; border-collapse:collapse; }
  td, th { padding:8px 0; border-bottom:1px solid var(--line); vertical-align:top;
           text-align:left; }
  th { font-size:11px; color:var(--muted); font-weight:600;
       text-transform:uppercase; letter-spacing:.05em; }
  .amt, th.amt { text-align:right; white-space:nowrap;
                 font-variant-numeric:tabular-nums; padding-left:16px; }
  .lbl { font-weight:600; }
  .det { color:var(--muted); font-size:12.5px; margin-top:2px; }
  .sub { color:var(--muted); font-size:12px; margin:3px 0 0 14px; padding:0; }
  .sub li { margin:1px 0; }
  .total td { border-bottom:0; border-top:2px solid var(--ink);
              font-size:18px; font-weight:700; padding-top:12px; }
  .plates td, .plates th { font-size:12.5px; padding:6px 8px 6px 0; }
  .plates td.num, .plates th.num { text-align:right; }
  .method { color:#30353c; font-size:13px; padding-left:18px; margin:4px 0; }
  .method li { margin:4px 0; }
  .terms { font-size:12px; color:var(--muted); padding-left:18px; margin:4px 0; }
  .notes { background:var(--soft); border-radius:6px; padding:10px 12px;
           font-size:13px; margin-top:12px; }
  footer { margin-top:28px; padding-top:14px; border-top:1px solid var(--line);
           color:var(--muted); font-size:12px; text-align:center; }
  #copied { font-size:12px; color:var(--ok); align-self:center; }
  @media (max-width:600px) { .paper { padding:24px 18px; } .doc { text-align:left; } }
  @media print {
    html, body { background:#fff; }
    .bar { display:none; }
    .paper { box-shadow:none; margin:0; max-width:none; padding:0; }
    @page { size:A4; margin:16mm 14mm; }
    tr, li { break-inside:avoid; }
  }
</style>
</head>
<body>
<div class="bar">
  <button class="primary" onclick="window.print()">Print / Save as PDF</button>
  <button onclick="copyText()">Copy as text (LINE)</button>
  <a href="/receipt/{{ id }}.txt" target="_blank">Plain text</a>
  <a href="/">← Back to quoting</a>
  {% if v.paid %}
  <button onclick="setPaid('unpaid')">Mark as unpaid</button>
  {% else %}
  <select id="method">{% for m in methods %}<option>{{ m }}</option>{% endfor %}</select>
  <button onclick="setPaid('paid')">Mark as paid → Receipt</button>
  {% endif %}
  <span id="copied"></span>
</div>

<main class="paper">
  <header>
    <div class="biz">
      <h1>{{ v.business.name }}</h1>
      {% if v.business.tagline %}<div>{{ v.business.tagline }}</div>{% endif %}
      {% if v.business.address %}<div>{{ v.business.address }}</div>{% endif %}
      {% for c in v.business.contact %}<div>{{ c }}</div>{% endfor %}
      {% if v.business.tax_id %}<div>Tax ID {{ v.business.tax_id }}</div>{% endif %}
    </div>
    <div class="doc">
      <div class="t">{{ v.title }}</div>
      <div class="no">{{ v.receipt_no }}</div>
      <div>{{ v.date }}</div>
      {% if v.paid %}
        <span class="stamp paid">Paid{% if v.payment_method %} · {{ v.payment_method }}{% endif %}</span>
        {% if v.paid_at %}<div>{{ v.paid_at }}</div>{% endif %}
      {% else %}
        <span class="stamp unpaid">Awaiting payment</span>
      {% endif %}
    </div>
  </header>

  <section class="meta">
    {% if v.customer_name %}<div><div class="k">Customer</div><div class="v">{{ v.customer_name }}</div></div>{% endif %}
    {% if v.customer_contact %}<div><div class="k">Contact</div><div class="v">{{ v.customer_contact }}</div></div>{% endif %}
    <div><div class="k">Job</div><div class="v">{{ v.order.plates }} plate{{ 's' if v.order.plates != 1 }} · {{ '%.1f'|format(v.order.grams) }} g · {{ v.order.time }}</div></div>
    <div><div class="k">Turnaround / delivery</div><div class="v">{{ v.order.rush }} · {{ v.order.delivery }}</div></div>
  </section>

  {% if v.notes %}<div class="notes">{{ v.notes }}</div>{% endif %}

  <h2>Charges</h2>
  <table>
    <tr><th>Item</th><th class="amt">Amount</th></tr>
    {% for l in v.lines %}
    <tr>
      <td>
        <div class="lbl">{{ l.label }}</div>
        {% if v.show_rates and l.detail %}<div class="det">{{ l.detail }}</div>{% endif %}
        {% if v.show_rates and l.sub %}<ul class="sub">{% for s in l.sub %}<li>{{ s }}</li>{% endfor %}</ul>{% endif %}
      </td>
      <td class="amt">{{ baht(l.amount) }}</td>
    </tr>
    {% endfor %}
    <tr class="total"><td>Total</td><td class="amt">{{ baht(v.total) }}</td></tr>
  </table>

  {% if v.show_plates %}
  <h2>Plates</h2>
  <table class="plates">
    <tr><th>File</th><th>Printer</th><th>Material</th><th class="num">Weight</th>
        <th class="num">Time</th><th class="num">Runs</th>
        <th class="num">First-time success</th><th class="num">Subtotal</th></tr>
    {% for p in v.plates %}
    <tr><td>{{ p.label }}</td><td>{{ p.printer }}</td><td>{{ p.materials }}</td>
        <td class="num">{{ '%.1f'|format(p.grams) }} g</td><td class="num">{{ p.time }}</td>
        <td class="num">{{ p.copies }}</td><td class="num">{{ '%.0f'|format(p.success) }}%</td>
        <td class="num">{{ baht(p.subtotal) }}</td></tr>
    {% endfor %}
  </table>
  <div class="det" style="margin-top:6px">Plate subtotals include filament, machine time, electricity, wear and that plate's failure allowance, before order-level charges.</div>
  {% endif %}

  {% if v.show_method %}
  <h2>How this price is calculated</h2>
  <ul class="method">{% for m in v.method %}<li>{{ m }}</li>{% endfor %}</ul>
  {% endif %}

  {% if v.terms %}
  <h2>Terms</h2>
  <ol class="terms">{% for t in v.terms %}<li>{{ t }}</li>{% endfor %}</ol>
  {% endif %}

  {% if v.footer %}<footer>{{ v.footer }}</footer>{% endif %}
</main>

<script>
async function setPaid(status) {
  const fd = new FormData();
  fd.append('status', status);
  const m = document.getElementById('method');
  if (m) fd.append('method', m.value);
  const r = await fetch('/api/orders/{{ id }}/payment', { method: 'POST', body: fd });
  if (r.ok) location.reload(); else alert((await r.json()).detail);
}
async function copyText() {
  const r = await fetch('/receipt/{{ id }}.txt');
  const t = await r.text();
  try { await navigator.clipboard.writeText(t);
        document.getElementById('copied').textContent = 'Copied'; }
  catch { window.open('/receipt/{{ id }}.txt', '_blank'); }
}
</script>
</body>
</html>
""")


def render_html(v: dict, quote_id: int, methods: list[str] | None = None) -> str:
    return HTML.render(v=v, id=quote_id, baht=baht, methods=methods or [])
