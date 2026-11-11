"""Read-only inspection of legal basis fields of one public EAT procurement session."""
import os, uuid, time
from xml.etree import ElementTree as ET
from eat_api_diagnostic import send, local, find_first
ROOT="http://agregatoreat.ru/eat/object-types/"
EAT="http://agregatoreat.ru/eat/"
uid=str(uuid.uuid4())
number=os.environ.get("EAT_SAMPLE_NUMBER")
root=ET.Element(f"{{{ROOT}}}requestOrderNotification",{f"{{{EAT}}}Version":"2.0.0",f"{{{EAT}}}RequestUID":uid})
ET.SubElement(root,f"{{{EAT}}}OrderNumber").text=number
ET.SubElement(root,"extSystem").text="300911501"
response=send("order/orderNotification",ET.tostring(root,xml_declaration=True,encoding="utf-8"),os.environ["EAT_API_TOKEN"])
print("root",local(response.tag))
for step in range(3):
    if step:time.sleep(65)
    nextreq=ET.Element(f"{{{ROOT}}}requestProcessingResult",{f"{{{EAT}}}Version":"2.0.0",f"{{{EAT}}}RequestUID":uid})
    ET.SubElement(nextreq,"extSystem").text="300911501"
    data=send("processingResult",ET.tostring(nextreq,xml_declaration=True,encoding="utf-8"),os.environ["EAT_API_TOKEN"])
    state=find_first(data,"ProcessingState")
    print("state:",state,"root:",local(data.tag))
    if state=="processing": continue
    # Print only fields describing legal basis; never print request headers or customer data.
    for name in ("typePurchase","PurchaseMethod","RNP44Flag","RNP223Flag","budgetInfo"):
        v=find_first(data,name)
        print(name, (v[:130] if name!="budgetInfo" else ("exists" if v else "absent")))
    print("payload roots",sorted({local(c.tag) for c in data.iter() if "Notification" in local(c.tag) or "Violation" in local(c.tag) or "Error" in local(c.tag)})[:20])
    break
