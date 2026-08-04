#!/usr/bin/env python3
"""
Netnaija API Scraper — GitHub Action Edition
============================================
- Resumes from last saved state (page number stored in page_state.json)
- Commits & pushes after every 2 pages using built-in GITHUB_TOKEN
- Stops automatically when no more data (hasMore=False or empty items)
- Appends data into a single rolling JSON file; creates next file when 5 MB reached
- Global deduplication across all runs (no duplicates ever saved)
- Saves JSON files to ./output/

Environment variables (auto-set by GitHub Actions):
  GITHUB_TOKEN      — Built-in GitHub token for repo write access
  GIT_USER_NAME     — Git commit author name  (default: "GitHub Action")
  GIT_USER_EMAIL    — Git commit author email (default: "action@github.com")
"""

import json
import os
import subprocess
import time
import logging
from typing import List

import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────

BASE_URL = "https://h5-api.aoneroom.com/wefeed-h5api-bff"

# ── HARDCODED TOKEN ───────────────────────────────────────────────────────────
TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJ1aWQiOjcwNjg3NzgxMzIyMzY4MjE3NDQsImF0cCI6MywiZXh0IjoiMTc4NTc1NzQxNC"
    "IsImV4cCI6MTc5MzUzMzQxNCwiaWF0IjoxNzg1NzU3MTE0fQ"
    ".2cQ9H7rY1HOTmbjcXNHoULguJ-YQfZjjsZ3YYI4jdSk"
)
# ──────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "accept":          "application/json",
    "accept-language": "en-US,en;q=0.9",
    "authorization":   f"Bearer {TOKEN}",
    "content-type":    "application/json",
    "origin":          "https://netnaija.film",
    "referer":         "https://netnaija.film/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "x-client-info": '{"timezone":"Asia/Dhaka"}',
    "x-request-lang": "en",
}

SUBJECT_TYPE_ALL    = 0
SUBJECT_TYPE_MOVIE  = 1
SUBJECT_TYPE_SERIES = 2

DELAY_BETWEEN_PAGES = 1.0
MAX_PAGES           = 500
COMMIT_EVERY        = 2
MAX_FILE_SIZE       = 5 * 1024 * 1024  # 5 MB

OUTPUT_DIR    = "./output"
STATE_FILE    = os.path.join(OUTPUT_DIR, "page_state.json")
SEEN_IDS_FILE = os.path.join(OUTPUT_DIR, "seen_ids.json")

os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

session = requests.Session()
session.headers.update(HEADERS)

# ── DEDUPLICATION ─────────────────────────────────────────────────────────────

def get_item_id(item: dict) -> str:
    for key in ("id", "subjectId", "sid", "uid"):
        if key in item and item[key] is not None:
            return str(item[key])
    return str(hash(json.dumps(item, sort_keys=True, ensure_ascii=False)))


def load_seen_ids() -> set:
    if os.path.exists(SEEN_IDS_FILE):
        with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(seen: set):
    with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f)


def deduplicate(items: list, seen_ids: set) -> list:
    unique = []
    for item in items:
        item_id = get_item_id(item)
        if item_id not in seen_ids:
            seen_ids.add(item_id)
            unique.append(item)
    return unique


# ── GIT HELPERS ───────────────────────────────────────────────────────────────

def git_config():
    name  = os.environ.get("GIT_USER_NAME", "GitHub Action")
    email = os.environ.get("GIT_USER_EMAIL", "action@github.com")
    subprocess.run(["git", "config", "user.name", name], check=True)
    subprocess.run(["git", "config", "user.email", email], check=True)


def git_commit_and_push(message: str):
    log.info(f"📦  Git commit: {message}")
    subprocess.run(["git", "add", OUTPUT_DIR], check=False)
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    if result.returncode == 0:
        log.info("   (nothing to commit — skipping)")
        return
    subprocess.run(["git", "commit", "-m", message], check=True)

    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        remotes = subprocess.run(
            ["git", "remote", "-v"], capture_output=True, text=True, check=True
        )
        for line in remotes.stdout.splitlines():
            if line.startswith("origin") and "(push)" in line:
                url = line.split()[1]
                if (
                    url.startswith("https://")
                    and "x-access-token" not in url
                    and "@" not in url[8:]
                ):
                    auth_url = f"https://x-access-token:{token}@{url[8:]}"
                    subprocess.run(
                        ["git", "remote", "set-url", "origin", auth_url], check=True
                    )
                break
    subprocess.run(["git", "push"], check=True)
    log.info("🚀  Pushed to remote.")


