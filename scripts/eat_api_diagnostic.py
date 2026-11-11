"""Read-only ЕАТ purchase-list API diagnostic. Does not persist or submit bids."""
import os
import re
import sys
import time
import uuid
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree as ET

BASE = "https://agregatoreat.ru/integration/ecom/rest/api/"
NS = "http://agregatoreat.ru/eat/object-types/"
EAT = "http://agregatoreat.ru/eat/"
MIN_PRICE = Decimal("300000")
KEYWORDS = re.compile(
    r"сувенир|полиграф|брендир|(?<![а-яё])мерч|футбол|толстов|худи|"
    r"блокнот|значк|бейсбол|кепк|шоппер|подароч|"
    r"нанесени|гравиров|ежедневник|термокруж|кружк|промопродукц|"
    r"типограф|печатн.{0,30}продукц|ручк.{0,30}(логотип|сувенир)|"
    r"(пакет|флаг|баннер).{0,30}(логотип|нанесени|печать)",
    re.IGNORECASE,
)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def xml_request(element, external_number, request_id):
    root = ET.Element(
        f"{{{NS}}}{element}",
        {f"{{{EAT}}}Version": "2.0.0", f"{{{EAT}}}RequestUID": request_id},
    )
    ET.SubElement(root, "extSystem").text = external_number
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def send(endpoint, payload, token):
    request = Request(
        BASE + endpoint,
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "text/xml",
            "Accept": "text/xml",
            "User-Agent": "PSADMIN-EAT-ReadOnlyDiagnostic/1.0",
        },
    )
    try:
        with urlopen(request, timeout=35) as resp:
            status = resp.status
            media_type = resp.headers.get_content_type()
            data = resp.read(20 * 1024 * 1024 + 1)
    except HTTPError as exc:
        status = exc.code
        media_type = exc.headers.get_content_type()
        data = exc.read(4096)
    except (URLError, OSError, TimeoutError):
        print("Network/TLS failure. No token or response body printed.")
        raise SystemExit(2)
    print(f"API endpoint={endpoint}: HTTP {status}, content-type={media_type}")
    if status >= 400:
        print("Request rejected. HTTP status alone does not establish token validity.")
        raise SystemExit(2)
    if len(data) > 20 * 1024 * 1024:
        print("Response exceeded 20 MiB safety cap.")
        raise SystemExit(2)
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        print("Unexpected non-XML response; body suppressed.")
        raise SystemExit(2)


def find_first(root, field):
    for element in root.iter():
        if local(element.tag) == field and element.text:
            return element.text.strip()
    return ""


def summarize(root):
    orders = [e for e in root.iter() if local(e.tag) == "orders"]
    matches = []
    above_min = 0
    keyword_hits = 0
    for order in orders:
        try:
            amount = Decimal(find_first(order, "contractSum").replace(" ", "").replace(",", "."))
        except (InvalidOperation, ValueError):
            continue
        if amount < MIN_PRICE:
            continue
        above_min += 1
        label_parts = []
        for child in order.iter():
            if local(child.tag) in {"regName", "name", "description", "title", "productName"} and child.text:
                label_parts.append(child.text.strip())
        name = " ".join(label_parts)[:2500]
        if not KEYWORDS.search(name):
            continue
        keyword_hits += 1
        number = find_first(order, "regNumber")
        end = find_first(order, "expireTime")
        state = find_first(order, "state")
        # Read-only first-stage shortlist: legal basis is absent from the list schema.
        matches.append({"number": number, "title": name[:180], "price_rub": str(amount),
                        "expires": end, "state": state, "law": "unverified"})
    summary = {"source": "EAT_API", "returned_sessions": len(orders),
               "above_300k": above_min, "keyword_matches": keyword_hits,
               "law_44fz_verified": 0, "candidates": matches[:30]}
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False))
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("## EAT API (read-only)\n\n")
            f.write(f"Sessions: {len(orders)}, price >= 300k: {above_min}, keyword matches: {keyword_hits}.\n\n")
            f.write("44-FZ status not verified from list endpoint; detail lookups required.\n\n")
            for row in matches[:30]:
                title = re.sub(r"[|\r\n]", " ", row["title"])
                f.write(f"- {row['number']}: {title} — {row['price_rub']} RUB, end {row['expires']}\n")
    return len(orders)


def main():
    token = os.getenv("EAT_API_TOKEN", "").strip()
    external_number = os.getenv("EAT_EXT_SYSTEM", "").strip()
    if not token:
        sys.exit("Missing EAT_API_TOKEN secret; no request sent.")
    if not external_number:
        sys.exit("Missing EAT_EXT_SYSTEM repository variable (supplier number from LK); no request sent.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", external_number):
        sys.exit("EAT_EXT_SYSTEM has unexpected characters; no request sent.")
    request_id = str(uuid.uuid4())
    response = send("order/requestOrderList", xml_request("requestOrderList", external_number, request_id), token)
    tag = local(response.tag)
    print("Initial XML root:", tag)
    if tag == "responseOrderList" or tag == "responseProcessingResult" and any(local(n.tag) == "responseOrderList" for n in response.iter()):
        summarize(response)
        return
    if tag != "responseConfirmation":
        print("Unexpected response root; no data extracted. (Content suppressed)")
        sys.exit(2)
    if any(local(n.tag) == "violations" for n in response.iter()):
        print("Server returned validation violations (values suppressed); credentials still unverified.")
        sys.exit(2)
    # The API is asynchronous. Never poll more often than once a minute.
    for index in range(2):
        if index:
            time.sleep(65)
        result = send("processingResult", xml_request("requestProcessingResult", external_number, request_id), token)
        state = find_first(result, "ProcessingState")
        print("ProcessingState:", state if state in {"processing", "complete", "failed"} else "other")
        if any(local(n.tag) == "responseOrderList" for n in result.iter()):
            summarize(result)
            return
        if state != "processing":
            print("Processing ended without purchase list (response values suppressed).")
            sys.exit(2)
    print("Still processing. Stop without repeated requests; can resume with original request ID.")
    sys.exit(3)


if __name__ == "__main__":
    main()
