# WeBuilt quote service

Upload a sliced file, get a price. Internal tool for the WeBuilt print service.

```bash
pip install -r requirements.txt
python3 -m uvicorn app:app --reload --port 8000
# open http://localhost:8000
```

To try things without adding to the real order log, point the server at a
throwaway database: `WEBUILT_DB=/tmp/trial.db python3 -m uvicorn app:app --port 8001`.

Run the tests with `python3 tests/run_all.py` (42 tests, 4 suites). They
use a throwaway database and never touch `orders.db`.

---

## Why HTTP and not WebSocket

You asked for either. This is one request in, one quote out — there is no
stream to hold open, so a socket would add reconnection and state handling
for nothing. If uploads of very large files ever need a progress bar, add
Server-Sent Events to `/api/quote` rather than switching transport.

## What replaced the spreadsheet's `hours ** 1.5`

The old time cost was `rate * hours^1.5`. That made the effective hourly rate
climb from ฿100/h at one hour to ฿313/h at ten, which no real cost behaves
like — and it was dimensionally incoherent, so feeding it minutes instead of
hours would have changed the answer by 465x.

Machine time is now **linear**. What genuinely grows with duration is the
chance of failure, and that is modelled explicitly, per machine:

```
p_success = p_start * exp( -(hazard * material_risk * hours) ** shape )
risk_factor = 1 + (1/p_success - 1) * failure_consumed_fraction
```

`hazard * hours` is dimensionless, so the unit bug cannot recur.

### Each printer has its own failure model

Not just its own constants — its own curve and its own per-material table.

| | `normal` | `large` |
|---|---|---|
| `p_start` | 0.97 | 0.94 |
| `hazard` /h | 0.015 | 0.025 |
| `shape` | 1.0 (constant) | 1.3 (rising) |
| ABS `material_risk` | 1.6 | 2.1 |

`shape` is the Weibull exponent and is what makes the *model* differ rather
than just the numbers:

- **1.0** — constant hazard, failures arrive at a steady rate. Memoryless.
- **> 1** — hazard rises with print length. Machines that heat-soak, drift
  out of tune, or warp big parts late. Long jobs are disproportionately risky.
- **< 1** — hazard falls with length. Trouble shows up early; a print past
  hour two will almost certainly finish. Runout sensors push a machine here.

`material_risk` is per machine because an enclosure changes what ABS *is*:
the same spool is a 1.6x job in a heated chamber and a 2.1x job on the large
open bed. Resolution order is printer override → fleet default in
`filaments.yaml` → 1.0.

`headroom` (in `pricing.yaml`) is kept separate from all of this on purpose,
so you can be cautious without corrupting the measured statistics.

### Measuring the numbers

- `p_start` = 1 − (prints that died in roughly the first 10 min / total starts)
- `hazard` = (prints that died after the first 10 min) / (total running hours)
- `shape` — bucket failures by the hour they happened. Flat histogram → 1.0;
  rising → above 1; front-loaded → below 1.

Every quote is logged to `orders.db`. Mark what actually happened with
`POST /api/orders/{id}/outcome` (`completed` / `failed`) and after a few
hundred jobs you can fit all three parameters from your own history instead
of guessing. Until then each printer carries `measured: false` and quotes
carry a visible warning.

## Two input modes

**Upload sliced files**, or **enter the numbers by hand**. Both go through
the identical pricing path — there is one `build_quote`, and a test asserts
the two routes return the same total for the same job.

Upload takes **as many files as the order needs**. A customer with more parts
than fit on one bed sends one file per plate; drop them all in, set copies on
any that repeat, and it quotes as a single order. A `.gcode.3mf` holding
several plates is split automatically — three plates inside one file count as
three plates, not one.

Manual mode exists for quoting from a photo, a sketch, a customer's own
estimate, or a repeat of a job you already know. It takes what the file would
have told us (per-material grams, print time, plates) and then shares every
option with upload mode.

`POST /api/quote/manual`:

```json
{
  "filaments": [{"type": "PETG", "grams": 222.86}],
  "hours": 9, "minutes": 49,
  "printer": "normal",
  "client_profile": "architecture",
  "prep_minutes": 25,
  "plates": 1,
  "rush": "standard",
  "delivery": "pickup",
  "postproc_minutes": 20,
  "label": "WB-2610-010"
}
```

Multiple filament rows are supported, and a material not in the roster still
prices (at the fallback rate) with a warning rather than failing.

## Multiple plates

An order is a **list of plates**, never a lump. Each plate is priced and
risked on its own duration.

This matters because survival is exponential in duration, so risk is convex
and does not aggregate. Two plates of 2 h and 20 h are **not** two 11 h
plates:

| order | plate risk factors | total |
|---|---|---|
| 2 h + 20 h | 1.030, 1.195 | ฿785 |
| 11 h + 11 h | 1.106, 1.106 | ฿745 |

Same two plates, same 22 hours, same 200 g — ฿40 apart purely from how the
work is distributed. Summing grams and hours before pricing would have
flattened both to the same number, under-charging the long plate's risk and
over-charging the short one's.

Each file can also go on a different printer within one quote (`QuoteItem.printer_id`),
so a tall part can take the large machine while the small parts stay on the
normal one — and each then gets that machine's own failure curve.

### Plate fees

```
1 plate  -> 50
2 plates -> 50 + 20  = 70
3 plates -> 50 + 40  = 90
```

`setup_fee: 50` covers the order *and* the first plate's handling.
`additional_plate_fee: 20` covers each plate after that — the extra bed prep,
removal and restart.

The ฿20 sits **outside** the failure multiplier, as a flat handling charge.
The material and machine cost of an actual reprint is already carried by that
plate's risk factor; headroom absorbs the small labour under-recovery.

## Client profiles — replacing the faculty exponent

The old calculator gave engineering `expo 1.3` and architecture `expo 1.5`.
Since `time_cost = rate * hours^expo`, the architecture premium was
`hours^0.2`, which behaves badly at both ends:

| hours | old premium |
|---|---|
| 0.25 | **0.76×** — architecture paid *less* |
| 0.5 | **0.87×** — architecture paid *less* |
| 1 | 1.00× — no premium at all |
| 10 | 1.58× |
| 48 | 2.17× |
| 100 | 2.51× and still climbing |

Below one hour the exponent **inverted**, because `h^1.5 < h^1.3` whenever
`h < 1`. An architecture student with a 15-minute print was getting a 24%
discount.

The deeper problem is that one exponent was standing in for three different
costs that do not scale alike:

| cost | scales with | old model |
|---|---|---|
| **File prep** — mesh repair, rescaling, reorienting | nothing. Flat per job | never charged |
| **Failure risk** — thin walls, overhangs, knife edges | duration | the only part it expressed, unboundedly |
| **Post-processing** — supports out of delicate work | part complexity | never charged |

Each now lands where the cost actually occurs:

```yaml
architecture:
  prep_minutes: 25          # flat, billed at labour_rate_per_hour
  risk_multiplier: 1.45     # multiplies the printer's hazard
  postproc_multiplier: 1.6  # scales the minutes you enter
  price_multiplier: 1.0     # blunt instrument, left off by default
```

Resulting premium, 100 g PLA on `normal`:

| hours | engineering | architecture | new | old |
|---|---|---|---|---|
| 0.25 | ฿150 | ฿195 | 1.30× | 0.76× |
| 1 | ฿165 | ฿210 | 1.27× | 1.00× |
| 4 | ฿230 | ฿280 | 1.22× | 1.32× |
| 10 | ฿370 | ฿425 | 1.15× | 1.58× |
| 48 | ฿1,655 | ฿2,090 | 1.26× | 2.17× |

Steady 1.15–1.30× with a floor at the short end, instead of 0.76×–2.17×.
The short-print premium is now the *largest* in relative terms, which is
correct: that is where fixed prep cost dominates.

`risk_multiplier` compounds with the material's, so a thin-walled
architecture model in ABS on the large printer is all three problems at once
(1.6 × 1.45 = 2.32× hazard).

### On framing

These are **defaults, not verdicts**. The charge is driven by `prep_minutes`,
overridable per job from the UI. An architecture student who sends a clean,
manifold, correctly-scaled file should be dialled down to the engineering
preset; an engineering student who sends a non-manifold SketchUp export
should be dialled up.

Same revenue, but defensible line by line — "your file needed 25 minutes of
repair" is a conversation you can have, where "you are an architecture
student" is one that travels badly in a small campus market. It also
self-corrects as students learn, which a fixed faculty surcharge does not.

## Risk cap

The survival model is exponential, so on a long enough job the implied
reprint cost runs away: at `hazard 0.015/h`, a 100-hour print computes to a
22% first-try success rate and a 2.63× risk factor. Arithmetically right,
commercially meaningless — past a point you renegotiate rather than price in
three reprints.

`max_risk_factor: 2.5` in `pricing.yaml` bounds it, and the quote warns when
the cap engages. **A real quote hitting the cap is a signal**: either the
hazard is set too high and needs re-measuring, or that job should not be
taken as-is.

## Receipts

Every saved quote gets a number and a document:

| | |
|---|---|
| `/receipt/{id}` | print-ready page. **Print / Save as PDF** gives an A4 PDF |
| `/receipt/{id}.txt` | plain text, for pasting into LINE or email |
| `/api/receipt/{id}` | the customer-safe data behind both |