# ── STATE HELPERS ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "next_page": 1,
        "mode": "trending",
        "per_page": 18,
        "file_index": 1,
    }


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── ROLLING JSON FILE APPENDER ────────────────────────────────────────────────
#
# File naming: {prefix}_{index:03d}.json  — STABLE, no timestamp.
# A single file grows until it hits 5 MB, then a new index is opened.
# The OLD file is never modified once a new index starts.

def _filepath(prefix: str, file_index: int) -> str:
    """Return the stable path for a given prefix + index (no timestamp)."""
    return os.path.join(OUTPUT_DIR, f"{prefix}_{file_index:03d}.json")


def _load_file(filepath: str) -> list:
    """Load JSON array from filepath; return [] if missing or corrupt."""
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _write_file(filepath: str, data: list):
    """Atomically write a JSON array to filepath."""
    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, filepath)  # atomic on POSIX; safe on Windows too


def append_items(prefix: str, file_index: int, new_items: list) -> int:
    """
    Append *unique* new_items to the current rolling file.
    If the resulting file would exceed MAX_FILE_SIZE:
      - keep the old file intact
      - open the next index and write new_items there
      - if new_items alone still exceed 5 MB, chunk across additional indices
    Returns the (possibly incremented) file_index.
    """
    if not new_items:
        return file_index

    filepath  = _filepath(prefix, file_index)
    existing  = _load_file(filepath)
    combined  = existing + new_items

    _write_file(filepath, combined)
    size = os.path.getsize(filepath)

    if size <= MAX_FILE_SIZE:
        log.info(
            f"💾  Appended {len(new_items)} items → {filepath} "
            f"({size / 1024 / 1024:.2f} MB, {len(combined)} total)"
        )
        return file_index

    # ── File exceeded 5 MB ────────────────────────────────────────────────────
    if existing:
        # Restore old file to its previous state; put new_items in next file(s).
        _write_file(filepath, existing)
        log.info(
            f"💾  {filepath} full "
            f"({os.path.getsize(filepath) / 1024 / 1024:.2f} MB) — rolling over."
        )
        file_index += 1
        # Fall through to chunk new_items starting at the new index.
        items_to_write = new_items
    else:
        # Even the very first batch is > 5 MB — we must chunk it.
        os.remove(filepath)
        items_to_write = combined

    # Write items_to_write across one or more new files.
    while items_to_write:
        fp    = _filepath(prefix, file_index)
        prev  = _load_file(fp)          # may already have data if resuming mid-chunk

        # Estimate how many items fit in the remaining space.
        if prev:
            # Pre-fill current file before estimating.
            test  = prev + items_to_write
            _write_file(fp, test)
            sz    = os.path.getsize(fp)
            if sz <= MAX_FILE_SIZE:
                log.info(
                    f"💾  {fp} ({sz / 1024 / 1024:.2f} MB, "
                    f"{len(test)} items)"
                )
                items_to_write = []
                break
            # Overflowed — figure out the split point.
            bytes_per_item = sz / len(test)
            prev_size      = os.path.getsize(fp) if os.path.exists(fp) else 0
            # Revert to prev content; calculate how many new items fit.
            _write_file(fp, prev)
            prev_size  = os.path.getsize(fp)
            space_left = MAX_FILE_SIZE - prev_size
            chunk_size = max(1, int(space_left / bytes_per_item * 0.95))
            chunk      = items_to_write[:chunk_size]
            _write_file(fp, prev + chunk)
            log.info(
                f"💾  {fp} ({os.path.getsize(fp) / 1024 / 1024:.2f} MB, "
                f"{len(prev + chunk)} items)"
            )
            items_to_write = items_to_write[chunk_size:]
            file_index    += 1
        else:
            # Empty new file — estimate from items_to_write size alone.
            _write_file(fp, items_to_write)
            sz = os.path.getsize(fp)
            if sz <= MAX_FILE_SIZE:
                log.info(
                    f"💾  {fp} ({sz / 1024 / 1024:.2f} MB, "
                    f"{len(items_to_write)} items)"
                )
                items_to_write = []
                break
            bytes_per_item = sz / len(items_to_write)
            chunk_size     = max(1, int(MAX_FILE_SIZE / bytes_per_item * 0.95))
            chunk          = items_to_write[:chunk_size]
            _write_file(fp, chunk)
            log.info(
                f"💾  {fp} ({os.path.getsize(fp) / 1024 / 1024:.2f} MB, "
                f"{len(chunk)} items)"
            )
            items_to_write = items_to_write[chunk_size:]
            file_index    += 1

    return file_index


