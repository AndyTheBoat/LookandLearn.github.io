#!/usr/bin/env python3
"""
IWM holdings -> ticker list -> last-day OHLCV -> append to existing CSVs.

NEW:
- At start: deletes ALL *.txt and *.csv in the CURRENT WORKING DIRECTORY (CWD).
- Always writes success_tickers.txt (even if empty).

Saves in CURRENT WORKING DIRECTORY (where you run python):
- Ticker_Russell_2000_Current_Past_YYYY_MM_DD.txt
- Ticker_Russell_2000_Current_Past_YYYY_MM_DD.csv
- IWM_holdings_raw_YYYY_MM_DD.csv
- success_tickers.txt              (always)
- failed_tickers.txt               (always)
- missing_on_stooq.txt             (always, may be empty)

Update mode:
- Default: update ONLY tickers that already exist as CSV files in --data-dir (RECURSIVE)
- Use --update-all-iwm to attempt all IWM tickers (creates new CSVs, more failures)

Data sources:
- Primary: Stooq (q/l then q/d/l fallback)
- Optional fallback: Yahoo via yfinance (enable with --fallback yahoo)

Install:
  pip install requests
  pip install yfinance pandas   (only if you use --fallback yahoo)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed


# iShares IWM holdings CSV endpoints (try multiple; some regions block one or more)
DEFAULT_IWM_HOLDINGS_URLS = [
    "https://www.ishares.com/ch/professionals/en/products/239710/"
    "ishares-russell-2000-etf/1495092304805.ajax"
    "?dataType=fund&fileName=IWM_holdings&fileType=csv",
    "https://www.ishares.com/us/products/239710/"
    "ishares-russell-2000-etf/1467271812596.ajax"
    "?dataType=fund&fileName=IWM_holdings&fileType=csv",
]

# Stooq endpoints
STOOQ_QUOTE_URL = "https://stooq.com/q/l/"   # quote snapshot
STOOQ_DAILY_URL = "https://stooq.com/q/d/l/" # daily history

USER_AGENT = "iwm-stooq-lastday-updater/1.6"
DEFAULT_TIMEOUT = 30


@dataclass
class UpdateResult:
    ticker: str
    ok: bool
    action: str  # UPDATED | CREATED | SKIP | FAILED
    message: str = ""


# ---------------------------
# Purge CWD output files
# ---------------------------

def purge_cwd_txt_and_csv() -> List[Path]:
    """
    Deletes ALL *.txt and *.csv files in the CURRENT WORKING DIRECTORY only (no subfolders).
    Returns the list of deleted paths.
    """
    cwd = Path.cwd()
    deleted: List[Path] = []

    for ext in (".txt", ".csv"):
        for p in cwd.glob(f"*{ext}"):
            try:
                p.unlink()
                deleted.append(p)
            except Exception:
                # If a file is locked, we just skip it.
                pass

    return deleted


# ---------------------------
# Helpers
# ---------------------------

def _try_sniff_dialect(sample: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample)
    except Exception:
        class D(csv.Dialect):
            delimiter = ","
            quotechar = '"'
            doublequote = True
            skipinitialspace = True
            lineterminator = "\n"
            quoting = csv.QUOTE_MINIMAL
        return D()


def _parse_date_yyyy_mm_dd(s: str) -> dt.date:
    return dt.datetime.strptime(s.strip(), "%Y-%m-%d").date()


def _safe_filename_from_ticker(t: str) -> str:
    # Windows illegal: <>:"/\|?*
    t = t.strip().upper()
    return re.sub(r'[<>:"/\\|?*]', "_", t)


def _read_last_data_date(csv_path: Path) -> Optional[dt.date]:
    if not csv_path.exists():
        return None

    try:
        lines = csv_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = csv_path.read_text(encoding="latin-1").splitlines()

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("date,"):
            return None
        date_str = line.split(",")[0].strip()
        try:
            return _parse_date_yyyy_mm_dd(date_str)
        except Exception:
            continue
    return None


def _ensure_header_and_append(csv_path: Path, row: Sequence[str]) -> str:
    header = "Date,Open,High,Low,Close,Volume\n"
    line = ",".join(row) + "\n"

    if not csv_path.exists():
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(header + line, encoding="utf-8")
        return "CREATED"

    with csv_path.open("a", encoding="utf-8", newline="") as f:
        f.write(line)
    return "UPDATED"


def list_existing_tickers_recursive(data_dir: Path) -> Set[str]:
    tickers: Set[str] = set()
    for p in data_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() == ".csv":
            base = p.stem.strip().upper()
            if base:
                tickers.add(base)
    return tickers


def save_iwm_outputs(iwm_tickers: Sequence[str], raw_holdings_csv: str) -> Tuple[Path, Path, Path]:
    today = dt.date.today()
    cwd = Path.cwd()

    txt_path = cwd / f"Ticker_Russell_2000_Current_Past_{today:%Y_%m_%d}.txt"
    csv_path = cwd / f"Ticker_Russell_2000_Current_Past_{today:%Y_%m_%d}.csv"
    raw_path = cwd / f"IWM_holdings_raw_{today:%Y_%m_%d}.csv"

    txt_path.write_text("\n".join(sorted(set(iwm_tickers))) + "\n", encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Ticker"])
        for t in sorted(set(iwm_tickers)):
            w.writerow([t])

    raw_path.write_text(raw_holdings_csv, encoding="utf-8")
    return txt_path, csv_path, raw_path


# ---------------------------
# iShares: download + tickers
# ---------------------------

def _build_ishares_headers(referer: str) -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/csv,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Origin": referer.split("/products/")[0],
    }


def _normalize_ishares_sources(iwm_url: str) -> List[str]:
    if not iwm_url:
        return list(DEFAULT_IWM_HOLDINGS_URLS)
    if "," in iwm_url:
        return [part.strip() for part in iwm_url.split(",") if part.strip()]
    return [iwm_url]


def _is_local_path(source: str) -> bool:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        return False
    return Path(source).expanduser().exists()


def _ishares_referer_for(url: str) -> str:
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if "products/239710" in url:
        return f"{base}/us/products/239710/ishares-russell-2000-etf"
    return f"{base}/ch/professionals/en/products/239710/ishares-russell-2000-etf"


def download_ishares_holdings_csv(iwm_url: str, timeout: int) -> str:
    sources = _normalize_ishares_sources(iwm_url)
    last_error: Optional[str] = None

    for source in sources:
        if _is_local_path(source):
            path = Path(source).expanduser()
            return path.read_text(encoding="utf-8")

        referer = _ishares_referer_for(source)
        headers = _build_ishares_headers(referer)
        with requests.Session() as s:
            s.headers.update(headers)
            try:
                s.get(referer, timeout=timeout)
            except Exception:
                pass

            try:
                r = s.get(source, timeout=timeout)
                if r.status_code == 403:
                    last_error = f"403 Forbidden for {source}"
                    continue
                r.raise_for_status()
                text = r.text.strip()
                if not text:
                    last_error = f"Empty response for {source}"
                    continue
                return text
            except Exception as e:
                last_error = str(e)
                continue

    raise ValueError(f"Unable to download IWM holdings CSV. Last error: {last_error}")


def extract_iwm_tickers(csv_text: str) -> List[str]:
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Empty holdings CSV")

    dialect = _try_sniff_dialect("\n".join(lines[:25]))
    rows = list(csv.reader(lines, dialect=dialect))

    header_idx = None
    for i, row in enumerate(rows):
        if any("ticker" in (c or "").lower() for c in row):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Could not find header row containing 'Ticker'")

    header = [h.strip().lstrip("\ufeff") for h in rows[header_idx]]
    data = rows[header_idx + 1 :]

    ticker_col = None
    for candidate in ("Issuer Ticker", "Ticker", "ISSUER TICKER"):
        if candidate in header:
            ticker_col = header.index(candidate)
            break
    if ticker_col is None:
        for j, h in enumerate(header):
            if "ticker" in h.lower():
                ticker_col = j
                break
    if ticker_col is None:
        raise ValueError("Could not locate ticker column")

    ticker_pat = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")

    out: List[str] = []
    seen: Set[str] = set()

    for row in data:
        if len(row) <= ticker_col:
            continue
        t = row[ticker_col].strip().upper()
        if not t or t in {"-", "N/A", "NA"}:
            continue
        if any(ch.isdigit() for ch in t):
            continue
        if len(t) > 8:
            continue
        if not ticker_pat.match(t):
            continue
        if t not in seen:
            out.append(t)
            seen.add(t)

    if not out:
        raise ValueError("Extracted 0 tickers after filtering (format may have changed).")

    return out


# ---------------------------
# Stooq: fetch last bar
# ---------------------------

def stooq_symbol_candidates_us(ticker: str) -> List[str]:
    base = ticker.strip().upper()
    variants = [base]
    if "." in base:
        variants.append(base.replace(".", "-"))
    if "-" in base:
        variants.append(base.replace("-", "."))

    seen: Set[str] = set()
    uniq: List[str] = []
    for v in variants:
        v = v.strip().upper()
        if v and v not in seen:
            uniq.append(v)
            seen.add(v)
    return [f"{v.lower()}.us" for v in uniq]


def fetch_stooq_quote_bar(session: requests.Session, stooq_sym: str, timeout: int) -> Optional[Tuple[dt.date, List[str]]]:
    params = {"s": stooq_sym, "f": "sd2t2ohlcv", "h": "", "e": "csv"}
    r = session.get(STOOQ_QUOTE_URL, params=params, timeout=timeout)
    r.raise_for_status()

    text = r.text.strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None

    dialect = _try_sniff_dialect("\n".join(lines[:5]))
    header = next(csv.reader([lines[0]], dialect=dialect))
    row = next(csv.reader([lines[1]], dialect=dialect))

    d: Dict[str, str] = {}
    for i, k in enumerate(header):
        if i < len(row):
            d[k.strip()] = row[i].strip()

    date_str = (d.get("Date") or d.get("date") or "").strip()
    if not date_str or date_str.upper() == "N/D":
        return None

    qdate = _parse_date_yyyy_mm_dd(date_str)

    out_row = [
        date_str,
        (d.get("Open") or "").strip(),
        (d.get("High") or "").strip(),
        (d.get("Low") or "").strip(),
        (d.get("Close") or "").strip(),
        (d.get("Volume") or "").strip(),
    ]
    if any(x == "" for x in out_row[:5]):
        return None

    return qdate, out_row


def fetch_stooq_daily_lastbar(session: requests.Session, stooq_sym: str, timeout: int) -> Optional[Tuple[dt.date, List[str]]]:
    params = {"s": stooq_sym, "i": "d"}
    r = session.get(STOOQ_DAILY_URL, params=params, timeout=timeout)
    r.raise_for_status()

    text = r.text.strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None

    dialect = _try_sniff_dialect("\n".join(lines[:5]))
    rows = list(csv.reader(lines, dialect=dialect))
    if len(rows) < 2:
        return None

    header = [h.strip() for h in rows[0]]
    idx = {name.lower(): i for i, name in enumerate(header)}
    required = ["date", "open", "high", "low", "close"]
    if not all(k in idx for k in required):
        return None

    for row in reversed(rows[1:]):
        if len(row) <= idx["close"]:
            continue
        date_str = row[idx["date"]].strip()
        if not date_str or date_str.upper() == "N/D":
            continue
        try:
            qdate = _parse_date_yyyy_mm_dd(date_str)
        except Exception:
            continue

        out_row = [
            date_str,
            row[idx["open"]].strip(),
            row[idx["high"]].strip(),
            row[idx["low"]].strip(),
            row[idx["close"]].strip(),
            row[idx["volume"]].strip() if "volume" in idx and idx["volume"] < len(row) else "",
        ]
        if any(x == "" for x in out_row[:5]):
            continue
        return qdate, out_row

    return None


def fetch_stooq_lastday_ohlcv(session: requests.Session, ticker: str, timeout: int) -> Tuple[dt.date, List[str], str]:
    candidates = stooq_symbol_candidates_us(ticker)
    last_err: Optional[str] = None

    for sym in candidates:
        try:
            q = fetch_stooq_quote_bar(session, sym, timeout=timeout)
            if q is not None:
                return q[0], q[1], sym
        except Exception as e:
            last_err = str(e)

        try:
            q2 = fetch_stooq_daily_lastbar(session, sym, timeout=timeout)
            if q2 is not None:
                return q2[0], q2[1], sym
        except Exception as e:
            last_err = str(e)

    raise ValueError(f"No usable OHLCV from Stooq for {ticker} (tried {candidates}). Last error: {last_err}")


# ---------------------------
# Yahoo fallback (optional)
# ---------------------------

def fetch_yahoo_lastday_ohlcv(ticker: str) -> Tuple[dt.date, List[str]]:
    import yfinance as yf  # optional dependency

    t = ticker.strip().upper()
    df = yf.download(
        tickers=t,
        period="10d",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if df is None or df.empty:
        raise ValueError("Yahoo returned empty dataframe")

    last = df.tail(1)
    idx = last.index[0]
    qdate = idx.date()

    o = float(last["Open"].iloc[0])
    h = float(last["High"].iloc[0])
    l = float(last["Low"].iloc[0])
    c = float(last["Close"].iloc[0])
    v = int(last["Volume"].iloc[0]) if "Volume" in last.columns else 0

    row = [qdate.isoformat(), f"{o}", f"{h}", f"{l}", f"{c}", f"{v}"]
    return qdate, row


# ---------------------------
# Update worker
# ---------------------------

def update_one(
    ticker: str,
    data_dir: Path,
    timeout: int,
    sleep_s: float,
    dry_run: bool,
    fallback: str,
) -> UpdateResult:
    safe = _safe_filename_from_ticker(ticker)
    csv_path = data_dir / f"{safe}.csv"
    last_local = _read_last_data_date(csv_path)

    with requests.Session() as session:
        session.headers.update({"User-Agent": USER_AGENT})
        try:
            try:
                qdate, row, used = fetch_stooq_lastday_ohlcv(session, ticker, timeout=timeout)
                source = f"stooq({used})"
            except Exception as stooq_err:
                if fallback.lower() == "yahoo":
                    qdate, row = fetch_yahoo_lastday_ohlcv(ticker)
                    source = "yahoo"
                else:
                    raise stooq_err

            if last_local is not None and qdate <= last_local:
                return UpdateResult(ticker=ticker, ok=True, action="SKIP", message=f"local={last_local} new={qdate} src={source}")

            if dry_run:
                action = "CREATED" if not csv_path.exists() else "UPDATED"
                return UpdateResult(ticker=ticker, ok=True, action=action, message=f"DRYRUN local={last_local} new={qdate} src={source}")

            action = _ensure_header_and_append(csv_path, row)
            return UpdateResult(ticker=ticker, ok=True, action=action, message=f"local={last_local} new={qdate} src={source}")

        except Exception as e:
            return UpdateResult(ticker=ticker, ok=False, action="FAILED", message=str(e))
        finally:
            if sleep_s > 0:
                time.sleep(sleep_s)


# ---------------------------
# Main
# ---------------------------

def main(argv: List[str]) -> int:
    # PURGE BEFORE ANYTHING ELSE (as requested)
    deleted = purge_cwd_txt_and_csv()
    if deleted:
        print(f"Deleted {len(deleted)} files in CWD (*.txt, *.csv).")
    else:
        print("Deleted 0 files in CWD (*.txt, *.csv).")

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--data-dir",
        default=r"C:\\Users\\andre\\OneDrive\\Desktop\\Russell 2000 Current & Past",
        help="Directory containing per-ticker CSV files (can include subfolders).",
    )
    ap.add_argument(
        "--iwm-url",
        default=DEFAULT_IWM_HOLDINGS_URLS[0],
        help=(
            "iShares IWM holdings CSV URL, comma-separated list of URLs, or local CSV path. "
            "Defaults to the iShares CH endpoint; US endpoint is used as fallback."
        ),
    )
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds")
    ap.add_argument("--sleep", type=float, default=0.03, help="Sleep seconds between requests per worker")
    ap.add_argument("--max-workers", type=int, default=6, help="Parallel workers (keep modest)")
    ap.add_argument("--limit", type=int, default=0, help="Limit tickers for testing (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="Do not write files; just simulate")
    ap.add_argument("--update-all-iwm", action="store_true", help="Update all IWM tickers (creates new files)")
    ap.add_argument("--fallback", default="none", choices=["none", "yahoo"], help="Fallback source if Stooq misses")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"ERROR: data-dir not found: {data_dir}", file=sys.stderr)
        # still create required outputs
        (Path.cwd() / "success_tickers.txt").write_text("", encoding="utf-8")
        (Path.cwd() / "failed_tickers.txt").write_text("", encoding="utf-8")
        (Path.cwd() / "missing_on_stooq.txt").write_text("", encoding="utf-8")
        return 2

    existing = list_existing_tickers_recursive(data_dir)

    print("Downloading IWM holdings...")
    try:
        raw_holdings_csv = download_ishares_holdings_csv(args.iwm_url, timeout=args.timeout)
        iwm_tickers = extract_iwm_tickers(raw_holdings_csv)
    except Exception as e:
        print(f"ERROR: failed to get IWM tickers: {e}", file=sys.stderr)
        # still create required outputs
        (Path.cwd() / "success_tickers.txt").write_text("", encoding="utf-8")
        (Path.cwd() / "failed_tickers.txt").write_text(f"ERROR\t{e}\n", encoding="utf-8")
        (Path.cwd() / "missing_on_stooq.txt").write_text("", encoding="utf-8")
        return 2

    txt_path, csv_path, raw_path = save_iwm_outputs(iwm_tickers, raw_holdings_csv)
    print(f"Saved ticker list TXT: {txt_path}")
    print(f"Saved ticker list CSV: {csv_path}")
    print(f"Saved raw IWM holdings: {raw_path}")

    if args.update_all_iwm:
        tickers = list(iwm_tickers)
        print(f"Tickers from iShares (updating all): {len(tickers)}")
    else:
        tickers = [t for t in iwm_tickers if t in existing]
        print(f"Tickers from iShares: {len(iwm_tickers)}")
        print(f"Existing CSV files found (recursive): {len(existing)}")
        print(f"Will update intersection (IWM ∩ existing): {len(tickers)}")

    if args.limit and args.limit > 0:
        tickers = tickers[: args.limit]

    print(f"Updating files in: {data_dir}")
    if args.fallback != "none":
        print(f"Fallback enabled: {args.fallback}")
    if args.dry_run:
        print("DRY RUN enabled: no files will be modified.\n")

    updated = created = skipped = failed = 0
    success_lines: List[str] = []
    failed_lines: List[str] = []
    missing_on_stooq: List[str] = []

    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as ex:
        futures = [
            ex.submit(update_one, t, data_dir, args.timeout, args.sleep, args.dry_run, args.fallback)
            for t in tickers
        ]
        for fut in as_completed(futures):
            res = fut.result()
            if res.ok and res.action in {"UPDATED", "CREATED", "SKIP"}:
                success_lines.append(f"{res.ticker}\t{res.action}\t{res.message}")
            else:
                failed_lines.append(f"{res.ticker}\t{res.message}")
                if "No usable OHLCV from Stooq" in res.message:
                    missing_on_stooq.append(res.ticker)
                print(f"[{res.action}] {res.ticker}: {res.message}")

            if res.action == "UPDATED":
                updated += 1
            elif res.action == "CREATED":
                created += 1
            elif res.action == "SKIP":
                skipped += 1
            else:
                failed += 1

    # Always write these 3 files (even if empty)
    success_path = Path.cwd() / "success_tickers.txt"
    failed_path = Path.cwd() / "failed_tickers.txt"
    missing_path = Path.cwd() / "missing_on_stooq.txt"

    success_path.write_text("\n".join(success_lines) + ("\n" if success_lines else ""), encoding="utf-8")
    failed_path.write_text("\n".join(failed_lines) + ("\n" if failed_lines else ""), encoding="utf-8")
    missing_path.write_text("\n".join(sorted(set(missing_on_stooq))) + ("\n" if missing_on_stooq else ""), encoding="utf-8")

    print(f"\nWrote success log: {success_path}")
    print(f"Wrote failure log: {failed_path}")
    print(f"Wrote Stooq-missing list: {missing_path}")

    print("\nDone.")
    print(f"  UPDATED: {updated}")
    print(f"  CREATED: {created}")
    print(f"  SKIPPED: {skipped}")
    print(f"  FAILED : {failed}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
