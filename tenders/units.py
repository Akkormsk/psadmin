import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def okei_units():
    data_path = Path(__file__).with_name("data") / "okei.json"
    return json.loads(data_path.read_text(encoding="utf-8"))["units"]


def display_unit(value):
    return str(value or "").replace("[2*]", "²").replace("[3*]", "³").replace("^2", "²").replace("^3", "³")
