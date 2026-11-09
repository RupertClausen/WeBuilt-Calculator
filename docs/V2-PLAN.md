# WeBuilt v2 — merge plan

v2 merges the customer self-service site (the `webuilt` codebase, originally
*printfarm*) into this repository, so the shop runs **one app with one price
model**. The screens it builds toward are in the v2 mock canvas
(<https://claude.ai/artifact/GeDN6uk4cFTFMY1S52pkRS>). The feature list comes
from *Website idea.pdf*.

Status: **M0 and M1 built** on branch `claude/quoting-system-features-73eae1`
(not yet released). M2 onward not started.

Built so far:
- M0: `webuilt/migrations.py` (numbered steps on `PRAGMA user_version`; a
  copy of a real pre-1.2 database upgraded with nothing lost), v2 statuses.
- M1: special additions (pricing, API, quote and order pages, EN/TH
  receipts), printer categories → printers → plate queue
  (`webuilt/queue.py`, Printers page, plates on the order page).
- Not done from M1 as planned: the console keeps the app's current look;
  the mock's restyle and the Quick quote rename are left for M3, when the
  pages get rebuilt anyway.

---

## 1. Decisions already made

| Decision | Source |
|---|---|
| This repo's calculator (`pricing.build_quote` + `config/*.yaml`) is the only price model. The customer site's `pricing.json`, its 1.4 / 1.5 / 1.6 demand markup and its own `estimate_item` are dropped. | Owner, 9 Nov 2026 |
| The two apps become **one app in this repo, released as v2**. | Owner, 9 Nov 2026 |
| **Spoolman stays stock (no fork).** WeBuilt stores its own filament info in Spoolman's *extra fields*. Filaments and rolls are added **only from the engineer console**, never on Spoolman's own site. | Owner requirement; fork rejected as needless upkeep |
| Normal / Large / Resin are **printer categories**. Each holds several printers and gets more over time. Quotes price by category; every printer has its own queue. | Owner, 9 Nov 2026 |
| Rolls aren't shared by every printer (printers are in different rooms). Each roll records which printers can use it. | Owner, 9 Nov 2026 |
| The customer price is a **suggestion**. An engineer confirms or changes it, and **PromptPay appears only after approval**. | PDF |

---

## 2. Shape of v2

```
                 public internet                         tailnet only
                        │                                     │
              ┌─────────▼─────────┐                 ┌─────────▼─────────┐
              │  customer door    │                 │  engineer door    │
              │  :8100            │                 │  :8000 (today's)  │
              │  /, /order/<tok>, │                 │  console, API,    │
              │  /guide, /track   │                 │  receipts, admin  │
              └─────────┬─────────┘                 └─────────┬─────────┘
                        └──────────── same code ──────────────┘
                                one SQLite (WAL) · data/ · config/
                                Spoolman · OrcaSlicer CLI · Google Sheet
```

- **One codebase, two front doors.** The customer door only mounts the
  customer routes, so a bug there can't expose the console. The engineer door
  stays on 127.0.0.1 behind Tailscale, exactly as today. The two run as two
  systemd services sharing the database.
- **The engineer door owns the background work**: slicing, the Spoolman
  write-back, the sheet mirror, backups and emails. The customer door only
  writes orders and files, and asks for a slice.
- **Desktop mode keeps working** for quick quoting on a laptop. The customer
  door, accounts and the sheet stay off there, as server mode does today.

### Repo layout

The flat modules stay where they are (`pricing.py`, `slicefile.py`,
`receipt.py`, …), so v1 history and tests keep working. New code goes in a
package:

```
webuilt/
  customer/      routes + Jinja templates for the public site
  console/       new engineer pages (orders, printers, stock, meetups…)
  slicing/       geometry.py (STL/3MF/OBJ), orca_cli.py, heuristic.py   ← ported
  spoolman.py    client: read stock, extra fields, write-back            ← rewritten
  promptpay.py   EMVCo payload + QR image
  orca_printer.py  OctoPrint-compatible upload endpoint
  queue.py       categories → printers → queues, assignment              ← ported
  meetups.py     windows, slots, booking rules                           ← ported
  notify.py      customer emails
migrations/      numbered schema steps (replaces ad-hoc ALTERs in core.py)
```

---

## 3. What comes from where

| Area | Comes from | What happens |
|---|---|---|
| Price model, profiles, margin, discounts | **this repo** | unchanged; it becomes the only one |
| Parsing sliced files, plate pictures | **this repo** | unchanged |
| Receipts EN/TH, numbering `WB-YYMM-NNN` | **this repo** | customer-site orders get WB numbers too |
| Accounts, sessions, roles, sheet mirror, backups, updater | **this repo** | unchanged |
| Mesh reading (STL/3MF/OBJ), size, volume | customer site `geometry.py` | ported as is (needs numpy) |
| OrcaSlicer CLI + heuristic estimate | customer site `slicer.py` | ported; output turned into a `SlicedJob` (§5) |
| Printer assignment by size/material | customer site `assignment.py` | ported; now chooses a **category**, then a printer (§6) |
| Delivery rules, campus meetups, slots | customer site `models.py`, `catalog.py` | ported as is (owner's rules) |
| PETG drying lead time | customer site `service.json` | moves to a Spoolman extra field per filament |
| Promotions (automatic / code) | customer site `promotions.py` | ported; discounts follow this repo's rules (never below break-even) |
| Keychain flow, NFC | customer site | ported; its fixed fees become *special additions* (§4) |
| Token order links, phone required | customer site | kept |
| Payment slips | customer site | kept; QR is now generated, not uploaded |
| `pricing.json`, demand markup, `estimate_item`, operator console, single shared login | customer site | **dropped** |

---

## 4. Where the two apps disagree, and the v2 rule

| Topic | Engineer tool (v1) | Customer site | **v2** |
|---|---|---|---|
| Price | build_quote | own model + 1.4–1.6 demand markup | build_quote only |
| Extra charges | none (needs code change) | NFC ฿8, chain fees, design fee hard-wired | **Special additions**: named lines added after margin, never discounted. Presets (repair, sanding, acetone, airbrush, NFC, chains, design) live in config; keychain options add theirs automatically. |
| Delivery | pickup / university ฿20 / express ฿60 | campus meetup (KMUTNB only) / parcel (postage set at review) / same-day courier (paid to rider) | Customer site's three, with their rules. Postage is a pass-through line the engineer enters. |
| Rush | +20% next day, +50% same day | rush = 1-day, courier only, ×1.6 | Engineer tiers price it. **Ask the owner** whether "rush ⇒ courier only" still applies. |
| Part type | client profile (engineering / architecture / general / internal) | general / architecture / keychain, ×1.3 for architecture | The customer picks a **category** (8 from the PDF). Each category maps to a client profile and a default print preset. The ×1.3 is dropped: the architecture profile already prices prep, risk and post-processing. |
| Statuses | quoted → queued → printing → done → delivered | pending_review → quoted → payment_review → queued → printing → ready → completed | See §6. |
| Order ID | WB-YYMM-NNN | PF-XXXX-XXXX | WB-YYMM-NNN, plus a private token for the customer's link |
| Logins | one per person, roles | one shared operator login | one per person (this repo's) |

---

## 5. How a customer upload gets priced

```
upload (STL/3MF/OBJ/STEP)
  → geometry: size, volume          (STEP: no mesh → "quote on review")
  → assignment: category that fits  (Normal / Large / Resin)
  → slice: Orca CLI with the category's profile + customer settings
           (falls back to the heuristic, labelled "not a real slice")
  → SlicedJob(filaments=[(material, grams)], seconds, plates=1)
  → build_quote(items, printer_id=category, client_profile=category's,
                rush, delivery, discount=promotion)
  → customer sees the customer-safe lines (receipt view model) + "not final"
```

- **Resin** goes to review without a number until resin is in the calculator
  (§9).
- The engineer re-prices from **their own slice**, sent from Orca's virtual
  printer or uploaded. The customer's estimate is kept for comparison, and the
  change is shown to the customer with the engineer's reason.

---

## 6. Data model changes

All in numbered migrations. A v1 `orders.db` upgrades in place, and a test
upgrades a copy of a real v1 database.

- **Statuses** (`config/server.yaml`): `review`, `rejected`,
  `awaiting_payment`, `slip_check`, `queued`, `printing`, `ready`,
  `shipped`, `done`, `cancelled`. v1 rows map as: quoted →
  awaiting_payment, queued/printing unchanged, done → ready,
  delivered → done.
- **orders** (today's `quotes` table, kept under that name): add `source`
  (customer site / virtual printer / walk-in), `token`, `customer_phone`,
  `customer_email`, `affiliation`, `category`, `delivery_method`,
  `meetup_at`, `meetup_spot`, `tracking_no`, `needed_by`, `estimate_total`,
  `reject_reason`, `promotion`.
- **order_lines**: one row per uploaded model or keychain line (settings,
  geometry, slice result, chosen colour).
- **special_additions**: order, label, amount, reason, added by.
- **payments**: amount, QR payload, slip file, status, checked by, reason.
- **printer_categories** (config) and **printers** (DB, edited in the
  console): category, name, model, features, active, paused.
- **plate_jobs**: one row per plate run, with category, pinned printer or
  "next free", queue position, spool id, started/finished, outcome. Failure
  stats come from here, per printer and per category.
- **meetup_windows**, **promotions**: ported tables.
- **Spoolman extra fields** (created by the app on first connect).
  Per filament: `wb_show`, `wb_name`, `wb_group`, `wb_good_for`,
  `wb_advice`, `wb_needs` (enclosure / hardened nozzle), `wb_drying_h`,
  `wb_price_per_kg`, `wb_risk`. Per roll: `wb_usable_on` (printer ids).
  `filaments.yaml` keeps pricing modes and fallbacks.

---

## 7. Milestones

Each one ships, so the team gets value before the customer site goes public.

### M0 — Groundwork (S)
- `webuilt/` package and a `migrations/` runner. Today's `init_db` becomes
  migration 001.
- v1→v2 status mapping, with the upgrade test on a copy of a real database.
- Customer-site tests sorted into "port" and "drop".

### M1 — Engineer console upgrades (M) → **v1.3**
- **Special additions** on quotes and orders, with presets in config. On
  receipts in EN/TH. *(PDF minor item)*
- **Printer categories → printers → queues**: Printers screen, pinning, ↑↓
  ordering, Finished / Failed per plate feeding failure stats.
- New nav, plus the **Orders**, **Order** and **Quick quote** screens from the
  mock.

### M2 — Spoolman, PromptPay, virtual printer (M) → **v1.4**
- Spoolman client: live stock, install extra fields, **Filament stock**
  screen (new filament, add roll, show/hide, usable-on), write-back on
  Printing.
- **PromptPay QR** generated from the shop's ID for each approved amount;
  slip check screen.
- **Orca virtual printer**: OctoPrint-compatible `/orca/api/version` and
  `/orca/api/files/local`, with a key per engineer. A plate named
  `WB-…` attaches to that order. *(PDF minor item)*

### M3 — Customer site inside the app (L) → **v2.0 beta** (team-only)
- Customer door (`serve.py --customer`), Jinja pages: landing, set up print,
  order status (all 9 states), guide & prices, track, keychain flow.
- Upload → geometry → slice → build_quote (§5); review queue; approve /
  reject with reason; slip upload.
- Campus meetup windows and booking; promotions.
- Material and colour picker driven by Spoolman (`wb_show`, stock,
  `wb_usable_on`).

### M4 — Notifications and polish (M)
- Customer emails at every status (EN/TH), with order ID, status and cost
  summary.
- Thai for the customer pages (same `locales/` approach as receipts).
- 3D viewer with rotate / lay flat / auto-orient (three.js, vendored). The
  rotation is applied to the mesh before slicing.
- Feedback form, social links, "pause new orders", upload rate limiting.

### M5 — Launch (S) → **v2.0**
- Public exposure of the customer door only (Cloudflare Tunnel or a small
  reverse proxy). TLS and a secure cookie for the public side.
- Backups extended to uploaded files and **payment slips** (personal
  financial data).
- Go-live checklist, a week of team-only use, then public.

---

## 8. Testing

- Keep this repo's plain-script tests (`tests/run_all.py`). Port the
  customer-site checks that still apply: geometry, heuristic, assignment,
  delivery rules, meetups, promotions, keychain.
- New invariants with tests:
  - one price per job whichever door it came through
  - special additions never discounted, never marked up
  - a plate is only assigned to a printer that can use a matching roll
  - the customer door can't reach any console route
  - a v1 database upgrades with no lost rows
  - every customer status renders in EN and TH

---

## 9. Open questions for the owner

1. **Rush**: keep "rush ⇒ same-day courier only", or let rush orders use any
   delivery?
2. **Resin**: price per ml or per gram, and the resin printer's model and
   build size? Until then resin is priced by hand.
3. **Public address** for the customer site, and Cloudflare Tunnel or your
   own proxy?
4. **Email**: which account sends customer emails (Gmail / Google Workspace
   / other)?
5. **PromptPay**: the shop's PromptPay ID and account name.
6. **Rooms and printers**: real room names and printers, to replace the
   mock's Lab A / Lab B.
7. **Real numbers** still open from v1: machine rates, failure measurements,
   special-addition prices, delivery postage.
8. Is the customer site running anywhere with real orders? (Its `data/` was
   empty in the zip, so the plan assumes there's nothing to migrate.)