# ── API HELPERS ───────────────────────────────────────────────────────────────

def _get(path: str, params: dict = None) -> dict:
    url  = f"{BASE_URL}{path}"
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"API error {data.get('code')}: {data.get('message')}")
    return data["data"]


def _post(path: str, body: dict) -> dict:
    url  = f"{BASE_URL}{path}"
    resp = session.post(url, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"API error {data.get('code')}: {data.get('message')}")
    return data["data"]


def fetch_trending_page(page: int = 1, per_page: int = 18, uid: str = "") -> dict:
    params = {"uid": uid, "page": page, "perPage": per_page}
    log.info(f"Fetching /subject/trending page={page} perPage={per_page}")
    return _get("/subject/trending", params=params)


def fetch_search_page(
    keyword: str,
    page: int = 1,
    per_page: int = 24,
    subject_type: int = SUBJECT_TYPE_ALL,
) -> dict:
    body = {
        "keyword":     keyword,
        "page":        str(page),
        "perPage":     per_page,
        "subjectType": subject_type,
    }
    log.info(f"Search '{keyword}' page={page} type={subject_type}")
    return _post("/subject/search", body)


# ── CORE SCRAPER ──────────────────────────────────────────────────────────────

def scrape_trending(
    start_page: int = 1,
    per_page: int = 18,
    file_index: int = 1,
) -> dict:
    all_items  = []
    dup_count  = 0
    stop_reason = ""
    seen_ids   = load_seen_ids()
    prefix     = "trending"

    state = {
        "next_page":  start_page,
        "mode":       "trending",
        "per_page":   per_page,
        "file_index": file_index,
    }

    for page in range(start_page, MAX_PAGES + 1):
        try:
            data = fetch_trending_page(page=page, per_page=per_page)
        except Exception as exc:
            log.error(f"Error on page {page}: {exc}")
            stop_reason = f"exception on page {page}"
            break

        pager    = data.get("pager", {})
        subjects = data.get("subjectList", [])

        log.info(
            f"  page={pager.get('page')} total={pager.get('totalCount')} "
            f"items={len(subjects)} hasMore={pager.get('hasMore')}"
        )

        if not subjects:
            stop_reason = f"no items on page {page}"
            log.info(f"🛑  Stopping: {stop_reason}")
            break

        unique     = deduplicate(subjects, seen_ids)
        dup_count += len(subjects) - len(unique)

        if not unique:
            log.info(
                f"  ⚠️  All {len(subjects)} items on page {page} were duplicates — continuing..."
            )
        else:
            all_items.extend(unique)
            file_index = append_items(prefix, file_index, unique)

        state["next_page"]  = page + 1
        state["file_index"] = file_index
        save_state(state)
        save_seen_ids(seen_ids)

        if page % COMMIT_EVERY == 0:
            git_commit_and_push(
                f"feat(scraper): trending pages {page - COMMIT_EVERY + 1}–{page} "
                f"({len(all_items)} unique, {dup_count} dups)"
            )

        if not pager.get("hasMore"):
            stop_reason = f"hasMore=False on page {page}"
            log.info(f"🛑  Stopping: {stop_reason}")
            break

        time.sleep(DELAY_BETWEEN_PAGES)

    # Final commit for any leftover pages.
    if stop_reason and (state["next_page"] - 1) % COMMIT_EVERY != 0:
        git_commit_and_push(
            f"feat(scraper): trending final batch ending page {state['next_page'] - 1}"
        )

    state.update(
        stop_reason=stop_reason,
        total_items=len(all_items),
        duplicates_skipped=dup_count,
    )
    save_state(state)
    save_seen_ids(seen_ids)
    return state


