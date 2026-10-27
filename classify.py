#!/usr/bin/env python3
"""paperless-ai-classify: classify Paperless-ngx documents with a local LLM (Ollama).

For every document without a document type (newest first) the model gets the text and must
answer with a JSON object constrained by a schema:
- type:          ONLY one of the document types that already exist in Paperless (or "none");
- title:         a short title in DOC_LANGUAGE, applied only if the current title looks generated
                 (file name, "Fwd:", "Scan_0012", all caps...);
- correspondent: ONLY one of the existing correspondents, and only if its name appears in the
                 text (small models like to invent them).
Every changed document gets the TAG_REVIEW tag; every processed one (even with no result) gets
TAG_SEEN, so it is not processed again. Paperless keeps the history of changes, so everything can
be undone. The log has only document ids and changed fields, never content or titles.

Runs once and exits (cron, systemd timer, `docker run --rm`), or every day at RUN_AT when set.
Stops at STOP_AT (local time) or after MAX_DOCS documents and continues the next run.

Usage: classify.py [--dry-run] [--max N] [--ids 1,2,3]
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_bool(name: str, default: bool = True) -> bool:
    return env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


PAPERLESS_URL = env("PAPERLESS_URL", "http://localhost:8000").rstrip("/")
API = PAPERLESS_URL if PAPERLESS_URL.endswith("/api") else PAPERLESS_URL + "/api"
TOKEN = env("PAPERLESS_TOKEN")
OLLAMA_URL = env("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODEL = env("OLLAMA_MODEL", "qwen3:4b")
DOC_LANGUAGE = env("DOC_LANGUAGE", "English")
TAG_REVIEW = env("TAG_REVIEW", "AI to review")
TAG_SEEN = env("TAG_SEEN", "AI seen")
SET_TYPE = env_bool("SET_TYPE")
SET_TITLE = env_bool("SET_TITLE")
SET_CORRESPONDENT = env_bool("SET_CORRESPONDENT")
MAX_DOCS = int(env("MAX_DOCS", "400"))
MAX_CHARS = int(env("MAX_CHARS", "3000"))
NUM_CTX = int(env("NUM_CTX", "4096"))
STOP_AT = env("STOP_AT")  # "HH:MM" or empty
RUN_AT = env("RUN_AT")  # "HH:MM" to stay running and classify every day at that time

NONE = "none"
GENERATED_TITLE = [
    re.compile(r"(?i)^(fwd?|re|aw|wg|tr|i|r)\s*:"),
    re.compile(
        r"(?i)^(scan|scansione|img|image|doc|document|file|pdf|untitled|senza titolo|sans titre|unbenannt)[\s_\-]*\d*"
    ),
]


def looks_generated(title: str) -> bool:
    """True for titles that are file names, mail subjects or scanner defaults rather than real titles."""
    t = (title or "").strip()
    return (
        not t
        or any(p.match(t) for p in GENERATED_TITLE)
        or (" " not in t and re.search(r"[_\d]", t) is not None)
        or (t.isupper() and len(t) > 25)
    )


def schema(types: list, correspondents: list) -> dict:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "type": {"type": "string", "enum": sorted(types) + [NONE]},
            "correspondent": {
                "type": "string",
                "enum": sorted(correspondents) + [NONE],
            },
        },
        "required": ["title", "type", "correspondent"],
    }


def prompt(types: list, title: str, text: str) -> str:
    return (
        f"Classify this document (probably written in {DOC_LANGUAGE}). Answer in JSON.\n"
        f"title: a short descriptive title IN {DOC_LANGUAGE.upper()} (max 80 characters).\n"
        f"type: the document type, one of: {', '.join(sorted(types))}. If none fits well: '{NONE}'.\n"
        "correspondent: the company or organisation that issued the document, ONLY if its name appears in "
        f"the text; otherwise '{NONE}'.\n\nCurrent title: {title}\n\nText:\n{text}"
    )


def decide(doc: dict, answer: dict, types: dict, correspondents: dict, text: str) -> tuple[dict, list]:
    """Turns the model's answer into a Paperless PATCH (without tags) and the list of changed fields."""
    patch, changes = {}, []
    if SET_TYPE and not doc.get("document_type") and answer.get("type") in types:
        patch["document_type"] = types[answer["type"]]
        changes.append("type=" + answer["type"])
    name = answer.get("correspondent")
    words = re.findall(r"[^\W\d_]{4,}", name or "")
    haystack = (text + " " + doc.get("title", "")).lower()
    if (
        SET_CORRESPONDENT
        and not doc.get("correspondent")
        and name in correspondents
        and words
        and any(w.lower() in haystack for w in words)
    ):
        patch["correspondent"] = correspondents[name]
        changes.append("correspondent=" + name)
    # small models sometimes continue the title on more lines
    new_title = (answer.get("title") or "").strip().split("\n")[0][:120]
    if SET_TITLE and new_title and looks_generated(doc.get("title", "")) and new_title != doc.get("title"):
        patch["title"] = new_title
        changes.append("title")
    return patch, changes


