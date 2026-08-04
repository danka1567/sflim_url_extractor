#!/usr/bin/env python3
"""
Netnaija API Scraper — GitHub Action Edition
============================================
- Resumes from last saved state (page number stored in page_state.json)
- Commits & pushes after every 2 pages using built-in GITHUB_TOKEN
- Stops automatically when no more data (hasMore=False or empty items)
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
from typing import Generator, Optional

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

DELAY_BETWEEN_PAGES = 1.0   # polite crawl delay
MAX_PAGES           = 500   # hard safety limit
COMMIT_EVERY        = 2     # commit & push every N pages

OUTPUT_DIR = "./output"
STATE_FILE = os.path.join(OUTPUT_DIR, "page_state.json")

os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

session = requests.Session()
session.headers.update(HEADERS)

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
    # Check if there is anything to commit
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    if result.returncode == 0:
        log.info("   (nothing to commit — skipping)")
        return
    subprocess.run(["git", "commit", "-m", message], check=True)

    # Use built-in GITHUB_TOKEN for push
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        remotes = subprocess.run(
            ["git", "remote", "-v"], capture_output=True, text=True, check=True
        )
        for line in remotes.stdout.splitlines():
            if line.startswith("origin") and "(push)" in line:
                url = line.split()[1]
                # Only modify if URL is plain HTTPS (no auth embedded yet)
                # If already has x-access-token or @ after https://, skip
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
    """Load resume state. Returns {'next_page': 1, 'mode': 'trending'} if missing."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"next_page": 1, "mode": "trending", "per_page": 18}


def save_state(state: dict):
    """Persist resume state."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── JSON OUTPUT HELPERS ───────────────────────────────────────────────────────

def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def save_json(data: object, filename: str) -> str:
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info(f"💾  Saved → {filepath}")
    return filepath


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
    """
    Scrape trending pages starting from start_page.
    Commits & pushes every COMMIT_EVERY pages.
    Stops when hasMore=False or items are empty.
    Returns final state dict.
    """
    all_items = []
    state = {"next_page": start_page, "mode": "trending", "per_page": per_page}
    stop_reason = ""

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

        all_items.extend(subjects)
        state["next_page"] = page + 1
        save_state(state)

        # Save incremental batch
        batch_file = f"trending_page_{page:04d}_{_timestamp()}.json"
        save_json(subjects, batch_file)

        # Commit & push every COMMIT_EVERY pages
        if page % COMMIT_EVERY == 0:
            git_commit_and_push(
                f"feat(scraper): trending pages {page - COMMIT_EVERY + 1}–{page} "
                f"({len(all_items)} total items so far)"
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

    # Save full cumulative dump
    if all_items:
        save_json(all_items, f"trending_all_{_timestamp()}.json")
        git_commit_and_push("feat(scraper): final cumulative trending dump")

    state["stop_reason"] = stop_reason
    state["total_items"] = len(all_items)
    save_state(state)
    return state


def scrape_search(
    keyword: str,
    start_page: int = 1,
    per_page: int = 24,
    subject_type: int = SUBJECT_TYPE_ALL,
) -> dict:
    """
    Scrape search results starting from start_page.
    Commits & pushes every COMMIT_EVERY pages.
    Stops when hasMore=False or items are empty.
    """
    all_items = []
    state = {
        "next_page": start_page,
        "mode": "search",
        "keyword": keyword,
        "per_page": per_page,
        "subject_type": subject_type,
    }
    stop_reason = ""

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

        all_items.extend(items)
        state["next_page"] = page + 1
        save_state(state)

        batch_file = f"search_{keyword.replace(' ', '_')[:30]}_page_{page:04d}_{_timestamp()}.json"
        save_json(items, batch_file)

        if page % COMMIT_EVERY == 0:
            git_commit_and_push(
                f"feat(scraper): search '{keyword}' pages {page - COMMIT_EVERY + 1}–{page} "
                f"({len(all_items)} total items so far)"
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
        save_json(all_items, f"search_{keyword.replace(' ', '_')[:30]}_all_{_timestamp()}.json")
        git_commit_and_push("feat(scraper): final cumulative search dump")

    state["stop_reason"] = stop_reason
    state["total_items"] = len(all_items)
    save_state(state)
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

    # Configure git once
    git_config()

    state = load_state()
    start_page = state.get("next_page", 1)

    if args.cmd == "trending":
        log.info(f"=== Starting trending scrape from page {start_page} ===")
        final = scrape_trending(start_page=start_page)
        log.info(f"✅  Done. Total items: {final['total_items']}. Reason: {final['stop_reason']}")

    elif args.cmd == "search":
        log.info(f"=== Starting search '{args.keyword}' from page {start_page} ===")
        final = scrape_search(
            keyword=args.keyword,
            start_page=start_page,
            per_page=args.per_page,
            subject_type=args.subject_type,
        )
        log.info(f"✅  Done. Total items: {final['total_items']}. Reason: {final['stop_reason']}")

    else:
        # Default: trending
        log.info(f"=== Starting trending scrape from page {start_page} ===")
        final = scrape_trending(start_page=start_page)
        log.info(f"✅  Done. Total items: {final['total_items']}. Reason: {final['stop_reason']}")


if __name__ == "__main__":
    main()
