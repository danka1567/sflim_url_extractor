#!/usr/bin/env python3
"""
Netnaija API Scraper — GitHub Action Edition
============================================
- Resumes from last saved state (page number stored in page_state.json)
- Commits & pushes after every 2 pages using built-in GITHUB_TOKEN
- Stops automatically when no more data (hasMore=False or empty items)
- Auto-splits JSON files when they exceed 5 MB
- Deduplicates items globally (no duplicates across any output file)
- Saves incremental JSON files to ./output/

Environment variables (auto-set by GitHub Actions):
  GITHUB_TOKEN      — Built-in GitHub token for repo write access
  GIT_USER_NAME     — Git commit author name  (default: "GitHub Action")
  GIT_USER_EMAIL    — Git commit author email (default: "action@github.com")
"""

import json
import os
import subprocess
import sys
import time
import logging
from datetime import datetime
from typing import Generator, Optional, List

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

OUTPUT_DIR = "./output"
STATE_FILE = os.path.join(OUTPUT_DIR, "page_state.json")
SEEN_IDS_FILE = os.path.join(OUTPUT_DIR, "seen_ids.json")

os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

session = requests.Session()
session.headers.update(HEADERS)

# ── DEDUPLICATION HELPERS ─────────────────────────────────────────────────────

def get_item_id(item: dict) -> str:
    """Extract a unique identifier from an item for deduplication."""
    for key in ("id", "subjectId", "sid", "uid"):
        if key in item and item[key] is not None:
            return str(item[key])
    # Fallback: hash the entire item
    return str(hash(json.dumps(item, sort_keys=True, ensure_ascii=False)))


def load_seen_ids() -> set:
    """Load previously seen item IDs from disk."""
    if os.path.exists(SEEN_IDS_FILE):
        with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(seen: set):
    """Persist seen item IDs to disk."""
    with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f)


# ── GIT HELPERS ───────────────────────────────────────────────────────────────

def git_config():
    """Set git user name/email from env."""
    name  = os.environ.get("GIT_USER_NAME", "GitHub Action")
    email = os.environ.get("GIT_USER_EMAIL", "action@github.com")
    subprocess.run(["git", "config", "user.name", name], check=True)
    subprocess.run(["git", "config", "user.email", email], check=True)


def git_commit_and_push(message: str):
    """Stage output/, commit, and push using built-in GITHUB_TOKEN."""
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
                if url.startswith("https://") and "x-access-token" not in url and "@" not in url[8:]:
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
    return {"next_page": 1, "mode": "trending", "per_page": 18}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── JSON OUTPUT HELPERS (with auto-split & dedup) ────────────────────────────

def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def save_json_split(data: list, filename_base: str, max_size: int = MAX_FILE_SIZE) -> List[str]:
    """
    Save data as JSON, auto-splitting into multiple files if size exceeds max_size.
    Returns list of saved file paths.
    """
    if not data:
        return []

    filepath = os.path.join(OUTPUT_DIR, filename_base)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    size = os.path.getsize(filepath)
    if size <= max_size:
        log.info(f"💾  Saved → {filepath} ({size/1024/1024:.2f} MB)")
        return [filepath]

    # Too big — split into chunks
    os.remove(filepath)
    files = []
    total = len(data)
    avg_size = size / total
    items_per_chunk = max(1, int(max_size / avg_size * 0.9))

    for i in range(0, total, items_per_chunk):
        chunk = data[i:i + items_per_chunk]
        if i == 0:
            chunk_path = filepath
        else:
            name, ext = os.path.splitext(filename_base)
            part_num = i // items_per_chunk + 1
            chunk_path = os.path.join(OUTPUT_DIR, f"{name}_part{part_num}{ext}")

        with open(chunk_path, "w", encoding="utf-8") as f:
            json.dump(chunk, f, indent=2, ensure_ascii=False)

        chunk_size = os.path.getsize(chunk_path)
        files.append(chunk_path)
        log.info(f"💾  Saved → {chunk_path} ({chunk_size/1024/1024:.2f} MB)")

    return files


def deduplicate_items(items: list, seen_ids: set) -> list:
    """Filter out items whose IDs are already in seen_ids. Returns only new unique items."""
    unique = []
    for item in items:
        item_id = get_item_id(item)
        if item_id in seen_ids:
            log.debug(f"  ⚠️  Duplicate skipped: {item.get('title', item_id)}")
            continue
        seen_ids.add(item_id)
        unique.append(item)
    return unique


# ── API HELPERS ───────────────────────────────────────────────────────────────

def _get(path: str, params: dict = None) -> dict:
    url = f"{BASE_URL}{path}"
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"API error {data.get('code')}: {data.get('message')}")
    return data["data"]


def _post(path: str, body: dict) -> dict:
    url = f"{BASE_URL}{path}"
    resp = session.post(url, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"API error {data.get('code')}: {data.get('message')}")
    return data["data"]


# ── ENDPOINT WRAPPERS ─────────────────────────────────────────────────────────

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

