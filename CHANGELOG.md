# Changelog

What changed in each version. The newest section is shown in the app as
**What's new** when the update is offered.

## [1.3.0] - 2026-11-09

### Special additions
- Extra work the calculator can't see (model repair, sanding, acetone,
  airbrush, inserts, or anything typed by name) is added as its own line at
  exactly the engineer's price: no margin, no discount, after rounding.
- On the quote page, and on an order afterwards until it's paid. Shown on
  receipts in English and Thai with the engineer's reason.
- One-click presets in config/pricing.yaml (prices still to be filled in).

### Printers and the plate queue
- Normal and Large are now printer *categories*; add the actual machines in
  each (P2S #1, P2S #2…) on the new **Printers** page. Quotes still price by
  category, so adding a printer changes no price.
- Moving an order to *In queue* puts each of its plates in its category's
  queue. Start a plate on a free printer, pin it to one, or reorder; the
  page shows what each printer is printing and when the next plates start.
- *Finished* / *Failed — reprint* per plate: a failure counts against that
  printer and the reprint goes to the front of the queue. The first plate
  started moves the order to Printing; the last one finished moves it to
  Ready.

### Order statuses
- New list: Needs review, Not accepted, Awaiting payment, Slip to check, In
  queue, Printing, Ready, Shipped, Done, Cancelled. Existing orders move over
  by themselves when the app updates: Quoted → Awaiting payment, Done →
  Ready, Delivered → Done. New quotes start at Awaiting payment.

### Also
- The database now upgrades in numbered steps, and refuses to run on a
  database from a newer version (after a rollback) instead of touching it.
- Release packages include the new `webuilt/` code; a test checks every
  module ships.

## [1.2.0] - 2026-10-24

### Team server
- One shared copy for the whole team (switched on in local/server.yaml), with
  a login per person: admins (price lists, accounts, sheet, updates) and
  members (quoting and orders).
- Orders: statuses from quoted to delivered, who's handling it, notes,
  payment, and the full history of who changed what.
- Uploaded files are kept with each order, downloadable, and more can be
  attached later. Kept 90 days after an order closes.
- Google Sheet mirror: every order on its own tab of the queue spreadsheet,
  in Thai, updated a few seconds after any change.
- Daily database backups.
- New pages: Orders, an order's page, Admin, Account, and a login page.

### Also
- Every dependency is now pinned, including the ones pulled in indirectly,
  so all installs are identical.
- Browsers always check for a newer copy of the app's pages and scripts, so
  an update can't leave someone running old scripts.
- Restarting no longer moves the app to another port while the old one is
  still winding down; the team server always waits for its own port.

## [1.1.1] - 2026-10-23

### Receipt numbers
- Typing a receipt number that's already used no longer stops the quote: it
  is saved under the next free number, with a warning saying which.
- The automatic number only continues from numbers the app chose itself, and
  steps over any that are taken. A hand-typed outlier like WB-2610-999 no
  longer makes every following receipt 1000-something.
- The receipt number field clears after each saved quote.

## [1.1.0] - 2026-10-22

### Quoting
- Discounts: pick a shared preset (Repeat customer 10%, Club member 15%) or a
  one-off percentage or baht amount with a reason. Never below the job's
  break-even, never on delivery. Shown on receipts as its own line, in
  English and Thai.

### Fixes
- Starting the app on Linux no longer prints the browser's internal error
  messages into the app window.

## [1.0.0] - 2026-10-21

First release for the team.

### Quoting
- Price sliced files from OrcaSlicer and Bambu Studio (`.gcode` and
  `.gcode.3mf`), several files per order, or enter the numbers by hand.
- Each plate is priced on its own print time and its own printer's failure
  rate, and shows the picture the slicer embedded.
- Client profiles for file preparation, risk and post-processing.
- Fees: ฿50 per order including the first plate, ฿20 for each further plate.
- 15% profit margin, shown on receipts as **Service margin** / **ค่าบริการ**.

### Receipts
- Quotations and receipts in English or Thai, printable as PDF or copied as
  text for LINE.
- Never show supplier costs or internal figures.

### Running it
- `run.bat` (Windows) and `run.sh` (Linux) set everything up on first start.
- Updates from inside the app, with backups and automatic rollback.
- Personal settings in `local/` that updates never touch.
- Order history in `data/orders.db`. A pre-1.0 `orders.db` in the app folder
  is moved there automatically.