def scrape_search(
    keyword: str,
    start_page: int = 1,
    per_page: int = 24,
    subject_type: int = SUBJECT_TYPE_ALL,
    file_index: int = 1,
) -> dict:
    all_items   = []
    dup_count   = 0
    stop_reason = ""
    seen_ids    = load_seen_ids()
    prefix      = f"search_{keyword.replace(' ', '_')[:30]}"

    state = {
        "next_page":    start_page,
        "mode":         "search",
        "keyword":      keyword,
        "per_page":     per_page,
        "subject_type": subject_type,
        "file_index":   file_index,
    }

    for page in range(start_page, MAX_PAGES + 1):
        try:
            data = fetch_search_page(
                keyword=keyword,
                page=page,
                per_page=per_page,
                subject_type=subject_type,
            )
        except Exception as exc:
            log.error(f"Error on page {page}: {exc}")
            stop_reason = f"exception on page {page}"
            break

        pager = data.get("pager", {})
        items = data.get("items", [])

        log.info(
            f"  page={pager.get('page')} total={pager.get('totalCount')} "
            f"items={len(items)} hasMore={pager.get('hasMore')}"
        )

        if not items:
            stop_reason = f"no items on page {page}"
            log.info(f"🛑  Stopping: {stop_reason}")
            break

        unique     = deduplicate(items, seen_ids)
        dup_count += len(items) - len(unique)

        if not unique:
            log.info(
                f"  ⚠️  All {len(items)} items on page {page} were duplicates — continuing..."
            )
        else:
            all_items.extend(unique)
            file_index = append_items(prefix, file_index, unique)

        state["next_page"]  = page + 1
        state["file_index"] = file_index
        save_state(state)
        save_seen_ids(seen_ids)

        if page % COMMIT_EVERY == 0:
            git_commit_and_push(
                f"feat(scraper): search '{keyword}' pages {page - COMMIT_EVERY + 1}–{page} "
                f"({len(all_items)} unique, {dup_count} dups)"
            )

        if not pager.get("hasMore"):
            stop_reason = f"hasMore=False on page {page}"
            log.info(f"🛑  Stopping: {stop_reason}")
            break

        time.sleep(DELAY_BETWEEN_PAGES)

    if stop_reason and (state["next_page"] - 1) % COMMIT_EVERY != 0:
        git_commit_and_push(
            f"feat(scraper): search '{keyword}' final batch ending page {state['next_page'] - 1}"
        )

    state.update(
        stop_reason=stop_reason,
        total_items=len(all_items),
        duplicates_skipped=dup_count,
    )
    save_state(state)
    save_seen_ids(seen_ids)
    return state


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Netnaija API scraper — GitHub Action edition"
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("trending", help="Scrape ALL trending titles")
    p_search = sub.add_parser("search", help="Scrape ALL search results")
    p_search.add_argument("keyword")
    p_search.add_argument("--per-page", type=int, default=24)
    p_search.add_argument(
        "--type", type=int, default=0, dest="subject_type",
        help="0=all  1=movies  2=series",
    )

    args = parser.parse_args()
    git_config()

    state      = load_state()
    start_page = state.get("next_page", 1)
    file_index = state.get("file_index", 1)

    if args.cmd == "trending":
        log.info(
            f"=== Starting trending scrape from page {start_page}, "
            f"file index {file_index} ==="
        )
        final = scrape_trending(start_page=start_page, file_index=file_index)

    elif args.cmd == "search":
        log.info(
            f"=== Starting search '{args.keyword}' from page {start_page}, "
            f"file index {file_index} ==="
        )
        final = scrape_search(
            keyword=args.keyword,
            start_page=start_page,
            per_page=args.per_page,
            subject_type=args.subject_type,
            file_index=file_index,
        )

    else:
        # Default: trending
        log.info(
            f"=== Starting trending scrape from page {start_page}, "
            f"file index {file_index} ==="
        )
        final = scrape_trending(start_page=start_page, file_index=file_index)

    log.info(
        f"✅  Done. Total unique: {final['total_items']}, "
        f"dups skipped: {final.get('duplicates_skipped', 0)}, "
        f"reason: {final['stop_reason']}"
    )


if __name__ == "__main__":
    main()
