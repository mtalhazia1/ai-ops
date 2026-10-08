"""Offline stand-ins for HubSpot, Slack, ShipMatch, the Gmail attachments API and the
Anthropic Messages API, so the n8n workflows can be run end to end without accounts.

Development only. Run by docker-compose.mocks.yml; inspect with GET /_state, clear with
POST /_reset. The "LLM" here is a few regexes: it exercises the plumbing, not accuracy.
"""

import base64
import itertools
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
IDS = itertools.count(1001)
STATE: dict = {}

SHIPMENTS = [
    {"id": 501, "reference": "SH-1022", "status": "ready", "containers": ["MSCU1234566"], "pos": ["7781"]},
    {"id": 502, "reference": "SH-889", "status": "approved", "containers": ["CSQU3054383"], "pos": []},
]
FAKE_PDF = b"%PDF-1.4\n% mock attachment\n1 0 obj <<>> endobj\ntrailer <<>>\n%%EOF\n"


def reset():
    with LOCK:
        STATE.clear()
        STATE.update({"contacts": {}, "deals": [], "notes": [], "tasks": [], "slack": [], "documents": [],
                      "lookups": [], "llm_calls": 0, "unauthorized": 0})


reset()


# --- Rule-based "LLM" -----------------------------------------------------------------

CITIES = ["Lahore", "Karachi", "Islamabad", "Faisalabad", "Multan", "Peshawar", "Hyderabad", "Quetta", "Sialkot", "Dubai"]
INJECTION = re.compile(r"ignore (all |the )?previous instructions|^\s*system\s*:|you are now|\[system\]", re.I | re.M)


def classify(text):
    t = text.lower()
    has_docs = "attachments: (none)" not in t
    if re.search(r"damage|crushed|shortage|kharab|geela|compensation", t):
        cat = "claim"
    elif re.search(r"confirm\w* (the )?booking|book it|booking confirm|bookng", t):
        cat = "booking"
    elif re.search(r"where is|status|any update|\beta\b|tracking", t):
        cat = "shipment_status"
    elif has_docs and re.search(r"invoice|bill of lading|credit note|packing list", t):
        cat = "paperwork"
    elif re.search(r"\brate\b|quote|truck chahiye", t):
        cat = "quote_request"
    else:
        cat = "other"
    return {"category": cat, "urgency": "high" if cat == "claim" else "normal", "confidence": 0.9 if cat != "other" else 0.5,
            "reason": f"mock rule matched {cat}", "contains_instructions_to_ai": bool(INJECTION.search(text))}


def extract(text, fields):
    items = []

    def add(field, value, evidence):
        if field in fields:
            items.append({"field": field, "value": str(value), "evidence": evidence, "confidence": 0.9})

    found = [(m.start(), c) for c in CITIES for m in re.finditer(c, text)]
    found.sort()
    if found:
        add("origin_city", found[0][1], found[0][1])
    if len(found) > 1:
        add("destination_city", found[1][1], found[1][1])
    if m := re.search(r"(\d+)\s*(pallets?|cartons?|bags?)", text, re.I):
        add("pieces", m.group(1), m.group(0))
        add("pieces_affected", m.group(1), m.group(0))
        add("package_type", m.group(2).lower().rstrip("s"), m.group(0))
    if m := re.search(r"(\d[\d,]*)\s*kg", text, re.I):
        add("weight_kg", m.group(1).replace(",", ""), m.group(0))
    for m in re.finditer(r"\b[A-Z]{4}\s?\d{6}\s?\d\b", text):
        add("container_numbers", m.group(0).replace(" ", ""), m.group(0))
    if m := re.search(r"\bPO\s*#?\s*(\d+)", text):
        add("po_numbers", m.group(1), m.group(0))
    if m := re.search(r"\bQ-?\d{3,}\b", text):
        add("quote_reference", m.group(0), m.group(0))
    for m in re.finditer(r"\b(?:SH|INV|CN|IF|KHI)-\d+\b", text):
        add("reference_numbers", m.group(0), m.group(0))
    if re.search(r"photos? attached|\.jpg", text, re.I):
        add("photos_attached", "true", "Photos attached")
    if re.search(r"crushed|damaged", text, re.I):
        add("damage_description", "cartons crushed or damaged", "damaged")
    return {"items": items}