def scrape_trending(start_page: int = 1, per_page: int = 18) -> dict:
    all_items = []
    state = {"next_page": start_page, "mode": "trending", "per_page": per_page}
    stop_reason = ""
    seen_ids = load_seen_ids()
    dup_count = 0

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

        # Deduplicate
        unique_subjects = deduplicate_items(subjects, seen_ids)
        dup_count += len(subjects) - len(unique_subjects)
        if dup_count > 0:
            log.info(f"  🔄  Duplicates skipped so far: {dup_count}")

        if not unique_subjects:
            log.info(f"  ⚠️  All items on page {page} were duplicates, continuing...")
            state["next_page"] = page + 1
            save_state(state)
            save_seen_ids(seen_ids)
            if not pager.get("hasMore"):
                stop_reason = f"hasMore=False on page {page}"
                log.info(f"🛑  Stopping: {stop_reason}")
                break
            time.sleep(DELAY_BETWEEN_PAGES)
            continue

        all_items.extend(unique_subjects)
        state["next_page"] = page + 1
        save_state(state)
        save_seen_ids(seen_ids)

        # Save incremental batch (auto-split if > 5 MB)
        batch_file = f"trending_page_{page:04d}_{_timestamp()}.json"
        save_json_split(unique_subjects, batch_file)

        # Commit & push every COMMIT_EVERY pages
        if page % COMMIT_EVERY == 0:
            git_commit_and_push(
                f"feat(scraper): trending pages {page - COMMIT_EVERY + 1}–{page} "
                f"({len(all_items)} unique items so far, {dup_count} dups skipped)"
            )

        if not pager.get("hasMore"):
            stop_reason = f"hasMore=False on page {page}"
            log.info(f"🛑  Stopping: {stop_reason}")
            break

        time.sleep(DELAY_BETWEEN_PAGES)

    # Final commit if there are uncommitted pages
    if (state["next_page"] - 1) % COMMIT_EVERY != 0 and stop_reason:
        git_commit_and_push(
            f"feat(scraper): trending final batch ending page {state['next_page'] - 1}"
        )

    # Save full cumulative dump (auto-split if > 5 MB)
    if all_items:
        save_json_split(all_items, f"trending_all_{_timestamp()}.json")
        git_commit_and_push("feat(scraper): final cumulative trending dump")

    state["stop_reason"] = stop_reason
    state["total_items"] = len(all_items)
    state["duplicates_skipped"] = dup_count
    save_state(state)
    save_seen_ids(seen_ids)
    return state


def scrape_search(
    keyword: str,
    start_page: int = 1,
    per_page: int = 24,
    subject_type: int = SUBJECT_TYPE_ALL,
) -> dict:
    all_items = []
    state = {
        "next_page": start_page,
        "mode": "search",
        "keyword": keyword,
        "per_page": per_page,
        "subject_type": subject_type,
    }
    stop_reason = ""
    seen_ids = load_seen_ids()
    dup_count = 0

    for page in range(start_page, MAX_PAGES + 1):
        try:
            data = fetch_search_page(
                keyword=keyword, page=page,
                per_page=per_page, subject_type=subject_type,
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

        # Deduplicate
        unique_items = deduplicate_items(items, seen_ids)
        dup_count += len(items) - len(unique_items)
        if dup_count > 0:
            log.info(f"  🔄  Duplicates skipped so far: {dup_count}")

        if not unique_items:
            log.info(f"  ⚠️  All items on page {page} were duplicates, continuing...")
            state["next_page"] = page + 1
            save_state(state)
            save_seen_ids(seen_ids)
            if not pager.get("hasMore"):
                stop_reason = f"hasMore=False on page {page}"
                log.info(f"🛑  Stopping: {stop_reason}")
                break
            time.sleep(DELAY_BETWEEN_PAGES)
            continue

        all_items.extend(unique_items)
        state["next_page"] = page + 1
        save_state(state)
        save_seen_ids(seen_ids)

        batch_file = f"search_{keyword.replace(' ', '_')[:30]}_page_{page:04d}_{_timestamp()}.json"
        save_json_split(unique_items, batch_file)

        if page % COMMIT_EVERY == 0:
            git_commit_and_push(
                f"feat(scraper): search '{keyword}' pages {page - COMMIT_EVERY + 1}–{page} "
                f"({len(all_items)} unique items so far, {dup_count} dups skipped)"
            )

        if not pager.get("hasMore"):
            stop_reason = f"hasMore=False on page {page}"
            log.info(f"🛑  Stopping: {stop_reason}")
            break

        time.sleep(DELAY_BETWEEN_PAGES)

    if (state["next_page"] - 1) % COMMIT_EVERY != 0 and stop_reason:
        git_commit_and_push(
            f"feat(scraper): search final batch ending page {state['next_page'] - 1}"
        )

    if all_items:
        save_json_split(all_items, f"search_{keyword.replace(' ', '_')[:30]}_all_{_timestamp()}.json")
        git_commit_and_push("feat(scraper): final cumulative search dump")

    state["stop_reason"] = stop_reason
    state["total_items"] = len(all_items)
    state["duplicates_skipped"] = dup_count
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

    state = load_state()
    start_page = state.get("next_page", 1)

    if args.cmd == "trending":
        log.info(f"=== Starting trending scrape from page {start_page} ===")
        final = scrape_trending(start_page=start_page)
        log.info(
            f"✅  Done. Total unique: {final['total_items']}, "
            f"dups skipped: {final.get('duplicates_skipped', 0)}, "
            f"reason: {final['stop_reason']}"
        )

    elif args.cmd == "search":
        log.info(f"=== Starting search '{args.keyword}' from page {start_page} ===")
        final = scrape_search(
            keyword=args.keyword,
            start_page=start_page,
            per_page=args.per_page,
            subject_type=args.subject_type,
        )
        log.info(
            f"✅  Done. Total unique: {final['total_items']}, "
            f"dups skipped: {final.get('duplicates_skipped', 0)}, "
            f"reason: {final['stop_reason']}"
        )

    else:
        log.info(f"=== Starting trending scrape from page {start_page} ===")
        final = scrape_trending(start_page=start_page)
        log.info(
            f"✅  Done. Total unique: {final['total_items']}, "
            f"dups skipped: {final.get('duplicates_skipped', 0)}, "
            f"reason: {final['stop_reason']}"
        )


if __name__ == "__main__":
    main()