After quoting, the result shows an **Open WB-YYMM-NNN →** link. Customer name,
contact and a note go in the "Customer details" section of the form.

**Numbering** follows your queue sheet, `WB-YYMM-NNN`, restarting monthly.
Your sheet already used WB-2610-001 to 009, so `next_number_floor` in
`config/receipt.yaml` starts October 2026 at 010. Add a line for any month
where you hand out numbers outside the tool, or type a number into the form
to override; reusing one is refused.

**Quotation vs Receipt.** The document is titled *Quotation* with an
"Awaiting payment" stamp until you record payment from the bar at the top
(PromptPay / bank transfer / cash). Then it becomes a *Receipt* with a paid
stamp and timestamp. The operator bar never prints.

### English and Thai

Every receipt can be shown in either language. The **ภาษาไทย** / **English**
link at the top right of a receipt switches it, and the "Receipt language"
picker in the quote form sets which one the **Open WB-… →** link opens. The
plain-text copy for LINE follows whichever language is on screen. You can
also add `?lang=th` or `?lang=en` to any receipt link.

`default_language` in `config/receipt.yaml` decides what a receipt shows when
no language is chosen. It is `en` for now.

The Thai version uses Thai dates with the Buddhist Era year
(`21 ต.ค. 2569 14:10 น.`), "หน่วย" for kWh, and Thai names for rush tiers,
delivery options, printers and payment methods. The amounts and lines are
identical in both languages; a test checks it.

All the wording lives in `locales/en.yaml` and `locales/th.yaml`, not in the
code. **The Thai text is my translation: please read `locales/th.yaml` and
fix anything that sounds off.** Edits take effect on the next page load. Keep
the `{placeholders}` intact; a test fails if a key or placeholder in one file
has no match in the other. A key missing from `th.yaml` shows English rather
than breaking the page.

Labels from the config files have a Thai version alongside: `label_th` on
rush tiers, delivery options and printers, and `{en: …, th: …}` for the
tagline, terms and footer in `receipt.yaml`. Any of those text fields can also
be a single string used for both languages.

Quotes store the rush, delivery and printer *keys*, so a receipt can show
those labels in any language later. Quotes saved before this change keep the
label they were saved with.

### What "transparent" means here

The document shows every charge, the quantity and unit rate behind it, a
per-plate table with each plate's estimated first-time success rate, a plain
"how this price is calculated" section, and the terms. A test asserts the
lines add up to the total exactly, for every order shape.

It deliberately does **not** show:

- **Supplier cost (`cost_per_kg`), estimated direct cost, or margin.** Those
  are your numbers, not the customer's. Printing them tells the customer
  nothing about their job and gives away your negotiating position.
- **The client profile name.** The customer sees its effect instead:
  estimated minutes of file preparation, and each plate's success rate. That
  is the part they can check and act on, and a student who sends a cleaner
  file sees the prep line shrink.

The document is built from a whitelisted view model in `receipt.py`, never by
passing the stored quote through. A test renders every profile in all three
formats and fails if any of those terms appear.

### It does not overclaim

Two sentences only appear when they are true:

- "estimated from our own printers' failure records" appears only once every
  printer in the order is `measured: true`. Until then it says the estimate
  comes from typical rates and you are still collecting your own.
- "a reprint caused by a print failure costs you nothing" appears only when
  `free_reprints: true`, so the method text can never contradict your terms.

File prep and post-processing minutes are labelled **estimated**, since they
are, and post-processing shows the minutes billed after the profile's
multiplier rather than implying they were timed.

**Review the terms in `config/receipt.yaml` before handing these out.** They
are placeholders. The first one matters most: if the customer pays a failure
allowance, the document has to say what it buys.

## Config

Three files under `config/`, hot-reloaded on save — no restart needed.

| file | holds | changes |
|---|---|---|
| `pricing.yaml` | policy: fees, headroom, rush tiers, delivery, rounding | rarely |
| `printers.yaml` | measured per-machine physics and failure models | when you measure |
| `filaments.yaml` | the price roster | constantly |
| `receipt.yaml` | shop details, terms, receipt numbering, default language | rarely |
| `locales/*.yaml` | every word on a receipt, per language | when wording needs fixing |

`filaments.yaml` is separate because it is the one you will edit weekly, and
it keeps **cost and price in two different columns on purpose**:

- `cost_per_kg` — what you pay the supplier. Margin tracking only.
- charged price — derived by `pricing_mode`.

Keeping the real cost honest even while charging a flat rate is what lets you
notice a material going unprofitable after a supplier change. Conflated into
one number, a flat rate hides exactly that.

`pricing_mode` options:

- `flat_standard` — one rate for PLA/PETG/ABS. Simplest to quote, and safe
  because filament is only ~5–9% of a correct price. **But then
  `material_risk` carries the entire PLA-vs-ABS difficulty signal, so do not
  leave ABS at 1.0.**
