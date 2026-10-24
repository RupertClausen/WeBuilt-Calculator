# Team server

One shared copy of WeBuilt Quote for the whole team: everyone opens it in a
browser, logs in with their own account, and works on the same orders. Each
order keeps the files it was quoted from, and every order is mirrored to a tab
of the WeBuilt queue spreadsheet.

The same app still runs on someone's own computer exactly as before. Server
mode is off unless switched on.

## Switching it on

On the server, create `local/server.yaml` (never touched by updates):

```yaml
enabled: true
public_url: "https://webuilt-quote.your-tailnet.ts.net"
```

`public_url` is the address teammates open; it's used for the links in the
sheet. Everything else has defaults in `config/server.yaml`.

Then create the first admin, from the server's terminal:

```bash
cd /opt/webuilt-quote
runuser -u webuilt -- .venv/bin/python manage.py add-user yourname --admin --name "Your Name"
```

(`runuser -u webuilt` runs it as the app's own user, so the database stays
owned by it.)

It asks for the password without showing it. After that, admins add everyone
else from the **Admin** page. `manage.py` can also reset a password, change a
role, switch someone off and list users.

## Accounts and roles

| | member | admin |
|---|---|---|
| Quote, orders, files, payments, receipts | ✓ | ✓ |
| Price lists (they include supplier costs) | | ✓ |
| Team accounts, sheet sync, housekeeping, updates | | ✓ |

Everyone can change their own password under **Account**; that signs them
out on their other devices. An admin resetting someone's password, or
switching them off, signs them out everywhere at once. The app refuses to
lose its last active admin.

## What's protected, and how

- **Everything needs a login**: pages redirect to the login page, the API
  answers 401. Only the login page, `/healthz` and the page's own styles are
  open.
- **Passwords** are stored as salted scrypt hashes. **Sessions** are random
  tokens in an HttpOnly, SameSite=Lax cookie; the database keeps only a hash
  of each token, so a copied database can't be used to log in.
- **Wrong passwords**: after 5 the account locks for 10 minutes. The message
  never says whether the username or the password was wrong.
- **Changes need the `X-WeBuilt` header**, which the app's pages send and
  other websites can't, so a page someone happens to be browsing can't make
  changes using their login.
- **Customer files** are stored under generated names, so an uploaded file's
  name can never decide where it's written. They are only ever offered as
  downloads, never opened in the browser.
- **The sheet** is written as plain values, so a customer name like
  `=IMPORTXML(…)` stays text and never runs as a formula.

The app listens only on the server itself (127.0.0.1). Tailscale makes it
reachable to the team, over HTTPS, without exposing it to the internet.

## Orders

Every saved quote is an order. Statuses, in order:

| | | |
|---|---|---|
| Quoted | เสนอราคา | |
| In queue | รอพิมพ์ | |
| Printing | กำลังปริ้น | |
| Done | เสร็จสิ้น | closes |
| Delivered | ส่งแล้ว | closes |
| Cancelled | ยกเลิก | closes |

Change them in `config/server.yaml` (labels, order, which ones close).

Each order page has its status and who's handling it, payment, the customer
and notes, its files (download, or attach more: the customer's model,
photos, a new slice), plate pictures, the price breakdown, receipt links in
both languages, and the full history of who changed what, when.

The **Orders** page lists them newest first, opens on *Open orders*, and
searches receipt numbers, customers, contacts, file names and notes.

There is one receipt counter for the whole team on the server, so the
per-person prefixes needed for separate copies aren't needed here.

## Files and backups

- **Files** are kept while the order is open and for
  `files.keep_days_after_closed` (90) days after it first closes, then
  deleted. The order, its prices and plate pictures are kept forever. The
  order page shows "removed after closing" instead of a download.
- **The database** is backed up once a day after `backups.hour` (03:00) to
  `data/backups/`, keeping the newest `backups.keep` (14). The backup is
  taken with SQLite's online backup, so it's consistent while people work.
  Uploaded files aren't in these backups; Proxmox's backups of the container
  cover them.

The **Admin** page shows the last backup and cleanup, and has **Run now**.

## Google Sheet mirror

The app writes every order to its own tab, **"Orders (app)"**, in the queue
spreadsheet, with the team's usual columns: Order ID, date, customer,
filament, printer, price, payment, status, delivery, plus plates, who created
it, who's handling it and a link back. The app leads: the tab is rewritten
from the app a few seconds after any change and every 30 minutes, so edits
made in that tab are undone. **No other tab is touched**, including the
existing hand-kept queue.

### Setting it up (you do this once)

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project (any name), and enable the **Google Sheets API** for it.
2. Under **IAM & Admin → Service accounts**, create a service account. It
   needs no roles.
3. On the service account, **Keys → Add key → JSON**. A key file downloads.
   Treat it like a password.
4. Copy the key file onto the server as
   `local/google-service-account.json`. Copy it yourself (for example with
   `scp`); don't paste it into chat or commit it.
5. Open the spreadsheet in Google Sheets, **Share** it with the service
   account's email (it ends in `iam.gserviceaccount.com`) as an **Editor**.
   The Admin page shows the exact address once the key is in place.
6. In `local/server.yaml` add:

   ```yaml
   sheets:
     enabled: true
     spreadsheet_id: "the long id from the sheet's URL"
   ```

7. On the **Admin** page, press **Sync now**. It reports either the number
   of orders written, or what to fix ("share the spreadsheet with …",
   "check spreadsheet_id").

Failures are retried automatically with growing pauses; nothing in the app
waits on Google.
