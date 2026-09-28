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
from tools.review_clusters import build_clusters
from tools import review_batches


LOOPBACK_HOST = "127.0.0.1"
PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}


class ReviewWorkspace:
    """Read only the requested item/page; decisions stay in #33's artifact."""

    def __init__(self, output_dir):
        self.root = Path(output_dir).expanduser().resolve()
        self.priority_path = self.root / "review_priority.json"
        self.book_path = self.root / "book.json"
        self.decisions_path = self.root / "review_decisions.json"
        self.clusters_path = self.root / "review_clusters.json"
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
            stored_clusters = review_decisions.load_json(self.clusters_path).get("clusters", []) if self.clusters_path.exists() else []
            # Older #37 artifacts predate strict batch metadata; rebuild their
            # deterministic organizational view rather than weakening safety.
            self.clusters = stored_clusters if stored_clusters and all("batch_eligible" in cluster for cluster in stored_clusters) else build_clusters(self.priority)["clusters"]

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
        eligible_clusters = [cluster for cluster in self.clusters if cluster.get("batch_eligible")]
        eligible_unresolved = [item_id for cluster in eligible_clusters for item_id in cluster.get("eligible_member_ids", []) if self.status_for(item_id)["status"] == "unresolved"]
        batch_decisions = [entry for entry in self.decisions.get("decisions", []) if isinstance(entry, dict) and entry.get("batch_provenance")]
        return {
            "total": len(self.items), "unresolved": states["unresolved"],
            "resolved_changed": states["resolved_changed"], "resolved_no_change": states["resolved_no_change"],
            "deferred": states["deferred"], "stale_conflict": states["stale"] + states["conflict"] + states["invalid"],
            "unresolved_high": sum(item["priority"] == "HIGH" for item in unresolved),
            "unresolved_medium": sum(item["priority"] == "MEDIUM" for item in unresolved),
            "batch_eligible_clusters": len(eligible_clusters),
            "batch_eligible_unresolved_items": len(eligible_unresolved),
            "batch_potential_decision_units_saved": sum(max(0, sum(self.status_for(item_id)["status"] == "unresolved" for item_id in cluster["eligible_member_ids"]) - 1) for cluster in eligible_clusters),
            "confirmed_batch_operations": len({entry["batch_provenance"]["batch_id"] for entry in batch_decisions}),
            "items_resolved_via_batch": len(batch_decisions),
        }

    def filtered_clusters(self, filters):
        priority, category, status = filters.get("priority"), filters.get("category"), filters.get("status")
        values = []
        for cluster in self.clusters:
            member_states = [self.status_for(item_id)["status"] for item_id in cluster["member_item_ids"]]
            if priority and not cluster.get("priority_distribution", {}).get(priority): continue
            if category and cluster.get("category") != category: continue
            if status and status not in member_states: continue
            values.append(cluster)
        return sorted(values, key=lambda cluster: (
            0 if "unresolved" in [self.status_for(item_id)["status"] for item_id in cluster["member_item_ids"]] else 1,
            -cluster["member_count"], cluster["cluster_id"],
        ))

    def cluster_payload(self, cluster_id):
        cluster = next(cluster for cluster in self.clusters if cluster["cluster_id"] == cluster_id)
        members = [{"item_id": item_id, "page": self.items[item_id]["page"], "priority": self.items[item_id]["priority"], "status": self.status_for(item_id)["status"]} for item_id in cluster["member_item_ids"]]
        return {**cluster, "members": members, "decision_statistics": dict(Counter(member["status"] for member in members))}

    def item_payload(self, item_id):
        item = self.items[item_id]
        page = self.pages.get(item["page"], {})
        image_path = self.page_image_path(item["page"])
        image_reference = None
        if image_path is not None:
            # A native client receives a logical, root-relative reference only.
            # It must not infer or browse arbitrary local paths.
            image_reference = str(image_path.relative_to(self.root))
        signals = item.get("signals", {})
        # Prefer exact Vision word geometry. Vision uses a bottom-left origin,
        # whereas every BookText page asset (and native view) uses top-left.
        # Convert only for display; raw review.json and priority evidence remain
        # immutable. A Surya block is intentionally an *approximate* fallback,
        # not a claim that the disputed word was located.
        vision_bbox = signals.get("vision_bbox")
        if vision_bbox:
            bbox = self._source_page_bbox(
                vision_bbox,
                signals.get("vision_bbox_coordinate_space", "vision_normalized_bottom_left"),
            )
            location_kind, location_accuracy = "vision_word", "exact"
        else:
            bbox = signals.get("surya_bbox")
            location_kind = "surya_text_block" if bbox else None
            location_accuracy = "block" if bbox else "unavailable"
        valid_bbox = (
            isinstance(bbox, list) and len(bbox) == 4
            and all(isinstance(value, (int, float)) and 0 <= value <= 1 for value in bbox)
            and bbox[2] > bbox[0] and bbox[3] > bbox[1]
        )
        return {
            "item_id": item_id, "page": item["page"], "priority": item["priority"],
            "item_type": item["item_type"], "reasons": item.get("reasons", []), "signals": item.get("signals", {}),
            "surya": item.get("surya", ""), "vision": item.get("vision", ""),
            "status": self.status_for(item_id), "image_available": image_path is not None,
            "page_image": image_reference,
            "source_asset": {
                "page_image": image_reference,
                "crop": bbox if valid_bbox else None,
                "crop_available": bool(valid_bbox and image_path),
                "coordinate_space": "source_page_normalized_top_left" if valid_bbox else None,
                "location_kind": location_kind,
                "location_accuracy": location_accuracy if valid_bbox else "unavailable",
            },
            "page_image_url": f"/asset/page/{item['page']}" if image_path else None,
            "page_context": {"text_length": len(str(page.get("text", "")))},
        }

    @staticmethod
    def _source_page_bbox(bbox, coordinate_space):
        """Return a valid top-left-normalised rectangle or None.

        The legacy default is deliberate: all stored Apple Vision word
        rectangles prior to this metadata used Vision's bottom-left system.
        Unknown coordinate systems fail closed rather than creating a visibly
        misleading highlight.
        """
        if not (isinstance(bbox, list) and len(bbox) == 4):
            return None
        try:
            x0, y0, x1, y1 = (float(value) for value in bbox)
        except (TypeError, ValueError):
            return None
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            return None
        if coordinate_space == "vision_normalized_bottom_left":
            return [x0, 1 - y1, x1, 1 - y0]
        if coordinate_space == "source_page_normalized_top_left":
            return [x0, y0, x1, y1]
        return None

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

    def batch_preview(self, cluster_id, action, excluded_ids=()):
        with self.lock:
            cluster = next(cluster for cluster in self.clusters if cluster["cluster_id"] == cluster_id)
            plan = review_batches.preview(cluster, self.items, action, excluded_ids)
            resolved = [item_id for item_id in plan["member_ids"] if self.status_for(item_id)["status"] != "unresolved"]
            if resolved:
                raise ValueError("batch members must all be unresolved")
            # The confirmation UI needs inspectable evidence, not just opaque
            # IDs.  This remains a preview: no decision is persisted here.
            plan["members"] = [{
                "item_id": item_id, "page": self.items[item_id]["page"],
                "priority": self.items[item_id]["priority"],
                "reasons": self.items[item_id].get("reasons", []),
                "surya": self.items[item_id].get("surya", ""),
                "vision": self.items[item_id].get("vision", ""),
            } for item_id in plan["member_ids"]]
            return plan

    def batch_confirm(self, cluster_id, action, excluded_ids=(), confirmed=False):
        with self.lock:
            # Re-run the preview in the same lock: no workspace request can
            # interleave an individual decision between confirmation and write.
            self.batch_preview(cluster_id, action, excluded_ids)
            cluster = next(cluster for cluster in self.clusters if cluster["cluster_id"] == cluster_id)
            result = review_batches.apply_batch(cluster=cluster, priority_path=self.priority_path, book_path=self.book_path, decisions_path=self.decisions_path, action=action, excluded_ids=excluded_ids, confirmed=confirmed)
            if result["applied"]: self.refresh()
            return result

    def batch_revert(self, batch_id):
        result = review_batches.revert_batch(decisions_path=self.decisions_path, batch_id=batch_id); self.refresh(); return result


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
        if parsed.path == "/api/cluster":
            try:
                query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
                clusters = self.workspace.filtered_clusters(query); index = max(0, int(query.get("index", "0")))
                if not clusters: return self._json({"total": 0, "progress": self.workspace.progress()})
                index = min(index, len(clusters) - 1)
                return self._json({"total": len(clusters), "index": index, "cluster": self.workspace.cluster_payload(clusters[index]["cluster_id"]), "progress": self.workspace.progress()})
            except (ValueError, KeyError, StopIteration) as error:
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
            if parsed.path == "/api/batch/preview": return self._json(self.workspace.batch_preview(payload["cluster_id"], payload["action"], payload.get("excluded_member_ids", [])))
            if parsed.path == "/api/batch/confirm": return self._json(self.workspace.batch_confirm(payload["cluster_id"], payload["action"], payload.get("excluded_member_ids", []), payload.get("confirmed") is True))
            if parsed.path == "/api/batch/revert": return self._json(self.workspace.batch_revert(payload["batch_id"]))
            return self._error("not found", HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as error:
            return self._error(str(error))


PAGE = """<!doctype html><meta charset=utf-8><title>BookText review</title>
<style>body{font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem}button,select,input,textarea{font:inherit;margin:.2rem}pre{white-space:pre-wrap;background:#f5f5f5;padding:.8rem}img{max-width:100%;max-height:600px}.members label{display:block}</style>
<h1>BookText local review</h1><p>Surya is primary OCR; Vision is evidence, never automatic replacement.</p>
<div><select id=mode><option value=item>Items</option><option value=cluster>Clusters</option></select><select id=pfilter><option value=''>All priorities</option><option>HIGH</option><option>MEDIUM</option><option>LOW</option><option>INFO</option></select><select id=sfilter><option value=''>All statuses</option><option value=unresolved>Unresolved</option><option value=resolved_changed>Resolved changed</option><option value=resolved_no_change>Resolved no-change</option><option value=deferred>Deferred</option><option value=stale>Stale</option><option value=conflict>Conflict</option></select><input id=page placeholder=Page><button onclick='go(0)'>Filter</button></div>
<p id=progress></p><button onclick='go(-1)'>Previous</button><button onclick='go(1)'>Next</button><button onclick='nextUnresolved()'>Next unresolved</button><button onclick='build()'>Build final book</button><p id=message></p><section id=item></section>
<script>
let n=0,current='',batch=null;const q=()=>new URLSearchParams({index:n,priority:pfilter.value,status:sfilter.value,page:page.value});
async function post(path,body){return await (await fetch(path,{method:'POST',body:JSON.stringify(body)})).json()}
function append(tag,text){let e=document.createElement(tag);e.textContent=text;item.append(e);return e}
function exclusions(){return [...document.querySelectorAll('.member-exclude:checked')].map(x=>x.value)}
function show(x){progress.textContent=Object.entries(x.progress).map(([k,v])=>k+': '+v).join(' | ');n=x.index||0;item.replaceChildren();
 if(x.cluster){let c=x.cluster;append('h2',`Cluster ${c.category} · ${c.member_count} members`);append('pre',JSON.stringify({pages:c.pages,decisions:c.decision_statistics,eligibility:c.eligibility_reasons,rejected:c.rejection_reasons},null,2));let members=document.createElement('div');members.className='members';for(let m of c.members){let l=document.createElement('label'),box=document.createElement('input');box.type='checkbox';box.className='member-exclude';box.value=m.item_id;box.disabled=!c.eligible_member_ids.includes(m.item_id);l.append(box,document.createTextNode(` Exclude ${m.item_id} · page ${m.page} · ${m.priority} · ${m.status}`));members.append(l)}item.append(members);
 if(c.batch_eligible){let s=document.createElement('select');for(let a of c.allowed_batch_actions){let o=document.createElement('option');o.value=a;o.textContent=a;s.append(o)}let b=document.createElement('button');b.textContent='Preview exact batch';b.onclick=async()=>{batch=await post('/api/batch/preview',{cluster_id:c.cluster_id,action:s.value,excluded_member_ids:exclusions()});message.textContent='Preview: '+JSON.stringify(batch)+' — inspect every listed member, then confirm.';let ok=document.createElement('button');ok.textContent='Confirm batch';ok.onclick=async()=>{message.textContent=JSON.stringify(await post('/api/batch/confirm',{cluster_id:c.cluster_id,action:s.value,excluded_member_ids:batch.excluded_member_ids,confirmed:true}));go(0)};item.append(ok)};item.append(s,b)}return}
 if(!x.item){append('p','No matching review items.');return}let i=x.item;current=i.item_id;append('h2',`${i.priority} · page ${i.page} · ${i.status.status}`);for(let [k,v] of [['Item ID',i.item_id],['Reasons',i.reasons.join(', ')],['Signals',JSON.stringify(i.signals)],['Surya',i.surya],['Vision alternative',i.vision||'—']])append('pre',k+'\\n'+v);if(i.page_image_url){let im=document.createElement('img');im.src=i.page_image_url;im.alt='Source page '+i.page;item.append(im)}for(let a of ['keep_surya','accept_vision','ignore','defer']){let b=document.createElement('button');b.textContent=a;b.disabled=a==='accept_vision'&&!i.vision;b.onclick=()=>decide(a);item.append(b)}let ta=document.createElement('textarea');ta.id='manual';ta.placeholder='Manual replacement';item.append(ta);let mb=document.createElement('button');mb.textContent='manual';mb.onclick=()=>decide('manual');item.append(mb)}
async function go(d){n=Math.max(0,n+d);let path=mode.value==='cluster'?'/api/cluster?':'/api/item?';show(await (await fetch(path+q())).json())}async function nextUnresolved(){mode.value='item';sfilter.value='unresolved';go(0)}async function decide(action){let x=await post('/api/decision',{item_id:current,action,replacement_text:manual.value});message.textContent=x.error||'Saved immediately.';go(0)}async function build(){message.textContent=JSON.stringify(await post('/api/build',{}))}go(0)
</script>"""


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
