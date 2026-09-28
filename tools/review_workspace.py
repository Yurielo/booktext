#!/usr/bin/env python3
"""Loopback-only, lazy local workspace for BookText review decisions."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import threading
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tools import review_decisions


LOOPBACK_HOST = "127.0.0.1"
PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}


class ReviewWorkspace:
    """Read only the requested item/page; decisions stay in #33's artifact."""

    def __init__(self, output_dir):
        self.root = Path(output_dir).expanduser().resolve()
        self.priority_path = self.root / "review_priority.json"
        self.book_path = self.root / "book.json"
        self.decisions_path = self.root / "review_decisions.json"
        if not self.root.is_dir() or not self.priority_path.is_file() or not self.book_path.is_file():
            raise ValueError("review workspace requires book.json and review_priority.json in one BookText output directory")
        self.lock = threading.RLock()
        self.refresh()

    def refresh(self):
        with self.lock:
            self.priority = review_decisions.load_json(self.priority_path)
            self.book = review_decisions.load_json(self.book_path)
            self.items = review_decisions._priority_index(self.priority)
            self.pages = review_decisions._page_index(self.book)
            self.decisions = review_decisions.load_json(self.decisions_path) if self.decisions_path.exists() else review_decisions.empty_decisions(self.priority_path, self.book_path)
            self.statuses = self._statuses()

    def _statuses(self):
        entries = self.decisions.get("decisions", [])
        root_matches = (
            self.decisions.get("priority_source", {}).get("sha256") == review_decisions.sha256_file(self.priority_path)
            and self.decisions.get("book_source", {}).get("sha256") == review_decisions.sha256_file(self.book_path)
        )
        counts = Counter(entry.get("item_id") for entry in entries if isinstance(entry, dict))
        result = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item_id = entry.get("item_id")
            if counts[item_id] > 1:
                result[item_id] = {"status": "conflict", "reason": "duplicate_decision_for_item", "decision": entry}
                continue
            if not root_matches:
                result[item_id] = {"status": "stale", "reason": "source_output_identity_changed", "decision": entry}
                continue
            status, item_or_reason, _ = review_decisions.validate_decision(entry, self.items, self.pages)
            if status in {"invalid", "stale", "conflict"}:
                result[item_id] = {"status": status, "reason": item_or_reason, "decision": entry}
            elif status == "pending_change":
                result[item_id] = {"status": "resolved_changed", "decision": entry}
            else:
                result[item_id] = {"status": status, "decision": entry}
        return result

    def status_for(self, item_id):
        return self.statuses.get(item_id, {"status": "unresolved"})

    def filtered_ids(self, filters):
        priority = filters.get("priority")
        status = filters.get("status")
        page = filters.get("page")
        if priority and priority not in PRIORITY_ORDER:
            raise ValueError("invalid priority filter")
        if status and status not in {"unresolved", "resolved_changed", "resolved_no_change", "deferred", "stale", "conflict", "invalid"}:
            raise ValueError("invalid status filter")
        try:
            page = int(page) if page else None
        except ValueError as error:
            raise ValueError("invalid page filter") from error
        values = []
        for item in self.items.values():
            item_status = self.status_for(item["item_id"])["status"]
            if priority and item["priority"] != priority:
                continue
            if status and item_status != status:
                continue
            if page and item["page"] != page:
                continue
            values.append(item)
        return [item["item_id"] for item in sorted(values, key=lambda item: (
            0 if self.status_for(item["item_id"])["status"] == "unresolved" else 1,
            PRIORITY_ORDER[item["priority"]], item["page"], item["item_id"],
        ))]

    def progress(self):
        states = Counter(self.status_for(item_id)["status"] for item_id in self.items)
        unresolved = [item for item in self.items.values() if self.status_for(item["item_id"])["status"] == "unresolved"]
        return {
            "total": len(self.items), "unresolved": states["unresolved"],
            "resolved_changed": states["resolved_changed"], "resolved_no_change": states["resolved_no_change"],
            "deferred": states["deferred"], "stale_conflict": states["stale"] + states["conflict"] + states["invalid"],
            "unresolved_high": sum(item["priority"] == "HIGH" for item in unresolved),
            "unresolved_medium": sum(item["priority"] == "MEDIUM" for item in unresolved),
        }

    def item_payload(self, item_id):
        item = self.items[item_id]
        page = self.pages.get(item["page"], {})
        return {
            "item_id": item_id, "page": item["page"], "priority": item["priority"],
            "item_type": item["item_type"], "reasons": item.get("reasons", []), "signals": item.get("signals", {}),
            "surya": item.get("surya", ""), "vision": item.get("vision", ""),
            "status": self.status_for(item_id), "image_available": self.page_image_path(item["page"]) is not None,
            "page_image_url": f"/asset/page/{item['page']}" if self.page_image_path(item["page"]) else None,
            "page_context": {"text_length": len(str(page.get("text", "")))},
        }

    def page_image_path(self, page_number):
        page = self.pages.get(page_number, {})
        relative = page.get("page_image") or page.get("image")
        if not isinstance(relative, str):
            return None
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def record(self, payload):
        action = payload.get("action")
        item_id = payload.get("item_id")
        if not isinstance(action, str) or not isinstance(item_id, str):
            raise ValueError("item_id and action are required")
        review_decisions.record_decision(
            decisions_path=self.decisions_path, priority_path=self.priority_path, book_path=self.book_path,
            item_id=item_id, action=action, replacement_text=payload.get("replacement_text"),
            note=payload.get("note"), replace=True,
        )
        self.refresh()
        return self.status_for(item_id)

    def build_final(self):
        final_text, final_markdown, report = review_decisions.build_final(
            self.book, self.priority, self.decisions, book_path=self.book_path,
            priority_path=self.priority_path, decisions_path=self.decisions_path, booktext_version="0.1.0",
        )
        review_decisions.write_final(self.root, final_text, final_markdown, report)
        return report


