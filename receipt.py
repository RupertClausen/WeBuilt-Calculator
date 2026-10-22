"""Customer-facing quotations and receipts, in English or Thai.

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

All customer-facing wording lives in locales/<lang>.yaml, not here, so the
Thai can be corrected by a Thai speaker without touching code. A missing
key falls back to English rather than failing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from jinja2 import Environment, select_autoescape

_env = Environment(autoescape=select_autoescape(["html"]), trim_blocks=True,
                   lstrip_blocks=True)

LOCALE_DIR = Path(__file__).parent / "locales"
FALLBACK = "en"

# Zones with no daylight saving, so a fixed offset is exactly right. Used
# only if the time zone database is missing (normally the tzdata package
# provides it), so a broken install shows the right time instead of crashing.
FIXED_OFFSETS = {"Asia/Bangkok": 7, "UTC": 0}


def zone(name: str):
    """The time zone called `name`, never raising."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        hours = FIXED_OFFSETS.get(name)
        if hours is None:
            print(f"[webuilt] unknown time zone {name!r}; using UTC")
            return timezone.utc
        return timezone(timedelta(hours=hours), name)


THAI_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
               "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


# ---------------------------------------------------------------------------
# locales, hot-reloaded like the config files
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, dict]] = {}


def _read(lang: str) -> dict:
    path = LOCALE_DIR / f"{lang}.yaml"
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    hit = _cache.get(lang)
    if hit and hit[0] == mtime:
        return hit[1]
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _cache[lang] = (mtime, data)
    return data


def available_languages() -> list[str]:
    return sorted(p.stem for p in LOCALE_DIR.glob("*.yaml"))


class Locale:
    """Strings for one language, with English underneath for missing keys."""

    def __init__(self, lang: str):
        self.lang = lang
        self.strings = {**_read(FALLBACK), **_read(lang)}

    def __call__(self, key: str, **kw) -> str:
        text = self.strings.get(key, key)
        return text.format(**kw) if kw else text

    def __getitem__(self, key: str):
        return self.strings.get(key, key)


def resolve_language(requested: str | None, rc: dict) -> str:
    allowed = rc.get("languages") or available_languages()
    if requested in allowed:
        return requested
    default = rc.get("default_language", FALLBACK)
    return default if default in allowed else FALLBACK


def pick(value, lang: str):
    """Config text may be one value for every language, or {en:…, th:…}."""
    if isinstance(value, dict):
        return value.get(lang) or value.get(FALLBACK) or next(iter(value.values()), "")
    return value


def label_for(spec: dict | None, lang: str, fallback: str) -> str:
    """`label` is English; `label_<lang>` overrides it in that language."""
    if not spec:
        return fallback
    return spec.get(f"label_{lang}") or spec.get("label") or fallback


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def baht(x: float) -> str:
    sign = "-" if x < -0.004 else ""
    return f"{sign}฿{abs(x):,.2f}"


def duration(hours: float, t: Locale) -> str:
    h, m = divmod(round(hours * 60), 60)
    if h and m:
        return t("dur_h_m", h=h, m=m)
    return t("dur_h", h=h) if h else t("dur_m", m=m)


def pct(x: float) -> str:
    return f"{x:.0f}%" if abs(x - round(x)) < 1e-9 else f"{x:.1f}%"


def fmt_date(dt: datetime, t: Locale) -> str:
    """Thai documents use the Buddhist Era year: 2026 CE is 2569 BE."""
    if t["date_format"] == "thai":
        return (f"{dt.day} {THAI_MONTHS[dt.month - 1]} {dt.year + 543} "
                f"{dt:%H:%M} น.")
    return dt.strftime(t["date_format"])


# ---------------------------------------------------------------------------
# view model
# ---------------------------------------------------------------------------

