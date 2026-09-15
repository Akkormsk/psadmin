from copy import deepcopy


def text_search_settings(settings):
    cleaned = deepcopy(settings) if isinstance(settings, dict) else {}
    steps = cleaned.get("steps")
    if isinstance(steps, dict) and isinstance(steps.get("3"), dict):
        steps["3"] = {key: value for key, value in steps["3"].items() if key == "sources"}
    return cleaned