def local_url(next_url: str) -> str:
    """Paperless returns `next` with its public URL: keep only the path and use our API base."""
    return API + next_url.split("/api", 1)[1]


# ---------- HTTP ----------


def api(path: str, method: str = "GET", data: dict | None = None, timeout: int = 60):
    url = path if path.startswith("http") else API + path
    body = json.dumps(data).encode() if data is not None else None
    headers = {
        "Authorization": "Token " + TOKEN,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp) if resp.status != 204 else None


def all_pages(path: str) -> list:
    out = []
    while path:
        page = api(path)
        out += page["results"]
        path = local_url(page["next"]) if page.get("next") else None
    return out


def tag_id(name: str, dry: bool) -> int:
    for tag in all_pages("/tags/?page_size=200&name__iexact=" + urllib.parse.quote(name)):
        return tag["id"]
    if dry:
        return -1
    return api("/tags/", "POST", {"name": name, "matching_algorithm": 0})["id"]


def ask(types: dict, correspondents: dict, doc: dict, text: str) -> dict:
    body = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "format": schema(list(types), list(correspondents)),
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
        "messages": [{"role": "user", "content": prompt(list(types), doc.get("title", ""), text)}],
    }
    req = urllib.request.Request(
        OLLAMA_URL + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(json.load(resp)["message"]["content"])


# ---------- run ----------


def stop_time(now: dt.datetime) -> dt.datetime | None:
    if not STOP_AT:
        return None
    stop = dt.datetime.combine(now.date(), dt.time.fromisoformat(STOP_AT))
    return stop if stop > now else stop + dt.timedelta(days=1)


def run(dry: bool = False, max_docs: int = MAX_DOCS, ids: list | None = None) -> int:
    if not TOKEN:
        print("PAPERLESS_TOKEN is not set", file=sys.stderr)
        return 2
    stop = stop_time(dt.datetime.now())
    types = {t["name"]: t["id"] for t in all_pages("/document_types/?page_size=200")}
    correspondents = {c["name"]: c["id"] for c in all_pages("/correspondents/?page_size=200")}
    review_id, seen_id = tag_id(TAG_REVIEW, dry), tag_id(TAG_SEEN, dry)
    if ids:
        queue = ids
    else:
        query = f"/documents/?page_size={max_docs}&ordering=-added&fields=id&document_type__isnull=1"
        if seen_id > 0:
            query += f"&tags__id__none={seen_id}"
        queue = [d["id"] for d in api(query)["results"]][:max_docs]
    stats = {"seen": 0, "type": 0, "title": 0, "correspondent": 0, "errors": 0}
    started = time.time()
    for i, doc_id in enumerate(queue):
        if (stop and dt.datetime.now() >= stop) or i >= max_docs:
            print("stop: time window over or limit reached")
            break
        try:
            doc = api(f"/documents/{doc_id}/?fields=id,title,content,tags,document_type,correspondent")
            text = (doc.get("content") or "")[:MAX_CHARS]
            t0 = time.time()
            answer = ask(types, correspondents, doc, text) if text.strip() else {}
            patch, changes = decide(doc, answer, types, correspondents, text)
            for field in ("type", "title", "correspondent"):
                stats[field] += any(c == field or c.startswith(field + "=") for c in changes)
            patch["tags"] = sorted(set(doc["tags"]) | {seen_id} | ({review_id} if changes else set()))
            stats["seen"] += 1
            print(
                f"doc {doc_id}: {', '.join(changes) or 'no change'} ({time.time() - t0:.0f}s)",
                flush=True,
            )
            if not dry:
                api(f"/documents/{doc_id}/", "PATCH", patch)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            stats["errors"] += 1
            print(f"doc {doc_id}: ERROR {type(e).__name__}", flush=True)
            if stats["errors"] >= 5 and stats["errors"] > stats["seen"]:
                print("too many errors, stopping")
                break
    stats["minutes"] = round((time.time() - started) / 60)
    print("SUMMARY " + json.dumps(stats), flush=True)
    return 1 if stats["errors"] and not stats["seen"] else 0


def seconds_until(hhmm: str, now: dt.datetime) -> float:
    target = dt.datetime.combine(now.date(), dt.time.fromisoformat(hhmm))
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


def main(argv: list) -> int:
    dry = "--dry-run" in argv
    max_docs = int(argv[argv.index("--max") + 1]) if "--max" in argv else MAX_DOCS
    ids = [int(x) for x in argv[argv.index("--ids") + 1].split(",")] if "--ids" in argv else None
    if not RUN_AT or ids:
        return run(dry, max_docs, ids)
    print(f"scheduler: classifying every day at {RUN_AT}", flush=True)
    while True:
        time.sleep(seconds_until(RUN_AT, dt.datetime.now()))
        try:
            run(dry, max_docs)
        except Exception as e:  # keep the scheduler alive (Paperless or Ollama down tonight)
            print(f"run failed: {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