def build_view(
    row: dict,
    quote: dict,
    rc: dict,
    lang: str | None = None,
    *,
    pricing_cfg: dict | None = None,
    printers_cfg: dict | None = None,
    thumbnails: dict[int, str] | None = None,
) -> dict:
    """Turn a stored quote into what the customer is allowed to see.

    `thumbnails` maps plate number to a picture URL; plates without one get
    a placeholder. The picture is the customer's own part, so it is safe to
    show.

    Labels for printers, rush tiers and delivery are looked up again from the
    current config by key, so an old quote can still be rendered in a
    language that did not exist when it was made. If the key is gone from the
    config, the label saved with the quote is used as it was.
    """
    lang = resolve_language(lang, rc)
    t = Locale(lang)
    pricing_cfg = pricing_cfg or {}
    printer_specs = (printers_cfg or {}).get("printers", {})

    bd = quote["breakdown"]
    rates = bd.get("rates", {})
    order = bd["order"]
    plates = bd.get("plates", [])
    amount = {l["key"]: l["amount"] for l in quote["lines"]}
    tz = zone(rc.get("timezone", "Asia/Bangkok"))
    created = datetime.fromisoformat(row["created_at"]).astimezone(tz)
    paid = (row.get("payment_status") or "unpaid") == "paid"
    free_reprints = bool(rc.get("free_reprints", False))

    def printer_name(pl: dict) -> str:
        return label_for(printer_specs.get(pl["printer"]), lang,
                         pl.get("printer_label", pl["printer"]))

    rush_name = label_for(
        (pricing_cfg.get("rush_tiers") or {}).get(rates.get("rush_key")),
        lang, rates.get("rush_label", ""))
    delivery_name = label_for(
        (pricing_cfg.get("delivery_options") or {}).get(rates.get("delivery_key")),
        lang, rates.get("delivery_label", ""))

    lines: list[dict] = []

    def add(key, label, detail="", sub=None):
        if key in amount and abs(amount[key]) > 0.004:
            lines.append({"key": key, "label": label, "detail": detail,
                          "amount": amount[key], "sub": sub or []})

    n = order["plates"]
    extra = order.get("additional_plates_charged", max(n - 1, 0))
    labour = baht(rates.get("labour_per_hour", 0))

    add("setup", t("setup_label"), t("setup_detail"))
    add("extra_plates", t("extra_label", n=extra),
        t("extra_detail", fee=baht(rates.get("additional_plate_fee", 0)), plates=n))
    prep_min = bd.get("client", {}).get("prep_minutes", 0)
    add("prep", t("prep_label"), t("prep_detail", min=f"{prep_min:.0f}", rate=labour))

    # filament, itemised per material at the CHARGED price only
    by_type: dict[str, dict] = {}
    for m in bd.get("materials", []):
        v = by_type.setdefault(m["type"], {"grams": 0.0, "reported": 0.0,
                                           "price_per_kg": m["price_per_kg"]})
        v["grams"] += m["grams_billed"]
        v["reported"] += m["grams_reported"]
    mat_sub = [
        t("material_sub", type=typ, grams=f"{v['grams']:.1f}",
          price=baht(v["price_per_kg"]))
        + (t("material_allowance", reported=f"{v['reported']:.1f}")
           if v["grams"] - v["reported"] > 0.05 else "")
        for typ, v in sorted(by_type.items(), key=lambda kv: -kv[1]["grams"])
    ]
    add("material", t("material_label"),
        t("material_detail", grams=f"{order['grams_total']:.1f}"), mat_sub)

    def per_plate(rate_field):
        return [
            t("per_plate_sub", label=pl["label"], time=duration(pl["hours"], t),
              copies=t("copies_suffix", n=pl["copies"]) if pl["copies"] > 1 else "",
              printer=printer_name(pl),
              rate=baht(pl.get("rates", {}).get(rate_field, 0)))
            for pl in plates
        ]

    add("machine", t("machine_label"),
        t("machine_detail", time=duration(order["hours_total"], t)),
        per_plate("machine_per_hour"))
    kwh = sum(pl["hours"] * pl["copies"] * pl.get("rates", {}).get("power_w", 0)
              for pl in plates) / 1000.0
    add("electricity", t("electricity_label"),
        t("electricity_detail", kwh=f"{kwh:.2f}",
          rate=baht(rates.get("electricity_per_kwh", 0))))
    add("maintenance", t("wear_label"), t("wear_detail"),
        per_plate("maintenance_per_hour"))

    pp_billed = rates.get("postproc_minutes_billed",
                          rates.get("postproc_minutes_entered", 0))
    add("postproc", t("postproc_label"),
        t("postproc_detail", min=f"{pp_billed:.0f}", rate=labour))

    add("risk", t("risk_label"),
        t("risk_detail", free=t("risk_detail_free") if free_reprints
          else t("risk_detail_plain")),
        [t("risk_sub", label=pl["label"],
           pct=f"{pl['risk']['p_success_first_try'] * 100:.0f}")
         for pl in plates])

    add("headroom", t("headroom_label"),
        t("headroom_detail", pct=pct(rates.get("headroom_pct", 0))))
    add("rush", t("rush_label", label=rush_name),
        t("rush_detail", mult=f"{rates.get('rush_multiplier', 1):g}"))
    add("client_price", t("adjust_label"))
    add("profit", t("profit_label"),
        t("profit_detail", pct=pct(rates.get("profit_margin_pct", 0))))

    # A capped discount shows the baht actually given, not the percentage
    # asked for: "60% off" next to a smaller amount wouldn't add up, and the
    # cap itself (break-even) is the shop's business.
    dsc = bd.get("discount") or {}
    if dsc:
        name = (dsc.get("label_th") if lang == "th" and dsc.get("label_th")
                else dsc.get("label"))
        show_pct = dsc.get("kind") == "percent" and not dsc.get("capped")
        add("discount",
            t("discount_label_named", name=name) if name else t("discount_label"),
            t("discount_detail_percent", pct=pct(dsc["value"])) if show_pct
            else t("discount_detail_amount", amount=baht(dsc.get("applied", 0))))
    add("delivery", t("delivery_label"), delivery_name)
    if rates.get("minimum_applied"):
        add("rounding", t("minimum_label"),
            t("minimum_detail", min=baht(rates.get("minimum_price", 0))))
    else:
        add("rounding", t("rounding_label"),
            t("rounding_detail",
              step=baht(rates.get("rounding_step", 1)).replace(".00", "")))

    plate_rows = [{
        "label": pl["label"],
        "printer": printer_name(pl),
        "materials": ", ".join(pl.get("materials", [])),
        "grams": pl["grams"],
        "time": duration(pl["hours"], t),
        "copies": pl["copies"],
        "success": pl["risk"]["p_success_first_try"] * 100,
        "subtotal": pl["subtotal"],
        "thumbnail": (thumbnails or {}).get(pl["plate"]),
    } for pl in plates]

    biz = rc.get("business", {})
    contact = biz.get("contact", {}) or {}
    method_raw = row.get("payment_method") or ""
    tax_id = pick(biz.get("tax_id", ""), lang)

    return {
        "lang": lang,
        "other_langs": [l for l in (rc.get("languages") or available_languages())
                        if l != lang],
        "t": {k: v for k, v in t.strings.items() if isinstance(v, str)},
        "title": t("title_receipt") if paid else t("title_quotation"),
        "paid": paid,
        "receipt_no": row.get("receipt_no") or f"Q{row['id']}",
        "date": fmt_date(created, t),
        "paid_at": (fmt_date(datetime.fromisoformat(row["paid_at"]).astimezone(tz), t)
                    if row.get("paid_at") else ""),
        "payment_method": (t["payment_methods"].get(method_raw, method_raw)
                           if isinstance(t["payment_methods"], dict) else method_raw),
        "customer_name": row.get("customer_name") or "",
        "customer_contact": row.get("customer_contact") or "",
        "notes": row.get("notes") or "",
        "business": {
            "name": pick(biz.get("name", ""), lang),
            "tagline": pick(biz.get("tagline", ""), lang),
            "address": pick(biz.get("address", ""), lang),
            "contact": [f"{t('contact_' + k)}: {v}" for k, v in contact.items() if v],
            "tax_id": t("tax_id", id=tax_id) if tax_id else "",
        },
        "order": {
            "summary": t("job_summary", plates=n,
                         plate_word=t("plate_singular") if n == 1 else t("plate_plural"),
                         grams=f"{order['grams_total']:.1f}",
                         time=duration(order["hours_total"], t)),
            "rush": rush_name,
            "delivery": delivery_name,
        },
        "lines": lines,
        "plates": plate_rows,
        "total": quote["total"],
        "terms": pick(rc.get("terms", []), lang) or [],
        "footer": pick(rc.get("footer", ""), lang),
        "show_rates": rc.get("show_rates", True),
        "show_plates": rc.get("show_per_plate_table", True) and len(plate_rows) > 0,
        "show_method": rc.get("show_method_section", True),
        "method": method_text(
            rates, t,
            flat_rate=all(m.get("basis", "").startswith("flat")
                          for m in bd.get("materials", [])),
            measured=all(pl.get("printer_measured", False) for pl in plates),
            free_reprints=free_reprints,
        ),
    }


