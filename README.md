# Nifty 50 Daily Range Forecast Agent

Runs every weekday at **7:00 PM UK time** and generates a next-trading-day
Nifty 50 range forecast as a Markdown report.

> **Disclaimer:** This is educational technical analysis only, not financial advice.
> Do not trade only based on this report.

---

## What it does

1. Fetches Nifty 50 OHLCV data across five timeframes (5m, 15m, 30m, 1h, 1D)
   — primary source: **TradingView** via `tvdatafeed`; fallback: **Yahoo Finance**.
2. Pulls supplemental data: India VIX, GIFT Nifty, NSE option chain, FII/DII flows.
3. Calculates CPR, Floor Pivots, VWAP, RSI (14), Bollinger Bands (20,2),
   Supertrend (ATR 10, mult 3), ORB levels, and a 20-day Volume Profile.
4. Searches **TradingView's public Pine Script library** daily for community
   indicators (ORB, CPR, Squeeze Momentum, Smart Money Concepts, etc.).
5. Scores every signal (Bullish / Bearish / Neutral) and applies a weighted bias:

   | Component          | Weight |
   |--------------------|--------|
   | Daily chart trend  | 25 %   |
   | 1H trend           | 20 %   |
   | 30M trend          | 15 %   |
   | 15M trend          | 10 %   |
   | 5M trend           | 10 %   |
   | CPR/Pivot location | 10 %   |
   | Community confirm  | 10 %   |

6. Outputs a scenario table keyed on where Nifty opens relative to CPR/pivots.
7. Saves the report to `reports/nifty-report-YYYY-MM-DD.md` and prints it to
   the terminal.

---

## Quick start (local)

```bash
# 1 — clone / download the project
cd nifty-agent

# 2 — create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3 — install dependencies
pip install -r requirements.txt

# 4 — run immediately (bypasses the 7 PM time-window check)
FORCE_RUN=1 python3 nifty_agent.py

# Windows PowerShell
$env:FORCE_RUN="1"; python nifty_agent.py
```

The report is saved to `reports/nifty-report-<today>.md` and printed to stdout.

---

## GitHub Actions scheduling

### One-time setup

1. Push this repo to GitHub (public or private).
2. The workflow `.github/workflows/nifty-agent.yml` is already configured.
   No extra steps needed for the default setup.

### How the scheduling handles UK DST

The UK observes **GMT** (UTC+0) in winter and **BST** (UTC+1) in summer.
7 PM UK time therefore maps to two different UTC times:

| Season | 7 PM UK → UTC |
|--------|--------------|
| Summer (BST, last Sun Mar – last Sun Oct) | 18:00 UTC |
| Winter (GMT) | 19:00 UTC |

The workflow fires at **both 18:00 and 19:00 UTC** Monday–Friday.  
Inside `nifty_agent.py` the `should_run_now()` function checks `Europe/London`
time and exits early if it is not within a 30-minute window of 19:00 UK time.
This ensures exactly one report per day regardless of the season.

### Manual trigger

Go to **Actions → Nifty 50 Daily Analysis Agent → Run workflow**.
Set *force_run* to `1` to skip the time-window check.

### Changing the scheduled time

Edit the two `cron:` lines in `.github/workflows/nifty-agent.yml`:

```yaml
- cron: "0 18 * * 1-5"   # BST: change 18 to your desired UTC hour in summer
- cron: "0 19 * * 1-5"   # GMT: change 19 to your desired UTC hour in winter
```

Also update `target_hour = 19` in `should_run_now()` inside `nifty_agent.py`.

---

## Free data sources used

| Source | What is fetched | Notes |
|--------|-----------------|-------|
| **TradingView** (via `tvdatafeed`) | OHLCV — all 5 timeframes | Anonymous session; may be rate-limited |
| **Yahoo Finance** (via `yfinance`) | OHLCV fallback + India VIX | `^NSEI`, `^INDIAVIX`; free |
| **NSE India** (`nseindia.com`) | Option chain, FII/DII flows | Public JSON endpoints; no API key needed |
| **TradingView public scripts API** | Community indicator search | Public suggest endpoint; no login needed |

---

## Limitations

| Limitation | Detail |
|------------|--------|
| TradingView login | `tvdatafeed` uses an anonymous session. Some data (extended history, premium symbols) requires a paid TradingView account. Set `TV_USERNAME` and `TV_PASSWORD` env vars (see below) to authenticate. |
| NSE rate limiting | NSE blocks aggressive scraping. The agent makes one request per endpoint per run. If blocked, FII/DII and option-chain sections will show "N/A". |
| GIFT Nifty | No reliable free real-time API. The agent tries several yfinance tickers and falls back to N/A. |
| Non-trading days | The agent exits without generating a report if GitHub Actions fires on a public holiday (India/UK). You can add a holiday calendar check if needed. |
| Intraday data | yfinance provides up to 60 days of 1m data and 730 days of 1h data. Data older than this will not be available via the free tier. |

---

## Adding API keys / credentials

All credentials are passed as **GitHub Actions secrets** (never hardcoded).

### TradingView credentials (optional — for premium data)

1. Go to your repo → **Settings → Secrets and variables → Actions → New secret**.
2. Add `TV_USERNAME` (your TradingView username/email).
3. Add `TV_PASSWORD` (your TradingView password).
4. In `nifty_agent.py`, update `fetch_nifty_tvdatafeed`:

```python
tv = TvDatafeed(
    username=os.environ.get("TV_USERNAME"),
    password=os.environ.get("TV_PASSWORD"),
)
```

5. In the workflow, expose the secrets:

```yaml
env:
  TV_USERNAME: ${{ secrets.TV_USERNAME }}
  TV_PASSWORD: ${{ secrets.TV_PASSWORD }}
```

---

## Project structure

```
nifty-agent/
├── nifty_agent.py                  # Main analysis script
├── requirements.txt                # Python dependencies
├── README.md                       # This file
├── reports/                        # Generated reports (git-committed by CI)
│   └── nifty-report-YYYY-MM-DD.md
└── .github/
    └── workflows/
        └── nifty-agent.yml         # GitHub Actions workflow
```

---

## Running on a schedule locally (without GitHub Actions)

Use `cron` on macOS/Linux:

```bash
# Open crontab
crontab -e

# Add this line (adjust path; runs at 19:05 UTC Mon–Fri)
5 19 * * 1-5 cd /path/to/nifty-agent && FORCE_RUN=1 /path/to/.venv/bin/python nifty_agent.py >> /tmp/nifty.log 2>&1
```

On Windows, use Task Scheduler with `FORCE_RUN=1` in the environment variables.
