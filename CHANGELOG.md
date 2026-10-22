# Changelog

What changed in each version. The newest section is shown in the app as
**What's new** when the update is offered.

## [Unreleased]

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