def method_text(rates: dict, t: Locale, *, flat_rate: bool, measured: bool,
                free_reprints: bool) -> list[str]:
    """Plain-language method. Each claim is conditional on it being true for
    THIS quote: the document must not overstate how the numbers were made."""
    return [
        t("method_setup", setup=baht(rates.get("setup_fee", 0)),
          extra=baht(rates.get("additional_plate_fee", 0))),
        t("method_filament", rate=t("method_filament_flat") if flat_rate
          else t("method_filament_mixed")),
        t("method_machine"),
        t("method_risk",
          source=t("method_risk_measured") if measured else t("method_risk_typical"),
          free=t("method_risk_free") if free_reprints else ""),
        t("method_headroom", pct=pct(rates.get("headroom_pct", 0))),
    ] + ([t("method_profit", pct=pct(rates["profit_margin_pct"]))]
         if rates.get("profit_margin_pct", 0) > 0 else [])


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------

def render_text(v: dict) -> str:
    """Plain text, for pasting into LINE or email."""
    t = v["t"]
    out = [v["business"]["name"],
           f"{v['title'].upper()}  {v['receipt_no']}",
           v["date"]]
    if v["customer_name"]:
        out.append(t["txt_customer"].format(name=v["customer_name"]))
    out += ["", v["order"]["summary"], "-" * 32]
    for l in v["lines"]:
        out.append(l["label"])
        out.append(f"  {baht(l['amount']):>28}")
        if v["show_rates"] and l["detail"] and l["key"] not in ("risk", "setup"):
            out.append(f"  {l['detail']}")
    out.append("-" * 32)
    out.append(f"{t['txt_total']}  {baht(v['total'])}")
    if v["paid"]:
        out.append(t["txt_paid"].format(at=v["paid_at"])
                   + (t["txt_paid_via"].format(method=v["payment_method"])
                      if v["payment_method"] else ""))
    else:
        out.append(t["txt_not_paid"])
    if len(v["plates"]) > 1:
        out += ["", t["txt_plates"]]
        for p in v["plates"]:
            out.append(t["txt_plate_row"].format(
                label=p["label"], grams=f"{p['grams']:.1f}", time=p["time"],
                copies=f" ×{p['copies']}" if p["copies"] > 1 else "",
                pct=f"{p['success']:.0f}"))
    if v["terms"]:
        out += ["", t["txt_terms"]]
        out += [f"- {term}" for term in v["terms"]]
    if v["footer"]:
        out += ["", v["footer"]]
    return "\n".join(out)


