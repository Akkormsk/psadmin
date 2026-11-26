"""Single entry point for reviewed PSADMIN defect registration and deployment review."""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path

KEY = re.compile(r"[a-z][a-z0-9_-]*(?:/[a-z][a-z0-9_-]*){2,}")
EVENT = re.compile(r"[a-zA-Z0-9._:/-]{1,160}")
SHA = re.compile(r"[0-9a-f]{40}")
TEST = re.compile(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*){2,}")
MARK = re.compile(r"<!-- psadmin-memory:([^\s]+) -->")
EVENT_MARK = re.compile(r"<!-- psadmin-event:([^\s]+) -->")
SCOPE = re.compile(r"<!-- psadmin-scope:(TEST_VERIFIED|PROD_VERIFIED) -->")
REVIEW = re.compile(r"<!-- psadmin-review:([^\s]+) -->")


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True)


def issues(repo):
    pages = json.loads(gh("api", "--paginate", "--slurp",
                          f"repos/{repo}/issues?state=all&per_page=100"))
    return [row for page in pages for row in page if "pull_request" not in row]


def marker(name, value):
    return f"<!-- psadmin-{name}:{value} -->"


def validate(data, repo):
    required = ("key", "event", "title", "stage", "cause", "scenario",
                "expected", "actual", "sha", "evidence_url", "scope", "review")
    for key in required:
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Required nonempty field: {key}")
        if "<!--" in data[key]:
            raise ValueError("Reserved metadata in input")
    if not KEY.fullmatch(data["key"]) or re.search(r"\d{8,}", data["key"]):
        raise ValueError("Use a stable domain/component/failure key, not a tender, date or job ID")
    if not EVENT.fullmatch(data["event"]) or not SHA.fullmatch(data["sha"]):
        raise ValueError("Invalid event identity or full code SHA")
    if data["scope"] not in ("TEST_VERIFIED", "PROD_VERIFIED"):
        raise ValueError("A defect must have verified evidence")
    if not re.fullmatch(r"https://github\.com/" + re.escape(repo) +
                        r"/actions/runs/[0-9]+(?:/job/[0-9]+)?", data["evidence_url"]):
        raise ValueError("Link to a completed reviewed diagnostic/test run in this repository")
    if data["review"] != "manual" and not TEST.fullmatch(data["review"]):
        raise ValueError("Review must be manual or one fully qualified regression test")
    if data.get("confirmed") is not True:
        raise ValueError("Explicit confirmation of a diagnosed cause is mandatory")
    if data["cause"].strip().casefold() in {"unknown", "неизвестно", "не установлена", "не прочитался", "partial", "failed"}:
        raise ValueError("A symptom is not a cause")
    # Verify run exists and completed; a successful diagnostic may reproduce a defect.
    run_id = re.search(r"/runs/([0-9]+)", data["evidence_url"]).group(1)
    run = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}"))
    if run.get("status") != "completed":
        raise ValueError("Evidence run must be completed")
    return data