def llm(body):
    with LOCK:
        STATE["llm_calls"] += 1
    text = body["messages"][0]["content"]
    schema = body.get("output_config", {}).get("format", {}).get("schema", {})
    if "category" in schema.get("properties", {}):
        out = classify(text)
    else:
        fields = schema["properties"]["items"]["items"]["properties"]["field"]["enum"]
        out = extract(text, fields)
    return {"id": f"msg_mock_{next(IDS)}", "type": "message", "role": "assistant", "model": body.get("model"),
            "content": [{"type": "text", "text": json.dumps(out)}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 600, "output_tokens": 80}}


# --- HTTP -------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("mock:", self.command, self.path.split("?")[0], fmt % args if "%" in fmt else "", flush=True)

    def _send(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _auth(self):
        if not (self.headers.get("Authorization") or self.headers.get("x-api-key")):
            with LOCK:
                STATE["unauthorized"] += 1
            self._send(401, {"error": "missing auth"})
            return False
        return True

    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path == "/_state":
            with LOCK:
                return self._send(200, STATE)
        if path == "/health":
            return self._send(200, {"ok": True})
        if not self._auth():
            return
        if m := re.match(r"^/gmail/gmail/v1/users/me/messages/[^/]+/attachments/([^/]+)$", path):
            data = base64.urlsafe_b64encode(FAKE_PDF + m.group(1).encode()).decode()
            return self._send(200, {"size": len(FAKE_PDF), "data": data})
        if re.match(r"^/shipmatch/api/[^/]+/shipments$", path):
            q = (query.get("q") or [""])[0].upper()
            hits = [s for s in SHIPMENTS if q and (q == s["reference"] or q in s["containers"] or q in s["pos"])]
            with LOCK:
                STATE["lookups"].append(q)
            return self._send(200, {"count": len(hits), "results": hits})
        self._send(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self._body()
        if path == "/_reset":
            reset()
            return self._send(200, {"ok": True})
        if path == "/anthropic/v1/messages":
            return self._send(200, llm(json.loads(raw)))
        if not self._auth():
            return
        if path == "/hubspot/crm/v3/objects/contacts/batch/upsert":
            results = []
            with LOCK:
                for item in json.loads(raw)["inputs"]:
                    email = item["id"].lower()
                    new = email not in STATE["contacts"]
                    if new:
                        STATE["contacts"][email] = {"id": str(next(IDS)), "properties": {}}
                    STATE["contacts"][email]["properties"].update(item.get("properties", {}))
                    results.append({"id": STATE["contacts"][email]["id"], "new": new,
                                    "properties": STATE["contacts"][email]["properties"]})
            return self._send(200, {"status": "COMPLETE", "results": results})
        if m := re.match(r"^/hubspot/crm/v3/objects/(deals|notes|tasks)$", path):
            data = json.loads(raw)
            record = {"id": str(next(IDS)), "properties": data.get("properties", {}),
                      "associations": data.get("associations", [])}
            with LOCK:
                STATE[m.group(1)].append(record)
            return self._send(201, record)
        if path == "/slack/api/chat.postMessage":
            data = json.loads(raw)
            if data.get("channel") == "#broken":
                return self._send(200, {"ok": False, "error": "channel_not_found"})
            ts = f"{next(IDS)}.000100"
            with LOCK:
                STATE["slack"].append({"channel": data.get("channel"), "text": data.get("text"), "ts": ts})
            return self._send(200, {"ok": True, "channel": data.get("channel"), "ts": ts})
        if re.match(r"^/shipmatch/api/[^/]+/documents$", path):
            if b'name="file"' not in raw:
                return self._send(400, {"detail": "file is required"})
            fname = re.search(rb'filename="([^"]*)"', raw)
            name = fname.group(1).decode() if fname else "upload"
            with LOCK:
                existing = next((d for d in STATE["documents"] if d["size"] == len(raw) and d["filename"] == name), None)
                if existing:
                    return self._send(200, {"id": existing["id"], "status": "duplicate"})
                doc = {"id": f"doc_{next(IDS)}", "filename": name, "size": len(raw)}
                STATE["documents"].append(doc)
            return self._send(201, {"id": doc["id"], "status": "received"})
        self._send(404, {"error": "not found", "path": path})


if __name__ == "__main__":
    print("mock services on :9000", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