class WorkspaceHandler(BaseHTTPRequestHandler):
    workspace = None

    def log_message(self, _format, *_args):
        return

    def _json(self, value, status=HTTPStatus.OK):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)

    def _error(self, message, status=HTTPStatus.BAD_REQUEST):
        self._json({"error": message}, status)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            data = PAGE.encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if parsed.path == "/api/progress":
            return self._json(self.workspace.progress())
        if parsed.path == "/api/item":
            try:
                query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
                ids = self.workspace.filtered_ids(query)
                index = max(0, int(query.get("index", "0")))
                if not ids: return self._json({"total": 0, "progress": self.workspace.progress()})
                index = min(index, len(ids) - 1)
                return self._json({"total": len(ids), "index": index, "item": self.workspace.item_payload(ids[index]), "progress": self.workspace.progress()})
            except (ValueError, KeyError) as error:
                return self._error(str(error))
        if parsed.path.startswith("/asset/page/"):
            try:
                number = int(parsed.path.removeprefix("/asset/page/"))
                if str(number) != parsed.path.removeprefix("/asset/page/"): raise ValueError
                path = self.workspace.page_image_path(number)
                if path is None: return self._error("not found", HTTPStatus.NOT_FOUND)
                body = path.read_bytes(); content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(body)
            except ValueError:
                return self._error("not found", HTTPStatus.NOT_FOUND)
            return
        self._error("not found", HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 1024 * 1024: return self._error("request too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            if parsed.path == "/api/decision": return self._json({"status": self.workspace.record(payload), "progress": self.workspace.progress()})
            if parsed.path == "/api/build": return self._json(self.workspace.build_final())
            return self._error("not found", HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as error:
            return self._error(str(error))


PAGE = """<!doctype html><meta charset=utf-8><title>BookText review</title>
<style>body{font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem}button,select,input,textarea{font:inherit;margin:.2rem}pre{white-space:pre-wrap;background:#f5f5f5;padding:.8rem}img{max-width:100%;max-height:600px}#priority{font-weight:bold}</style>
<h1>BookText local review</h1><p>Surya is primary OCR; Vision is evidence, never automatic replacement.</p>
<div><select id=pfilter><option value=''>All priorities</option><option>HIGH</option><option>MEDIUM</option><option>LOW</option><option>INFO</option></select><select id=sfilter><option value=''>All statuses</option><option value=unresolved>Unresolved</option><option value=resolved_changed>Resolved changed</option><option value=resolved_no_change>Resolved no-change</option><option value=deferred>Deferred</option><option value=stale>Stale</option><option value=conflict>Conflict</option></select><input id=page placeholder=Page><button onclick='go(0)'>Filter</button></div>
<p id=progress></p><button onclick='go(-1)'>Previous</button><button onclick='go(1)'>Next</button><button onclick='nextUnresolved()'>Next unresolved</button><button onclick='build()'>Build final book</button><p id=message></p><section id=item></section>
<script>let n=0,total=0;const q=()=>new URLSearchParams({index:n,priority:pfilter.value,status:sfilter.value,page:page.value});function esc(x){return String(x??'')}function show(x){progress.textContent=Object.entries(x.progress).map(([k,v])=>k+': '+v).join(' | ');total=x.total||0;n=x.index||0;if(!x.item){item.textContent='No matching review items.';return}let i=x.item;item.replaceChildren();let h=document.createElement('h2');h.textContent=`${i.priority} · page ${i.page} · ${i.status.status}`;item.append(h);for(let [k,v] of [['Item ID',i.item_id],['Reasons',i.reasons.join(', ')],['Signals',JSON.stringify(i.signals)],['Surya',i.surya],['Vision alternative',i.vision||'—']]){let e=document.createElement('pre');e.textContent=k+'\n'+v;item.append(e)}if(i.page_image_url){let im=document.createElement('img');im.src=i.page_image_url;im.alt='Source page '+i.page;item.append(im)}for(let a of ['keep_surya','accept_vision','ignore','defer']){let b=document.createElement('button');b.textContent=a;b.disabled=a==='accept_vision'&&!i.vision;b.onclick=()=>decide(a);item.append(b)}let ta=document.createElement('textarea');ta.id='manual';ta.placeholder='Manual replacement';item.append(ta);let mb=document.createElement('button');mb.textContent='manual';mb.onclick=()=>decide('manual');item.append(mb)}async function go(d){n=Math.max(0,n+d);let r=await fetch('/api/item?'+q());show(await r.json())}async function nextUnresolved(){sfilter.value='unresolved';go(0)}async function decide(action){let body={item_id:document.querySelector('pre').textContent.split('\n')[1],action,replacement_text:manual.value};let r=await fetch('/api/decision',{method:'POST',body:JSON.stringify(body)});let x=await r.json();message.textContent=x.error||'Saved immediately.';go(0)}async function build(){let r=await fetch('/api/build',{method:'POST',body:'{}'});message.textContent=JSON.stringify(await r.json())}go(0)</script>"""


def create_server(output_dir, host=LOOPBACK_HOST, port=0):
    if host != LOOPBACK_HOST:
        raise ValueError("review workspace may bind only to 127.0.0.1")
    workspace = ReviewWorkspace(output_dir)
    handler = type("BoundWorkspaceHandler", (WorkspaceHandler,), {"workspace": workspace})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start BookText's loopback-only review workspace.")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    server = create_server(args.output_dir, port=args.port)
    print(f"BookText review workspace: http://{LOOPBACK_HOST}:{server.server_port}/")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
