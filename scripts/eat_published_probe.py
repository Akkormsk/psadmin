"""Read-only probe of EAT sessions published in the preceding N days."""
import os
import time
import uuid
import json
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET
from eat_api_diagnostic import send, local, find_first

NS = "http://agregatoreat.ru/eat/object-types/"
EAT = "http://agregatoreat.ru/eat/"

def make_request(name, ext_system, uid, *, days=7):
    root = ET.Element(f"{{{NS}}}{name}", {f"{{{EAT}}}Version": "2.0.0",
                                               f"{{{EAT}}}RequestUID": uid})
    if name == "requestOrdersListLitePublished":
        now = datetime.now(timezone.utc)
        ET.SubElement(root, f"{{{EAT}}}dateFrom").text = (now - timedelta(days=days)).isoformat(timespec="seconds")
        ET.SubElement(root, f"{{{EAT}}}dateTo").text = now.isoformat(timespec="seconds")
    ET.SubElement(root, "extSystem").text = ext_system
    return ET.tostring(root, xml_declaration=True, encoding="utf-8")

def main():
    token = os.getenv("EAT_API_TOKEN", "").strip()
    ext_system = os.getenv("EAT_EXT_SYSTEM", "").strip()
    if not token or not ext_system:
        raise SystemExit("Missing token or extSystem; no request.")
    name = "requestOrdersListLitePublished"
    uid = str(uuid.uuid4())
    root = send("order/" + name, make_request(name, ext_system, uid), token)
    print("Initial result:", local(root.tag))
    if local(root.tag) != "responseConfirmation":
        print("Non-acknowledgement root; response contents suppressed.")
        return
    for attempt in range(3):
        if attempt:
            time.sleep(65)
        result = send("processingResult", make_request("requestProcessingResult", ext_system, uid), token)
        state = find_first(result, "ProcessingState")
        orders = [el for el in result.iter() if local(el.tag) == "orders"]
        counts = {}
        for item in orders:
            s = find_first(item, "state")
            counts[s] = counts.get(s, 0) + 1
        print(json.dumps({"state":state,"results":len(orders),"states":counts,
                          "root":local(result.tag),
                          "types":sorted({local(c.tag) for c in result.iter() if local(c.tag).lower().startswith("response")})[:20]},
                         ensure_ascii=False))
        if state != "processing":
            break

if __name__=="__main__":
    main()