def register(repo, data):
    data = validate(data, repo)
    rows = issues(repo)
    matches = [row for row in rows if data["key"] in MARK.findall(row.get("body") or "")]
    if len(matches) > 1:
        raise ValueError("Duplicate existing keys: reconcile before registration")
    other = [row for row in rows if data["event"] in EVENT_MARK.findall(row.get("body") or "")
             and row not in matches]
    if other:
        raise ValueError("Event already belongs to another issue; check the canonical cause")
    entry = ("\n\n### Подтверждённый случай\n"
             + marker("event", data["event"]) + "\n"
             + "\n\n".join(f"**{label}:** {data[key]}" for label, key in (
                 ("Этап", "stage"), ("Причина", "cause"), ("Сценарий", "scenario"),
                 ("Ожидалось", "expected"), ("Фактически", "actual"),
                 ("Версия", "sha"), ("Доказательство", "evidence_url"))))
    if matches:
        row = matches[0]
        body = row.get("body") or ""
        events = set(EVENT_MARK.findall(body))
        if data["event"] in events:
            print(f'Issue #{row["number"]}: event already recorded')
            return
        count = len(events) + 1
        if not events:
            # Old issues have no machine event ID; do not invent historical counts.
            count = 1
        body = re.sub(r"<!-- psadmin-occurrences:\d+ -->\n?", "", body)
        body = re.sub(r"^\*\*Случаев: \d+\*\*.*\n?", "", body, flags=re.M)
        scopes = set(SCOPE.findall(body))
        scope = "PROD_VERIFIED" if "PROD_VERIFIED" in scopes or data["scope"] == "PROD_VERIFIED" else "TEST_VERIFIED"
        body = SCOPE.sub("", body)
        previous_reviews = set(REVIEW.findall(body))
        review = next(iter(previous_reviews)) if len(previous_reviews) == 1 else "manual"
        if review == "manual":
            review = data["review"]
        elif data["review"] not in ("manual", review):
            raise ValueError("Conflicting regression tests; reconcile manually")
        body = REVIEW.sub("", body)
        body = MARK.sub("", body, count=1)
        header = "\n".join([marker("memory", data["key"]), marker("occurrences", count),
                              marker("scope", scope), marker("review", review), f"**Случаев: {count}**"])
        body = header + "\n\n" + body.strip() + entry
        if len(body) > 60000:
            raise ValueError("Issue history nearing GitHub limit; compact reviewed events without losing identities")
        gh("api", "--method", "PATCH", f'repos/{repo}/issues/{row["number"]}',
           "-f", f"body={body}", "-f", "state=open")
        print(f'Issue #{row["number"]}: {count} confirmed cases')
    else:
        body = "\n".join([marker("memory", data["key"]), marker("occurrences", 1),
                           marker("scope", data["scope"]), marker("review", data["review"]),
                           "**Случаев: 1**"]) + entry
        result = json.loads(gh("api", "--method", "POST", f"repos/{repo}/issues",
                               "-f", f'title={data["title"]}', "-f", f"body={body}"))
        print(f'Created issue #{result["number"]}: 1 confirmed case')


def review_plan(repo):
    """Only committed, reviewed mappings are executable; never run issue text as commands."""
    mapping = json.loads((Path(__file__).parent / "issue_regressions.json").read_text())
    output = []
    for row in issues(repo):
        if row["state"] != "open":
            continue
        body = row.get("body") or ""
        keys, scopes, reviews = MARK.findall(body), SCOPE.findall(body), REVIEW.findall(body)
        test = mapping.get(keys[0]) if len(keys) == 1 else None
        eligible = (len(scopes) == 1 and scopes[0] == "TEST_VERIFIED"
                    and len(reviews) == 1 and test == reviews[0]
                    and isinstance(test, str) and TEST.fullmatch(test))
        output.append({"number": row["number"], "key": keys[0] if len(keys) == 1 else None,
                       "test": test if eligible else None,
                       "revision": row.get("updated_at"),
                       "events": sorted(set(EVENT_MARK.findall(body))),
                       "state": "eligible" if eligible else "manual_or_unverified"})
    return output


def review(repo, sha, evidence, results):
    if not SHA.fullmatch(sha) or not re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/actions/runs/\d+", evidence):
        raise ValueError("Verified deployment SHA and run required")
    plan = {row["number"]: row for row in review_plan(repo)}
    for result in results:
        item = plan.get(result["number"])
        if (not item or item["test"] is None or item["test"] != result.get("test")
                or item["revision"] != result.get("revision") or item["events"] != result.get("events")):
            continue
        if result.get("status") != "pass":
            print(f'Issue #{item["number"]}: remains open')
            continue
        message = (f'PASS targeted regression: {item["test"]} on verified TEST SHA {sha}. '
                   f'Evidence: {evidence}. No paid model calls.')
        gh("issue", "close", str(item["number"]), "--repo", repo, "--reason", "completed", "--comment", message)
        print(f'Issue #{item["number"]}: closed after targeted PASS')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["register", "plan", "review"])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--data")
    parser.add_argument("--sha")
    parser.add_argument("--evidence-url")
    args = parser.parse_args()
    if not args.repo:
        parser.error("Repository is required")
    if args.command == "plan":
        print(json.dumps(review_plan(args.repo)))
        return
    if os.getenv("PSADMIN_ISSUE_MEMORY_WRITER") != "1":
        parser.error("Writes must use the serialized issue-memory workflow")
    data = json.loads(Path(args.data).read_text()) if args.data else None
    if args.command == "register":
        register(args.repo, data)
    else:
        review(args.repo, args.sha or "", args.evidence_url or "", data)


if __name__ == "__main__":
    main()
