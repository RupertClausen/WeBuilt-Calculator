# Publishing a new version

Teammates get changes through **releases**. Each one is a numbered version.
GitHub builds it automatically when you push a tag, and the app on everyone's
computer offers it as an update.

## Version numbers

`MAJOR.MINOR.PATCH`, like `1.4.2`. Pick by what changed:

| bump | when | example |
|---|---|---|
| **PATCH** `1.4.2 → 1.4.3` | settings and prices only | new filament prices, a printer's measured failure rate, a delivery fee |
| **MINOR** `1.4.3 → 1.5.0` | the app itself changes, nobody has to do anything | a new feature, a fix, new wording on receipts |
| **MAJOR** `1.5.0 → 2.0.0` | teammates must change their `local/` files | a setting renamed or restructured |

Every saved quote records the version that made it, so you can always tell
which price list a quote was based on.

## Steps

1. Make the change. Prices and settings are in `config/`; wording on
   receipts in `locales/`.
2. Run the tests:
   ```bash
   python tests/run_all.py
   ```
3. Set the new version in `release.json`.
4. Add a section to the top of `CHANGELOG.md`. Teammates see it as **What's
   new** in the app, so write it for them:
   ```markdown
   ## [1.4.3] - 2026-11-02

   ### Prices
   - PETG up from ฿540 to ฿560/kg after the supplier's increase.
   ```
5. Commit, tag and push:
   ```bash
   git commit -am "Release 1.4.3: PETG price"
   git tag v1.4.3
   git push origin main v1.4.3
   ```

GitHub then runs the tests on Linux and Windows, checks that the tag matches
`release.json`, builds `webuilt-quote-1.4.3.zip` with its checksum, and
publishes the release with your changelog section as its notes. Follow it
under the repository's **Actions** tab. If anything fails, nothing is
published.

Within a few hours each teammate's app shows the update bar. They can click
**Update and restart** straight away.

## If a release is bad

Publish a fixed one with a higher number; everyone's app offers it. Anyone
who needs the old version back immediately can run
`python updater.py rollback`. Never delete or reuse a version number: the
updater only moves forward.

## Changing packages

`requirements.txt` pins exact versions. To change one, update it, run the
tests, and mention it in the changelog. The updater reinstalls packages only
when this file changes.

## What goes into a release

`tools/build_release.py` decides: the app's Python files, the launchers,
`config/`, `locales/`, `static/`, `docs/`, the README and changelog. Tests,
CI and tools stay in the repository only. The zip also carries
`MANIFEST.json`, listing every file with its SHA-256. The updater replaces
only files on that list, which is how it knows never to touch `data/` or
`local/`.

To build one locally to look at: `python tools/build_release.py` (the
output goes to `dist/`).

## First-time setup

1. Create a repository on GitHub. **Make it private.** `config/filaments.yaml`
   contains what you pay suppliers and `config/pricing.yaml` your profit
   margin. A public repository shows both to anyone, customers included.
2. Push this repository to it, including tags.
3. Add teammates as collaborators with **Read** access.
4. Each teammate makes a read-only token for updates (see the README).

GitHub Actions needs no setup: the release workflow uses the token GitHub
provides to every run.
