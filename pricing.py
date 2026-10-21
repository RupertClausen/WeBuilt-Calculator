"""The WeBuilt cost model.

An order is a LIST of plates — a customer with more parts than fit on one
bed sends several sliced files, and one .gcode.3mf can itself hold several
plates.

Structure of a quote:

    for each plate:
        cost   = material + machine + electricity + maintenance
        risked = cost * risk_factor(that plate's own duration)
    order  = setup_fee
           + (total_plates - 1) * additional_plate_fee
           + sum(risked)
           + file_prep + post_processing
    order  = order * headroom * rush * client_price
    total  = round_up(max(order + delivery_fee, minimum_price))

The risk factor is computed PER PLATE, never on the aggregate. Survival is
exponential in duration, so it does not aggregate linearly: a 2 h plate and
a 20 h plate are not two 11 h plates. Summing grams and hours across files
first and pricing the lump would materially misprice every mixed order —
under-pricing the long plate's risk and over-pricing the short one's.

Three things this deliberately does NOT do, carried over from the
spreadsheet review:

  * No exponent on hours. Machine time is linear; what grows with duration
    is the chance of failure, and that is modelled explicitly below.
  * Maintenance is per machine-hour, not a percentage of filament cost.
    Nozzle and belt wear do not care what a spool cost.
  * No per-faculty time exponent. Client differences are split into flat
    file prep, duration-coupled risk, and post-processing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field

from slicefile import SlicedJob


@dataclass
class QuoteItem:
    """One sliced file in an order.

    `copies` is how many times to run this plate. `printer_id` lets a big
    part go on the large machine while the small parts stay on the normal
    one, within a single quote.
    """
    job: SlicedJob
    printer_id: str | None = None
    copies: int = 1
    label: str = ""


@dataclass
class LineItem:
    key: str
    label: str
    amount: float
    note: str = ""


@dataclass
class Quote:
    total: float
    currency: str
    lines: list[LineItem]
    breakdown: dict
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": round(self.total, 2),
            "currency": self.currency,
            "lines": [asdict(l) for l in self.lines],
            "breakdown": self.breakdown,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# filament resolution
# ---------------------------------------------------------------------------

@dataclass
class ResolvedMaterial:
    type: str
    cost_per_kg: float        # what we pay   (margin tracking)
    price_per_kg: float       # what we charge
    risk_multiplier: float
    waste_factor: float
    basis: str                # how price_per_kg was decided


def resolve_material(mtype: str, filaments_cfg: dict, brand: str | None = None) -> ResolvedMaterial:
    mtype = (mtype or "UNKNOWN").upper()
    materials = filaments_cfg.get("materials", {})
    spec = materials.get(mtype)

    if spec is None:
        fb = filaments_cfg.get("unknown_material", {})
        cost = float(fb.get("cost_per_kg", 700))
        return ResolvedMaterial(
            type=mtype,
            cost_per_kg=cost,
            price_per_kg=cost,
            risk_multiplier=float(fb.get("risk_multiplier", 1.5)),
            waste_factor=float(fb.get("waste_factor", 0.05)),
            basis=f"unknown material '{mtype}' — fallback rate",
        )

    brands = spec.get("brands", []) or []
    restocked = [b for b in brands if b.get("restocked")] or brands

    # What it costs us: the dearest spool we routinely restock. Conservative,
    # because we cannot control which spool the job actually consumes.
    cost_per_kg = max((float(b["cost_per_kg"]) for b in restocked), default=700.0)

    mode = filaments_cfg.get("pricing_mode", "flat_standard")
    standard_types = [t.upper() for t in filaments_cfg.get("standard_types", [])]
    is_standard = spec.get("standard", True) and mtype in standard_types

    if mode == "flat_standard" and is_standard:
        price = float(filaments_cfg.get("flat_standard_price_per_kg", cost_per_kg))
        basis = "flat standard rate"
    elif mode == "per_brand" and brand:
        match = next((b for b in brands if b["name"].lower() == brand.lower()), None)
        if match:
            price = float(match["cost_per_kg"]) * float(filaments_cfg.get("brand_markup", 1.25))
            basis = f"{match['name']} cost x markup"
        else:
            price = cost_per_kg
            basis = f"brand '{brand}' not in roster — max restocked"
    else:
        price = cost_per_kg
        basis = "max restocked brand"
        if not is_standard:
            basis = "specialty material — max restocked brand"

    return ResolvedMaterial(
        type=mtype,
        cost_per_kg=cost_per_kg,
        price_per_kg=price,
        risk_multiplier=float(spec.get("risk_multiplier", 1.0)),
        waste_factor=float(spec.get("waste_factor", 0.0)),
        basis=basis,
    )


# ---------------------------------------------------------------------------
# failure model
# ---------------------------------------------------------------------------

def survival(
    hours: float,
    p_start: float,
    hazard: float,
    material_risk: float,
    shape: float = 1.0,
) -> float:
    """Probability a print of this length completes on the first attempt.

        p = p_start * exp( -(hazard * material_risk * hours) ** shape )

    Every parameter here belongs to ONE machine. Two printers in the same
    room legitimately have different curves, not just different constants.

      p_start  first-layer / adhesion failure. Does not scale with print
               length, so it sits outside the exponential.

      hazard   per-hour chance of a late failure, measured on that machine.

      shape    the Weibull exponent, which is what actually makes the model
               differ between machines rather than just the numbers:
                 1.0  constant hazard — failures arrive at a steady rate.
                      The memoryless case. Good default.
                 >1   hazard RISES with time. Machines that heat-soak badly,
                      drift out of tune, or warp large ABS parts late in a
                      print. A 20 h job is disproportionately risky on these.
                 <1   hazard FALLS with time. Machines where trouble shows up
                      early and a print that survived hour two will almost
                      certainly finish. Runout sensors and good first-layer
                      detection push a machine this way.

      material_risk  per-printer scaling (see printers.yaml `material_risk`).
               An enclosed machine may run ABS at 1.1 while an open-frame one
               needs 2.5 for exactly the same filament.

    The exponent is dimensionless — hazard is per-hour, so hazard*hours
    cancels — which is why, unlike the old `hours ** 1.5`, this does not
    change its answer if someone feeds it minutes by mistake.
    """
    lam = max(hazard, 0.0) * max(material_risk, 0.0)
    x = lam * max(hours, 0.0)
    if x <= 0:
        return min(max(p_start, 1e-6), 1.0)
    p = p_start * math.exp(-(x ** max(shape, 0.01)))
    return min(max(p, 1e-6), 1.0)


def printer_material_risk(printer: dict, mtype: str, filaments_cfg: dict) -> tuple[float, str]:
    """Risk multiplier for this material ON THIS MACHINE.

    Resolution order:
      1. printers.yaml -> printers.<id>.material_risk.<TYPE>   (machine-specific)
      2. filaments.yaml -> materials.<TYPE>.risk_multiplier    (fleet default)
      3. 1.0
    """
    mtype = (mtype or "UNKNOWN").upper()
    overrides = {k.upper(): v for k, v in (printer.get("material_risk") or {}).items()}
    if mtype in overrides:
        return float(overrides[mtype]), f"{printer.get('label', 'printer')} override"
    spec = (filaments_cfg.get("materials", {}) or {}).get(mtype)
    if spec and "risk_multiplier" in spec:
        return float(spec["risk_multiplier"]), "fleet default"
    fb = filaments_cfg.get("unknown_material", {})
    return float(fb.get("risk_multiplier", 1.0)), "unknown-material fallback"


def risk_factor(
    p_success: float,
    consumed_fraction: float,
    cap: float | None = None,
) -> float:
    """Expected cost multiplier from reprints.

    Expected attempts to get one good part is 1/p. Each failed attempt burns
    `consumed_fraction` of a full print's material and machine time before
    you catch it.

        factor = 1 + (1/p - 1) * consumed_fraction

    consumed_fraction = 1.0 reduces this to the fully conservative 1/p.

    `cap` bounds the result. Without it, the exponential survival model makes
    very long prints arbitrarily expensive — correct arithmetic, but past a
    point you renegotiate the job rather than price in three reprints.
    """
    expected_failures = (1.0 / p_success) - 1.0
    factor = 1.0 + expected_failures * max(consumed_fraction, 0.0)
    if cap is not None and factor > cap:
        return cap
    return factor


# ---------------------------------------------------------------------------
# the quote
# ---------------------------------------------------------------------------

def round_up(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.ceil(value / step) * step


def build_quote(
    items,
    *,
    pricing: dict,
    printers: dict,
    filaments: dict,
    printer_id: str | None = None,
    plates: int | None = None,
    rush: str | None = None,
    delivery: str | None = None,
    postproc_minutes: float = 0.0,
    client_profile: str | None = None,
    prep_minutes: float | None = None,
) -> Quote:
    """Price an order of one or more plates.

    `items` may be a single SlicedJob, a list of SlicedJob, or a list of
    QuoteItem. Each entry is ONE PLATE and is risked on its own duration.
    """

    # --- normalise the input ------------------------------------------------
    if isinstance(items, SlicedJob):
        items = [QuoteItem(job=items, copies=plates or 1)]
    else:
        items = [QuoteItem(job=i) if isinstance(i, SlicedJob) else i
                 for i in items]
        if plates is not None and len(items) == 1:
            items[0].copies = plates
    if not items:
        raise ValueError("No plates to quote.")

    warnings: list[str] = []
    for it in items:
        for w in it.job.warnings:
            if w not in warnings:
                warnings.append(w)

    # --- client profile -----------------------------------------------------
    profiles = pricing.get("client_profiles", {})
    prof_key = client_profile or pricing.get("default_client_profile", "general")
    profile = profiles.get(prof_key)
    if profile is None:
        if profiles:
            warnings.append(f"Unknown client profile '{prof_key}', using defaults.")
        profile = {"label": prof_key, "prep_minutes": 0, "risk_multiplier": 1.0,
                   "postproc_multiplier": 1.0, "price_multiplier": 1.0}
    prof_risk = float(profile.get("risk_multiplier", 1.0))
    prof_postproc = float(profile.get("postproc_multiplier", 1.0))
    prof_price = float(profile.get("price_multiplier", 1.0))

    # Profit: a share of the price, not a markup on cost. A profile may set
    # its own (internal jobs carry none).
    profit_margin = float(profile.get("profit_margin",
                                      pricing.get("profit_margin", 0.0)) or 0.0)
    if not 0.0 <= profit_margin < 0.9:
        raise ValueError(
            f"profit_margin must be at least 0 and below 0.9, got {profit_margin}")
    prep_min = prep_minutes if prep_minutes is not None else float(
        profile.get("prep_minutes", 0.0))

    roster = printers.get("printers", {})
    fallback = printers.get("fallback_printer") or next(iter(roster))
    cap = pricing.get("max_risk_factor")
    cap = float(cap) if cap else None

    # --- price each plate on its own curve ---------------------------------
    agg = {"material": 0.0, "machine": 0.0, "electricity": 0.0,
           "maintenance": 0.0, "risked": 0.0, "unrisked": 0.0,
           "material_cost": 0.0}
    total_plates = 0
    total_grams = 0.0
    total_hours = 0.0
    plate_detail = []
    material_detail = []

    for idx, it in enumerate(items, start=1):
        job = it.job
        pid = it.printer_id or printer_id or fallback
        if pid not in roster:
            warnings.append(f"Unknown printer '{pid}', using '{fallback}'.")
            pid = fallback
        printer = roster[pid]
        if not printer.get("measured", False):
            w = (f"Printer '{pid}' has unmeasured failure rates — the risk "
                 "portion of this quote is a guess.")
            if w not in warnings:
                warnings.append(w)

        copies = max(int(it.copies), 1) * max(int(job.plates), 1)
        hours = job.hours

        # material for this plate
        mat_price = mat_cost = 0.0
        worst_risk, worst_basis = 1.0, "none"
        for f in job.filaments:
            res = resolve_material(f.type, filaments)
            billed_g = f.grams * (1.0 + res.waste_factor)
            mat_price += billed_g * res.price_per_kg / 1000.0
            mat_cost += billed_g * res.cost_per_kg / 1000.0
            m_risk, m_basis = printer_material_risk(printer, f.type, filaments)
            if m_risk > worst_risk:
                worst_risk, worst_basis = m_risk, f"{res.type} ({m_basis})"
            material_detail.append({
                "plate": idx,
                "type": res.type,
                "grams_reported": round(f.grams * copies, 2),
                "grams_billed": round(billed_g * copies, 2),
                "price_per_kg": res.price_per_kg,
                "cost_per_kg": res.cost_per_kg,
                "charged": round(billed_g * res.price_per_kg / 1000.0 * copies, 2),
                "basis": res.basis,
                "risk_multiplier_on_this_printer": m_risk,
                "risk_basis": m_basis,
            })

        machine = hours * float(printer["rate_per_hour"])
        electricity = hours * (float(printer["power_w"]) / 1000.0) * float(
            pricing["electricity_tariff_per_kwh"])
        maintenance = hours * float(printer["maintenance_per_hour"])
        plate_cost = mat_price + machine + electricity + maintenance

        # THIS plate's own survival curve — never the aggregate's
        effective_risk = worst_risk * prof_risk
        p = survival(hours, float(printer["p_start"]), float(printer["hazard"]),
                     effective_risk, float(printer.get("shape", 1.0)))
        consumed = float(printer.get("failure_consumed_fraction",
                                     pricing["failure_consumed_fraction"]))
        rf_uncapped = risk_factor(p, consumed)
        rf = risk_factor(p, consumed, cap=cap)
        if rf < rf_uncapped - 1e-9:
            warnings.append(
                f"Plate {idx}: failure allowance capped at {rf:.2f}x "
                f"(uncapped {rf_uncapped:.2f}x). A print this long is worth "
                "discussing with the customer rather than pricing in reprints."
            )

        agg["material"] += mat_price * copies
        agg["material_cost"] += mat_cost * copies
        agg["machine"] += machine * copies
        agg["electricity"] += electricity * copies
        agg["maintenance"] += maintenance * copies
        agg["unrisked"] += plate_cost * copies
        agg["risked"] += plate_cost * rf * copies
        total_plates += copies
        total_grams += job.total_grams * copies
        total_hours += hours * copies

        plate_detail.append({
            "plate": idx,
            "label": it.label or f"plate {idx}",
            "printer": pid,
            "printer_label": printer.get("label", pid),
            "printer_measured": bool(printer.get("measured", False)),
            "copies": copies,
            "hours": round(hours, 3),
            "grams": round(job.total_grams, 2),
            "materials": [f.type for f in job.filaments],
            # Per-plate amounts, for one run. Customer-facing documents use
            # these; none of them is a supplier cost.
            "amounts_each": {
                "material": round(mat_price, 2),
                "machine": round(machine, 2),
                "electricity": round(electricity, 2),
                "maintenance": round(maintenance, 2),
            },
            "rates": {
                "machine_per_hour": float(printer["rate_per_hour"]),
                "power_w": float(printer["power_w"]),
                "maintenance_per_hour": float(printer["maintenance_per_hour"]),
            },
            "cost_each": round(plate_cost, 2),
            "risk": {
                "p_success_first_try": round(p, 4),
                "factor": round(rf, 4),
                "factor_uncapped": round(rf_uncapped, 4),
                "material_risk_multiplier": worst_risk,
                "material_risk_from": worst_basis,
                "client_risk_multiplier": prof_risk,
                "combined_risk_multiplier": round(effective_risk, 4),
                "shape": float(printer.get("shape", 1.0)),
                "model": "weibull" if float(printer.get("shape", 1.0)) != 1.0
                         else "exponential",
            },
            "subtotal": round(plate_cost * rf * copies, 2),
            "source": job.source,
        })

    # --- order level --------------------------------------------------------
    setup = float(pricing["setup_fee"])
    extra_plate_fee = float(
        pricing.get("additional_plate_fee", pricing.get("per_plate_fee", 0)))
    extra_plates = max(total_plates - 1, 0)
    plates_charge = extra_plates * extra_plate_fee

    labour_rate = float(pricing["labour_rate_per_hour"])
    prep = (prep_min / 60.0) * labour_rate
    billed_postproc_min = (postproc_minutes or 0.0) * prof_postproc
    postproc = (billed_postproc_min / 60.0) * labour_rate

    subtotal = setup + plates_charge + agg["risked"] + prep + postproc

    headroom = float(pricing["headroom"])
    rush_key = rush or pricing.get("default_rush", "standard")
    rush_spec = pricing["rush_tiers"].get(rush_key) or pricing["rush_tiers"]["standard"]
    rush_mult = float(rush_spec["multiplier"])

    before_profit = subtotal * headroom * rush_mult * prof_price
    # Margin on price: profit is `profit_margin` of the price before delivery,
    # so divide rather than multiply. 15% -> x1.176, of which 15% is profit.
    after_multipliers = before_profit / (1.0 - profit_margin)
    profit = after_multipliers - before_profit

    del_key = delivery or pricing.get("default_delivery", "pickup")
    del_spec = pricing["delivery_options"].get(del_key) or {"fee": 0, "label": del_key}
    delivery_fee = float(del_spec["fee"])

    raw_total = after_multipliers + delivery_fee
    bypass_min = bool(profile.get("bypass_minimum", prof_price == 0.0))
    floored = raw_total if bypass_min else max(
        raw_total, float(pricing["minimum_price"]))
    if floored > raw_total:
        warnings.append(
            f"Raised to the {pricing['currency']}{pricing['minimum_price']:.0f} minimum."
        )
    total = round_up(floored, float(pricing["rounding_step"]))

    # --- presentation -------------------------------------------------------
    plate_word = "plate" if total_plates == 1 else "plates"
    # Roll the per-plate rows up by material for the summary line.
    by_type: dict[str, float] = {}
    for d in material_detail:
        by_type[d["type"]] = by_type.get(d["type"], 0.0) + d["grams_billed"]
    mats = [f"{g:.2f}g {t}" for t, g in
            sorted(by_type.items(), key=lambda kv: -kv[1])]

    lines = [
        LineItem("setup", "Setup (once per order)", setup,
                 "includes handling the first plate"),
    ]
    if plates_charge:
        lines.append(LineItem(
            "extra_plates", f"Additional plates ({extra_plates})", plates_charge,
            f"{total_plates} {plate_word} total, {extra_plate_fee:.0f} each after the first"))
    lines += [
        LineItem("prep", "File preparation", prep,
                 f"{prep_min:.0f} min @ {labour_rate:.0f}/h — {profile.get('label', prof_key)}"),
        LineItem("material", "Filament", agg["material"], ", ".join(mats)),
        LineItem("machine", "Machine time", agg["machine"],
                 f"{total_hours:.2f} h across {total_plates} {plate_word}"),
        LineItem("electricity", "Electricity", agg["electricity"]),
        LineItem("maintenance", "Maintenance", agg["maintenance"]),
    ]
    if postproc:
        note = f"{postproc_minutes:.0f} min"
        if prof_postproc != 1.0:
            note += (f" x{prof_postproc} ({profile.get('label', prof_key)})"
                     f" = {billed_postproc_min:.0f} min")
        lines.append(LineItem("postproc", "Post-processing", postproc, note))

    risk_amount = agg["risked"] - agg["unrisked"]
    if len(plate_detail) == 1:
        d = plate_detail[0]["risk"]
        risk_note = (f"{(1-d['p_success_first_try'])*100:.1f}% chance of a "
                     f"reprint ({d['factor']:.3f}x)")
        if prof_risk != 1.0:
            risk_note += f", incl. {profile.get('label', prof_key)} x{prof_risk}"
    else:
        risk_note = "per plate: " + ", ".join(
            f"{d['label']} {d['risk']['factor']:.3f}x" for d in plate_detail)
    lines.append(LineItem("risk", "Failure allowance", risk_amount, risk_note))

    lines.append(LineItem("headroom", "Headroom", subtotal * (headroom - 1.0),
                          f"{(headroom-1)*100:.0f}%"))
    if rush_mult != 1.0:
        lines.append(LineItem("rush", rush_spec.get("label", rush_key),
                              subtotal * headroom * (rush_mult - 1.0)))
    if prof_price != 1.0:
        lines.append(LineItem(
            "client_price", f"{profile.get('label', prof_key)} rate",
            (subtotal * headroom * rush_mult) * (prof_price - 1.0),
            f"x{prof_price}"))
    if profit > 0.004:
        lines.append(LineItem(
            "profit", "Profit margin", profit,
            f"{profit_margin*100:g}% of the price before delivery"))
    if delivery_fee:
        lines.append(LineItem("delivery", del_spec.get("label", del_key), delivery_fee))

    rounding_delta = total - raw_total
    if abs(rounding_delta) > 1e-9:
        lines.append(LineItem("rounding", "Rounding / minimum", rounding_delta))

    true_cost = agg["material_cost"] + agg["electricity"] + agg["maintenance"]
    margin = total - true_cost

    # Where the money goes. Every charge on the quote lands in exactly one
    # bucket, so the buckets add up to the total. Kept internal: the receipt
    # never shows supplier cost or this split.
    amt = {l.key: l.amount for l in lines}
    labour = {
        "setup": setup,
        "extra_plates": plates_charge,
        "file_prep": prep,
        "post_processing": postproc,
    }
    running = {
        "filament_at_supplier_cost": agg["material_cost"],
        "electricity": agg["electricity"],
        "wear_parts": agg["maintenance"],
    }
    economics = {
        "running_costs": {k: round(v, 2) for k, v in running.items()}
                         | {"total": round(sum(running.values()), 2)},
        # Negative means the filament line sells below what the dearest
        # restocked spool costs you.
        "filament_markup": round(agg["material"] - agg["material_cost"], 2),
        "machine_time": round(agg["machine"], 2),
        "labour": {k: round(v, 2) for k, v in labour.items()}
                  | {"total": round(sum(labour.values()), 2)},
        "failure_allowance": round(amt.get("risk", 0.0), 2),
        "contingency": round(amt.get("headroom", 0.0), 2),
        "rush_premium": round(amt.get("rush", 0.0), 2),
        "rate_adjustment": round(amt.get("client_price", 0.0), 2),
        "profit": round(profit, 2),
        "profit_margin": profit_margin,
        "delivery": round(delivery_fee, 2),
        "rounding": round(rounding_delta, 2),
        "total": round(total, 2),
    }

    breakdown = {
        "order": {
            "plates": total_plates,
            "files": len(items),
            "grams_total": round(total_grams, 2),
            "hours_total": round(total_hours, 3),
            "setup_fee": setup,
            "additional_plate_fee": extra_plate_fee,
            "additional_plates_charged": extra_plates,
        },
        "plates": plate_detail,
        "materials": material_detail,
        "client": {
            "profile": prof_key,
            "label": profile.get("label", prof_key),
            "prep_minutes": prep_min,
            "risk_multiplier": prof_risk,
            "postproc_multiplier": prof_postproc,
            "price_multiplier": prof_price,
        },
        "economics": economics,
        "margin": {
            "estimated_direct_cost": round(true_cost, 2),
            "estimated_margin": round(margin, 2),
            "margin_pct": round(100 * margin / total, 1) if total else 0.0,
            "note": "Gross: the total minus running costs. This is NOT profit; "
                    "it also contains your labour, the contingency and the "
                    "failure allowance. See `economics` for the split.",
        },
        "rates": {
            "setup_fee": setup,
            "additional_plate_fee": extra_plate_fee,
            "labour_per_hour": labour_rate,
            "electricity_per_kwh": float(pricing["electricity_tariff_per_kwh"]),
            "headroom_pct": round((headroom - 1.0) * 100, 2),
            "profit_margin_pct": round(profit_margin * 100, 2),
            "rush_multiplier": rush_mult,
            "rush_key": rush_key,
            "rush_label": rush_spec.get("label", rush_key),
            "delivery_key": del_key,
            "delivery_label": del_spec.get("label", del_key),
            "delivery_fee": delivery_fee,
            "minimum_price": float(pricing["minimum_price"]),
            "rounding_step": float(pricing["rounding_step"]),
            "minimum_applied": floored > raw_total,
            "postproc_minutes_entered": float(postproc_minutes or 0.0),
            "postproc_minutes_billed": round(billed_postproc_min, 2),
            "consumed_fraction_default": float(pricing["failure_consumed_fraction"]),
            "max_risk_factor": cap,
        },
        "subtotal_before_multipliers": round(subtotal, 2),
        "raw_total": round(raw_total, 2),
    }

    return Quote(
        total=total,
        currency=pricing.get("currency", "THB"),
        lines=lines,
        breakdown=breakdown,
        warnings=warnings,
    )
