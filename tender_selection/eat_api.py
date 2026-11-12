"""Read-only EAT (Berezka) XML API adapter.

The upstream API is asynchronous: an initial POST acknowledges the request,
then processingResult returns the actual list. Only order-list and order-
notification endpoints are permitted here. All XML namespaces follow the
official EAT 2.82.1 schema.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

LOGGER = logging.getLogger(__name__)
API = "https://agregatoreat.ru/integration/ecom/rest/api/"
OBJECT_NS = "http://agregatoreat.ru/eat/object-types/"
EAT_NS = "http://agregatoreat.ru/eat/"

class EatError(Exception):
    """A transport or EAT processing failure (never contains credentials)."""


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _value(element, name):
    for child in element.iter():
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _xml_message(name, ext_system, uid, *, dates=None, order_number=None):
    root = ET.Element(
        f"{{{OBJECT_NS}}}{name}",
        {f"{{{EAT_NS}}}Version": "2.0.0", f"{{{EAT_NS}}}RequestUID": uid},
    )
    if dates is not None:
        start, end = dates
        ET.SubElement(root, f"{{{EAT_NS}}}dateFrom").text = start.isoformat(timespec="seconds")
        ET.SubElement(root, f"{{{EAT_NS}}}dateTo").text = end.isoformat(timespec="seconds")
    if order_number is not None:
        ET.SubElement(root, f"{{{EAT_NS}}}OrderNumber").text = order_number
    # Local (unqualified) extSystem is required by eatObjects.xsd.
    ET.SubElement(root, "extSystem").text = ext_system
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _send(path, body, token, *, timeout=35):
    if path not in {
        "order/requestOrderList",
        "order/requestOrderListLite",
        "order/requestOrdersListLitePublished",
        "order/orderNotification",
        "processingResult",
    }:
        raise EatError("EAT method is not allowed for read-only import")
    req = Request(
        API + path, method="POST", data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "text/xml",
            "Accept": "text/xml",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as res:
            if res.status != 200:
                raise EatError(f"EAT HTTP {res.status}")
            payload = res.read(20 * 1024 * 1024 + 1)
    except HTTPError as exc:
        raise EatError(f"EAT HTTP {exc.code} ({path})") from None
    except (URLError, OSError, TimeoutError):
        raise EatError("EAT connection failed") from None
    if len(payload) > 20 * 1024 * 1024:
        raise EatError("EAT response exceeded size limit")
    try:
        return ET.fromstring(payload)
    except ET.ParseError:
        raise EatError("EAT returned non-XML response") from None


def request(name, *, order_number=None, dates=None, token=None, ext_system=None,
            attempts=3, interval=65):
    """Return completed responseProcessingResult. Never issue an offer/proposal."""
    token = token or os.getenv("EAT_API_TOKEN", "")
    ext_system = ext_system or os.getenv("EAT_EXT_SYSTEM", "")
    if not token or not ext_system:
        raise EatError("EAT API credentials are not configured")
    if name not in {"requestOrderList", "requestOrderListLite", "requestOrdersListLitePublished", "requestOrderNotification"}:
        raise EatError("Unsupported EAT read operation")
    uid = str(uuid.uuid4())
    path = ("order/orderNotification" if name == "requestOrderNotification" else "order/" + name)
    initial = _send(
        path, _xml_message(name, ext_system, uid, dates=dates, order_number=order_number),
        token,
    )
    if _local(initial.tag) != "responseConfirmation":
        raise EatError("EAT did not acknowledge the request")
    for attempt in range(attempts):
        if attempt:
            time.sleep(interval)
        result = _send(
            "processingResult", _xml_message("requestProcessingResult", ext_system, uid), token,
        )
        state = _value(result, "ProcessingState").lower()
        if state == "complete":
            return result
        if state not in {"processing", "queued"}:
            # Error description can contain upstream details; don't echo raw XML/log secrets.
            raise EatError(f"EAT processing returned {state or 'unknown'}")
    raise EatError("EAT response is still processing; retry in the next update")


def _price(value):
    try:
        return Decimal(value.replace(",", ".").replace(" ", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # EAT timezone is not explicitly fixed by XSD. For offset-less values,
    # use the Moscow-zone convention of the site's published session times.
    if dt.tzinfo is None:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo("Europe/Moscow"))
    return dt


def _orders(root):
    return [node for node in root.iter() if _local(node.tag) == "orders"]


def listing(*, days=7, now=None):
    """Try date-scoped published service; if unavailable, expose a partial active-only list.

    Return (orders, mode). 'active_only' is NOT equivalent to all published
    purchases in the preceding N days; callers must preserve that limitation.
    """
    now = now or datetime.now(timezone.utc)
    date_from = now - timedelta(days=days)
    try:
        response = request("requestOrdersListLitePublished", dates=(date_from, now))
        return _orders(response), "published"
    except EatError as exc:
        LOGGER.warning("EAT date-scoped list unavailable; using active-only list: %s", exc)
        response = request("requestOrderList")
        return _orders(response), "active_only"


def shortlist(orders, *, minimum, include, exclude, now, days, mode):
    """Return low-cost candidates, retaining raw source item only in memory."""
    from .filtering import match_title
    minimum = Decimal(str(minimum or 0))
    cutoff = now - timedelta(days=days)
    result = []
    for item in orders:
        number = _value(item, "regNumber")
        title = _value(item, "regName")
        price = _price(_value(item, "contractSum"))
        deadline = _datetime(_value(item, "expireTime"))
        published = _datetime(_value(item, "startTime"))
        if not number or price is None or price < minimum:
            continue
        if deadline is None or deadline <= now:
            continue
        # Active-only API has no publication date; do not invent a date-window filter.
        if not match_title(title, include, exclude)[0]:
            continue
        result.append({
            "purchase_number": number, "title": title, "max_price": price,
            "collecting_finished_at": deadline,
            # startTime is the session's start, NOT the publication timestamp.
            "published_at": None, "session_start": published,
        })
    return result


def notification(order_number):
    """Read EAT session details for a shortlist item."""
    return request("requestOrderNotification", order_number=order_number)


def legal_basis(notification_response):
    """Classify only explicitly stated law in the EAT typePurchase field."""
    text = _value(notification_response, "typePurchase").lower().replace(" ", "")
    if "44-фз" in text or "№44-фз" in text:
        return "fz44"
    if "223-фз" in text or "№223-фз" in text:
        return "fz223"
    return None


def notification_fields(root):
    """Extract normalized details without conflating EAT data with EIS notification."""
    keys = ("typePurchase", "PurchaseMethod", "WWWReference", "maxOrderCost", "OrderNumber",
            "orderExpireDate", "startDate", "stateDescription", "DeliveryDate")
    return {key: _value(root, key) for key in keys}
