# WeBuilt Quote

The price calculator for WeBuilt 3D printing. Drop in sliced files from
OrcaSlicer or Bambu Studio (or type the numbers in), get a price, and hand
the customer a quotation or receipt in English or Thai.

It runs on your own computer, in your browser. Nothing is sent anywhere
except the occasional check for a newer version.

- [Install](#install) · [Use it](#use-it) · [Updates](#updates) ·
  [Your own settings](#your-own-settings) · [Your data](#your-data) ·
  [Problems](#problems)
- How the pricing works: [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md)
- Publishing a new version: [docs/RELEASING.md](docs/RELEASING.md)
- Running one shared copy for the team: [docs/TEAM-SERVER.md](docs/TEAM-SERVER.md)

## Install

You need **Python 3.10 or newer**. Everything else is set up automatically
the first time you start the app.

### Windows

1. Install Python from <https://www.python.org/downloads/windows/>. On the
   first screen of the installer, tick **Add python.exe to PATH**.
2. Download `webuilt-quote-<version>.zip` from the repository's
   **Releases** page and extract it somewhere you'll keep it, for example
   `Documents\WeBuilt`.
3. Double-click **`run.bat`**.

### Linux

```bash
sudo apt install python3 python3-venv     # Ubuntu / Debian
```

Download and extract the release zip, then in that folder:

```bash
bash run.sh
```

The first start takes a minute or two: it creates a private `.venv` folder
and installs the exact package versions the app is tested with. It needs
internet for that. Later starts take a few seconds.

## Use it

Starting the app opens it in your browser at `http://127.0.0.1:8000`
(or the next free port). **Keep the black window open** while you use it;
close it, or press Ctrl+C in it, to stop the app.

Only your own computer can reach it. Other people on the same network
cannot.

### Discounts

Pick one from **Discount** before quoting: a shared preset (like *Repeat
customer*) or **One-off…** with your own percentage or baht amount and a
reason, which prints on the receipt. A discount never takes the price below
what the job costs; if you ask for more, it's capped and the app tells you.
Delivery is never discounted.

## Updates

When a new version is out, a blue bar appears at the top of the app. Click
**What's new** to see what changed, then **Update and restart**. The app
closes, installs the update and opens again by itself. Your saved quotes,
receipts and personal settings are kept.

Most updates only change prices and settings; some also improve the app
itself. Either way, it's the same button.

From a terminal instead, with the app closed:

```bash
python updater.py check       # is there a newer version?
python updater.py apply       # install it
python updater.py rollback    # go back to the version before the last update
```

Every update keeps a copy of the version it replaced in `backups/` (the last
five), checks the download against its published checksum and every file
inside against the release's list, and undoes itself if anything goes wrong.

**If the repository is private**, the app needs permission to see it:

1. On GitHub: **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**.
2. Repository access: **only** the WeBuilt Quote repository.
   Permissions: **Contents → Read-only**. Nothing else.
3. Save the token, alone, in a file called `local/github_token` inside the
   app folder.

That file stays on your computer: updates never touch `local/`, and git
never includes it.

**No internet, or no token?** Someone can download the release zip for you;
then run `python updater.py apply --zip path/to/webuilt-quote-X.Y.Z.zip`.
Keep the matching `.zip.sha256` file next to it and the checksum is verified
too.

## Your own settings

Shared settings and price lists live in `config/` and are **replaced by
every update**, so don't edit them there. Put anything specific to your
computer in **`local/`**, using the same file names:

| file in `local/` | for example |
|---|---|
| `receipt.yaml` | your receipt number prefix, Thai as default, your LINE ID |
| `printers.yaml` | your own printers, or your measured failure rates |
| `pricing.yaml`, `filaments.yaml` | rarely; prices should come from updates |
| `settings.yaml` | `update_check: false` to stop update checks |

You only write what differs. The app lays your file over the shared one:
settings you list replace the shared ones, everything else stays shared, and
setting something to `null` removes it (`large: null` hides a printer you
don't have). Ready-made examples are in
[docs/local-examples/](docs/local-examples/).

### Set your receipt prefix

Each computer numbers its own receipts. If two people both use the default
`WB`, both will issue `WB-2610-010`. Give everyone their own prefix in
`local/receipt.yaml`:

```yaml
receipt_prefix: "WB-A"      # receipts become WB-A-2610-001, WB-A-2610-002 …
```

Changes in `config/` or `local/` take effect on the next quote; no restart
needed. If a file has a mistake in it, the app keeps using the last working
version and shows a warning at the top until it's fixed.

## Your data

| folder | what | touched by updates? |
|---|---|---|
| `data/` | `orders.db`: every saved quote, customer details, payments, plate pictures | never |
| `local/` | your own settings and token | never |
| `backups/` | the last five versions the updater replaced | the updater only |
| `.venv/` | installed packages | reinstalled when needed |

**Moving to another computer:** install the app there, then copy your `data/`
and `local/` folders across.

**Back up `data/`** now and then; it's the only copy of your order history.

## Problems

**"needs Python 3.10 or newer"** — install it (see [Install](#install)).
On Windows, reinstall with **Add python.exe to PATH** ticked.

**"Could not create the .venv folder"** on Linux — `sudo apt install python3-venv`.

**"Installing packages failed"** — the first start needs internet. Check the
connection and run it again.

**The update bar doesn't appear** — the check runs in the background every
few hours. For a private repository you need `local/github_token`. Run
`python updater.py check` to see the exact reason.

**"This folder is a git checkout"** — you're running a developer copy; update
it with `git pull` instead.

**An update went wrong** — `python updater.py rollback` puts back the
previous version.