HTML = _env.from_string(r"""<!doctype html>
<html lang="{{ v.lang }}">
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
         "Noto Sans Thai", "Sarabun", "Leelawadee UI", Tahoma, sans-serif; }
  html[lang="th"] body { line-height:1.65; }
  .bar { max-width:820px; margin:16px auto 0; padding:0 16px; display:flex;
         gap:8px; flex-wrap:wrap; align-items:center; }
  .bar button, .bar a, .bar select { font:inherit; font-size:13px; padding:7px 12px;
         border-radius:8px; border:1px solid var(--line); background:#fff;
         color:var(--ink); cursor:pointer; text-decoration:none; }
  .bar .primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  .bar .lang { margin-left:auto; font-weight:600; }
  .paper { max-width:820px; margin:12px auto 40px; background:#fff;
           padding:40px 44px; border-radius:6px;
           box-shadow:0 1px 3px rgba(0,0,0,.08); }
  header { display:flex; justify-content:space-between; gap:24px;
           flex-wrap:wrap; border-bottom:2px solid var(--ink); padding-bottom:18px; }
  .biz h1 { margin:0; font-size:20px; letter-spacing:-.01em; }
  .biz div { color:var(--muted); font-size:12px; }
  .doc { text-align:right; }
  .doc .t { font-size:22px; font-weight:700; letter-spacing:.06em; }
  html[lang="en"] .doc .t { text-transform:uppercase; }
  .doc .no { font-family:ui-monospace, Menlo, monospace; font-size:14px; }
  .doc div { color:var(--muted); font-size:12px; }
  .stamp { display:inline-block; margin-top:6px; padding:2px 10px;
           border-radius:999px; font-size:11px; font-weight:700; letter-spacing:.06em; }
  html[lang="en"] .stamp { text-transform:uppercase; }
  .stamp.paid { color:var(--ok); border:1.5px solid var(--ok); }
  .stamp.unpaid { color:var(--warn); border:1.5px solid var(--warn); }
  .meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
          gap:12px 24px; margin:18px 0 6px; }
  .meta .k { font-size:11px; color:var(--muted); letter-spacing:.04em; }
  html[lang="en"] .meta .k, html[lang="en"] h2, html[lang="en"] th { text-transform:uppercase; }
  .meta .v { font-size:14px; }
  h2 { font-size:12px; letter-spacing:.06em; color:var(--muted);
       margin:26px 0 6px; font-weight:600; }
  table { width:100%; border-collapse:collapse; }
  td, th { padding:8px 0; border-bottom:1px solid var(--line); vertical-align:top;
           text-align:left; }
  th { font-size:11px; color:var(--muted); font-weight:600; letter-spacing:.04em; }
  .amt, th.amt { text-align:right; white-space:nowrap;
                 font-variant-numeric:tabular-nums; padding-left:16px; }
  .lbl { font-weight:600; }
  .det { color:var(--muted); font-size:12.5px; margin-top:2px; }
  .sub { color:var(--muted); font-size:12px; margin:3px 0 0 14px; padding:0; }
  .sub li { margin:1px 0; }
  .total td { border-bottom:0; border-top:2px solid var(--ink);
              font-size:18px; font-weight:700; padding-top:12px; }
  .plate-grid { display:grid; grid-template-columns:repeat(3, 1fr); gap:12px; }
  .plate { border:1px solid var(--line); border-radius:8px; overflow:hidden;
           break-inside:avoid; page-break-inside:avoid; }
  .plate img, .plate .noimg { display:block; width:100%; aspect-ratio:1;
           object-fit:contain; background:var(--soft); }
  .plate .noimg { display:flex; align-items:center; justify-content:center;
           color:var(--muted); font-size:12px; }
  .plate .pb { padding:8px 10px 10px; border-top:1px solid var(--line); }
  .plate .pn { font-weight:600; font-size:13px; overflow-wrap:anywhere; }
  .plate .ps { color:var(--muted); font-size:11.5px; }
  .plate .pa { font-weight:700; font-size:13px; margin-top:4px;
           font-variant-numeric:tabular-nums; }
  .method { color:#30353c; font-size:13px; padding-left:18px; margin:4px 0; }
  .method li { margin:4px 0; }
  .terms { font-size:12px; color:var(--muted); padding-left:18px; margin:4px 0; }
  .notes { background:var(--soft); border-radius:6px; padding:10px 12px;
           font-size:13px; margin-top:12px; }
  footer { margin-top:28px; padding-top:14px; border-top:1px solid var(--line);
           color:var(--muted); font-size:12px; text-align:center; }
  #copied { font-size:12px; color:var(--ok); }
  @media (max-width:600px) { .paper { padding:24px 18px; } .doc { text-align:left; }
                             .plate-grid { grid-template-columns:repeat(2, 1fr); }
                             .bar .lang { margin-left:0; } }
  @media print {
    html, body { background:#fff; }
    .bar { display:none; }
    .paper { box-shadow:none; margin:0; max-width:none; padding:0; }
    @page { size:A4; margin:16mm 14mm; }
    tr, li { break-inside:avoid; }
    .plate img { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  }
</style>
</head>
<body>
<div class="bar">
  <button class="primary" onclick="window.print()">{{ v.t.bar_print }}</button>
  <button onclick="copyText()">{{ v.t.bar_copy }}</button>
  <a href="/receipt/{{ id }}.txt?lang={{ v.lang }}" target="_blank">{{ v.t.bar_plain }}</a>
  <a href="/">{{ v.t.bar_back }}</a>
  {% if v.paid %}
  <button onclick="setPaid('unpaid')">{{ v.t.bar_mark_unpaid }}</button>
  {% else %}
  <select id="method">{% for m in methods %}<option value="{{ m }}">{{ method_names.get(m, m) }}</option>{% endfor %}</select>
  <button onclick="setPaid('paid')">{{ v.t.bar_mark_paid }}</button>
  {% endif %}
  <span id="copied"></span>
  {% for l in v.other_langs %}
  <a class="lang" href="?lang={{ l }}" lang="{{ l }}">{{ switch_labels.get(l, l) }}</a>
  {% endfor %}
</div>

<main class="paper">
  <header>
    <div class="biz">
      <h1>{{ v.business.name }}</h1>
      {% if v.business.tagline %}<div>{{ v.business.tagline }}</div>{% endif %}
      {% if v.business.address %}<div>{{ v.business.address }}</div>{% endif %}
      {% for c in v.business.contact %}<div>{{ c }}</div>{% endfor %}
      {% if v.business.tax_id %}<div>{{ v.business.tax_id }}</div>{% endif %}
    </div>
    <div class="doc">
      <div class="t">{{ v.title }}</div>
      <div class="no">{{ v.receipt_no }}</div>
      <div>{{ v.date }}</div>
      {% if v.paid %}
        <span class="stamp paid">{{ v.t.stamp_paid }}{% if v.payment_method %} · {{ v.payment_method }}{% endif %}</span>
        {% if v.paid_at %}<div>{{ v.paid_at }}</div>{% endif %}
      {% else %}
        <span class="stamp unpaid">{{ v.t.stamp_unpaid }}</span>
      {% endif %}
    </div>
  </header>

  <section class="meta">
    {% if v.customer_name %}<div><div class="k">{{ v.t.meta_customer }}</div><div class="v">{{ v.customer_name }}</div></div>{% endif %}
    {% if v.customer_contact %}<div><div class="k">{{ v.t.meta_contact }}</div><div class="v">{{ v.customer_contact }}</div></div>{% endif %}
    <div><div class="k">{{ v.t.meta_job }}</div><div class="v">{{ v.order.summary }}</div></div>
    <div><div class="k">{{ v.t.meta_turnaround }}</div><div class="v">{{ v.order.rush }} · {{ v.order.delivery }}</div></div>
  </section>

  {% if v.notes %}<div class="notes">{{ v.notes }}</div>{% endif %}

  <h2>{{ v.t.section_charges }}</h2>
  <table>
    <tr><th>{{ v.t.col_item }}</th><th class="amt">{{ v.t.col_amount }}</th></tr>
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
    <tr class="total"><td>{{ v.t.total }}</td><td class="amt">{{ baht(v.total) }}</td></tr>
  </table>

  {% if v.show_plates %}
  <h2>{{ v.t.section_plates }}</h2>
  <div class="plate-grid">
    {% for p in v.plates %}
    <div class="plate">
      {% if p.thumbnail %}
      <img src="{{ p.thumbnail }}" alt="{{ p.label }}">
      {% else %}
      <div class="noimg">{{ v.t.no_picture }}</div>
      {% endif %}
      <div class="pb">
        <div class="pn" title="{{ p.label }}">{{ p.label }}</div>
        <div class="ps">{{ p.printer }} · {{ p.materials }}</div>
        <div class="ps">{{ '%.1f'|format(p.grams) }} {{ v.t.unit_g }} · {{ p.time }}{% if p.copies > 1 %} · {{ v.t.col_runs }} {{ p.copies }}{% endif %}</div>
        <div class="ps">{{ v.t.col_success }} {{ '%.0f'|format(p.success) }}%</div>
        <div class="pa">{{ baht(p.subtotal) }}</div>
      </div>
    </div>
    {% endfor %}
  </div>
  <div class="det" style="margin-top:8px">{{ v.t.plates_note }}</div>
  {% endif %}

  {% if v.show_method %}
  <h2>{{ v.t.section_method }}</h2>
  <ul class="method">{% for m in v.method %}<li>{{ m }}</li>{% endfor %}</ul>
  {% endif %}

  {% if v.terms %}
  <h2>{{ v.t.section_terms }}</h2>
  <ol class="terms">{% for term in v.terms %}<li>{{ term }}</li>{% endfor %}</ol>
  {% endif %}

  {% if v.footer %}<footer>{{ v.footer }}</footer>{% endif %}
</main>

<script>
const LANG = {{ v.lang|tojson }};
async function setPaid(status) {
  const fd = new FormData();
  fd.append('status', status);
  const m = document.getElementById('method');
  if (m) fd.append('method', m.value);
  const r = await fetch('/api/orders/{{ id }}/payment', { method: 'POST', body: fd });
  if (r.ok) location.reload(); else alert((await r.json()).detail);
}
async function copyText() {
  const url = '/receipt/{{ id }}.txt?lang=' + LANG;
  const t = await (await fetch(url)).text();
  try { await navigator.clipboard.writeText(t);
        document.getElementById('copied').textContent = {{ v.t.bar_copied|tojson }}; }
  catch (e) { window.open(url, '_blank'); }
}
</script>
</body>
</html>
""")


def render_html(v: dict, quote_id: int, methods: list[str] | None = None) -> str:
    # Each switch link is written in the language it switches TO, so a
    # customer who cannot read the current page can still find their own.
    switch_labels = {l: Locale(l)["language_name"] for l in v["other_langs"]}
    method_names = Locale(v["lang"])["payment_methods"]
    return HTML.render(v=v, id=quote_id, baht=baht, methods=methods or [],
                       switch_labels=switch_labels,
                       method_names=method_names if isinstance(method_names, dict) else {})
