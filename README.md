# Gold Intel Database

Hourly auction-house snapshots for **World of Warcraft: Forever (US)**, fetched from the Blizzard Battle.net API by a GitHub Action. [Gold Intel](https://github.com/jjansendev/forever-gold-intelligence) reads them, so its users don't need their own Blizzard API keys.

**Attribution:** auction data from the Blizzard Battle.net API. Not affiliated with or endorsed by Blizzard Entertainment. Free to use; never sold.

## What's here
- `main` branch: `fetch.py` (stdlib Python) and the workflow `.github/workflows/snapshot.yml`.
- `data` branch, replaced on every run:
  - `manifest.json`: houses, the list of snapshots (house, `items`/`commodities`, Blizzard's Last-Modified time, sha256, size) and any errors from the last run.
  - `snapshots/<house>/<scope>-<ms>.json.gz`: the API response as Blizzard sent it, gzipped.
  - `items.json.gz`: item name, quality, level and vendor price for every item seen. Each entry is refreshed within 25 days.

Snapshots are kept for 24 hours (`KEEP_HOURS`). Blizzard's API terms require data to be refreshed or deleted within 30 days.

## Setup (owner)
1. **Settings → Secrets and variables → Actions → New repository secret:** add `BLIZZARD_CLIENT_ID` and `BLIZZARD_CLIENT_SECRET` (from develop.battle.net).
2. **Actions → Auction snapshot → Run workflow** once. After that it runs every hour.
3. In Gold Intel: **Settings → Shared data repo**, enter `jjansendev/Gold-Intel-Database`.

Secrets are masked in the public logs, and the script never prints them. If GitHub ever pauses the schedule (it does for repos with no activity for 60 days), re-enable it on the Actions tab.

## Use from any tool
`https://raw.githubusercontent.com/jjansendev/Gold-Intel-Database/data/manifest.json`, then each snapshot's `path` under the same base. Blizzard's house → realm type/faction mapping isn't published; Gold Intel works it out by comparing with your own in-game scans.