- `max_restocked` — per type, the dearest brand you *routinely restock*. Note
  it keys off restocked brands, not the most expensive spool in the room, so
  one premium one-off purchase cannot permanently ratchet the type's price up.
- `per_brand` — actual brand cost × markup.

## Pricing structure

```
for each plate:
    cost   = material + machine + electricity + maintenance
    risked = cost * risk_factor(that plate's own duration)
order  = setup_fee
       + (total_plates - 1) * additional_plate_fee
       + sum(risked)
       + file_prep + post_processing
order  = order * headroom * rush * client_price
total  = round_up(max(order + delivery_fee, minimum))
```

- `setup_fee` is charged **once per order** and includes the first plate;
  `additional_plate_fee` covers each plate after that.
- Maintenance is **per machine-hour**, not a percentage of filament cost —
  nozzle and belt wear do not care what a spool cost.
- Delivery is added **after** the multipliers. You do not mark up postage for
  failure risk.

## API

| | |
|---|---|
| `POST /api/quote` | multipart: **`files`** (repeatable), `copies` (parallel list), `printer`, `rush`, `delivery`, `postproc_minutes`, `client_profile`, `prep_minutes`, `save` |
| `POST /api/quote/manual` | JSON: same options, plus `filaments[]`, `hours`, `minutes`, `label` |
| `GET /api/options` | printers, rush tiers, delivery options for the UI |
| `GET /api/config` | the three config files as loaded |
| `POST /api/config/reload` | force a reload |
| `GET /api/orders` | quote history |
| `POST /api/orders/{id}/outcome` | mark `completed` / `failed` |
| `POST /api/orders/{id}/payment` | `status=paid\|unpaid`, `method` |
| `GET /receipt/{id}` · `/receipt/{id}.txt` · `/api/receipt/{id}` | quotation / receipt; add `?lang=th` or `?lang=en` |

## Parser

Handles OrcaSlicer and Bambu Studio:

- **`.gcode.3mf` / `.3mf`** — reads `Metadata/slice_info.config`, which states
  per-filament grams and predicted seconds as structured XML. Preferred:
  nothing is inferred. An unsliced project file is rejected with a clear
  message.
- **`.gcode`** — reads the comment block. Accepts both `; key = value`
  (Orca) and `; key: value` (Bambu). Handles multi-extruder values split on
  `;` or `,`.

`parse()` returns one `SlicedJob` **per plate**. A plain `.gcode` gives one; a
`.gcode.3mf` holding three plates gives three, each with its own grams and
duration. They are deliberately never flattened — see **Multiple plates**.

AMS slots of the same material are merged, since they price identically. When
a plate mixes materials, the **riskiest** one drives its failure model.

If one file in a multi-file upload fails to parse, the error names it
("`broken.3mf: This 3MF has no Metadata/slice_info.config…`") rather than
failing anonymously.

## Sanity check against real orders

The 222.86 g / 9h49m PETG job from your spreadsheet:

| | |
|---|---|
| Old spreadsheet | **฿4,486** |
| This, on `normal` | **฿465** |
| This, on `large` | **฿660** |

Your largest real order to date was ฿452 (WB-2610-004, ABS on Large). The
linear model lands next to what you actually charge; the spreadsheet was an
order of magnitude out.

At the other end, a 3 g / 12 min keychain quotes at **฿90**, against the ฿8
you charged for WB-2610-002. The ฿50 setup fee is what closes that gap — the
same structural fix in both directions.

## Still to decide

Numbers only; all of them live in the config files.

1. **Measure the failure rates.** Everything is `measured: false` right now.
2. **Fill in real filament costs.** Current roster prices are placeholders.
3. **Rush tiers** — are +20% next-day and +50% same-day what you actually want?
4. **Delivery fees** — ฿20 to the university and ฿60 express are guesses.
5. **`labour_rate_per_hour: 120`** for post-processing — your call.
6. **Is ฿40 setup / ฿50 minimum right** for a student market? It roughly
   quadruples your smallest historical orders.
7. **`failure_consumed_fraction`** — 0.45 assumes failures are caught about
   halfway. Set 1.0 to charge as if every failure wastes a whole print.
8. **Architecture profile values** — 25 min prep, 1.45× risk, 1.6×
   post-processing are my estimates of what the old 1.3-vs-1.5 split was
   reaching for. Time a few real architecture files and replace them.
9. **`max_risk_factor: 2.5`** — the point past which you would rather talk to
   the customer than quote a reprint allowance.
10. **Receipt details and terms** in `config/receipt.yaml`: LINE ID, phone,
    pickup point, and whether the four placeholder terms are what you
    actually offer.
