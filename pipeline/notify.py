"""Open or update one GitHub Issue per alert label (plan §8.7).

  python3 pipeline/notify.py                    read data/alerts.json and file issues
  python3 pipeline/notify.py --workflow-failed  file a 'workflow-failed' issue
Labels: feed-down, schema-change, count-mismatch, range-reject, jump-held, datum-mismatch, override-expired,
accuracy-mismatch, workflow-failed. Uses GITHUB_TOKEN + GITHUB_REPOSITORY (set in Actions); without them it
only prints, e.g. on the Mac. Alerts are strings "label: text".
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import common  # noqa: E402

API = "https://api.github.com"
LABELS = ("feed-down", "schema-change", "count-mismatch", "range-reject", "jump-held", "datum-mismatch",
          "override-expired", "accuracy-mismatch", "workflow-failed")


def gh(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                          "Accept": "application/vnd.github+json",
                                          "User-Agent": "lakelevelfinder-notify"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"null")


def file_issue(label, lines):
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (repo and os.environ.get("GITHUB_TOKEN")):
        print(f"[notify] {label}: " + " | ".join(lines))
        return False
    text = "\n".join(f"- {x}" for x in lines)
    found = gh("GET", f"/repos/{repo}/issues?state=open&labels={urllib.parse.quote(label)}&per_page=5")
    if found:
        gh("POST", f"/repos/{repo}/issues/{found[0]['number']}/comments", {"body": text})
    else:
        gh("POST", f"/repos/{repo}/issues", {"title": f"[{label}] pipeline alert", "body": text, "labels": [label]})
    return True


def group(alerts):
    """{label: [alert, ...]}; an alert with an unknown label is filed under schema-change."""
    by = {}
    for a in alerts:
        label = a.partition(":")[0].strip()
        by.setdefault(label if label in LABELS else "schema-change", []).append(a)
    return by


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--workflow-failed" in argv:
        run = (os.environ.get("GITHUB_SERVER_URL", "") + "/" + os.environ.get("GITHUB_REPOSITORY", "")
               + "/actions/runs/" + os.environ.get("GITHUB_RUN_ID", ""))
        file_issue("workflow-failed", [f"Pipeline run failed: {run}"])
        return 0
    alerts = (common.load_json(common.DATA / "alerts.json", {}) or {}).get("alerts", [])
    for label, lines in group(alerts).items():
        file_issue(label, lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
