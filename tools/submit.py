"""Upload a submission to the leaderboard — refuses if tools/validate.py fails.

    HK_TOKEN=… python tools/submit.py out/live_….csv.gz --phase live
    python tools/submit.py out/final_….csv.gz --phase final --confirm "javier"      # FINAL needs an explicit confirmation
    python tools/submit.py out/live_….csv.gz --phase live --dry-run                 # validate + show what would be sent

Protocol (web `/enviar`, 4 steps): request an upload URL → PUT the file to S3 → POST /score → poll every 5 s.
The exact endpoints are taken from the official script; configure them with env vars so no URL/token lives in code:
    HK_TOKEN       team token (required, never commit it)
    HK_API         base URL of the API (e.g. https://…/api)  — default taken from HK_API_DEFAULT below if set
    HK_UPLOAD_PATH path to request the upload url   (default /upload-url;   POST json {phase, filename})
    HK_SCORE_PATH  path to trigger scoring         (default /score;        POST json {phase, key})
    HK_STATUS_PATH path to poll the result          (default /score/{id};   GET)
Limits (web /reglas): 1 submission / 10 s per phase; asking for an upload URL already consumes a slot; poll ≤ 1 / 5 s.
After a successful upload, prints the row to paste in SUBMISSIONS.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
import validate  # noqa: E402
from src.common import safe_stdout, git_sha  # noqa: E402

HK_API_DEFAULT = ""   # set to the base URL printed by the official /enviar script if the team agrees to hard-code it
POLL_SECONDS = 5
MAX_POLLS = 120


def _request(method: str, url: str, token: str | None = None, body: bytes | None = None, ctype: str | None = None):
    req = urllib.request.Request(url, data=body, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
    except urllib.error.HTTPError as ex:
        raise SystemExit(f"HTTP {ex.code} on {method} {url}: {ex.read()[:500]!r}")


def upload(path: str, phase: str, token: str, api: str) -> dict:
    up_path = os.environ.get("HK_UPLOAD_PATH", "/upload-url")
    sc_path = os.environ.get("HK_SCORE_PATH", "/score")
    st_path = os.environ.get("HK_STATUS_PATH", "/score/{id}")
    fname = os.path.basename(path)
    print(f"1/4 requesting upload url ({phase}, {fname})")
    _, info = _request("POST", api + up_path, token, json.dumps({"phase": phase, "filename": fname}).encode(), "application/json")
    put_url = info.get("upload_url") or info.get("url")
    key = info.get("key") or info.get("object_key") or fname
    if not put_url:
        raise SystemExit(f"no upload url in response: {info}")
    print("2/4 uploading to storage")
    with open(path, "rb") as fh:
        _request("PUT", put_url, None, fh.read(), "application/gzip" if path.endswith(".gz") else "text/csv")
    print("3/4 requesting score")
    _, sc = _request("POST", api + sc_path, token, json.dumps({"phase": phase, "key": key, "filename": fname}).encode(), "application/json")
    sid = sc.get("id") or sc.get("submission_id") or sc.get("job_id")
    print(f"4/4 polling every {POLL_SECONDS}s (id={sid})")
    res = sc
    for _ in range(MAX_POLLS):
        status = str(res.get("status", "")).lower()
        if status in ("done", "scored", "ok", "finished", "completed") or res.get("score") is not None or res.get("errors"):
            break
        time.sleep(POLL_SECONDS)
        _, res = _request("GET", api + st_path.format(id=sid), token)
    return res


def main(argv=None) -> int:
    safe_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("submission")
    ap.add_argument("--phase", required=True, choices=["live", "final"])
    ap.add_argument("--data", help="data root (folder with masters/) to take the phase ids from")
    ap.add_argument("--txns", help="bank_transactions.csv.gz with the phase ids (overrides --phase/--data for validation)")
    ap.add_argument("--confirm", help="FINAL only: name of the FINAL owner who approved this upload")
    ap.add_argument("--dry-run", action="store_true", help="validate and print the plan, do not upload")
    ap.add_argument("--note", default="", help="hypothesis / local window score to put in the SUBMISSIONS.md row")
    a = ap.parse_args(argv)

    print(f"validating {a.submission} against phase {a.phase} ...")
    res = validate.validate_file(a.submission, a.phase, a.data, a.txns)
    if not res["ok"]:
        print("REFUSED: the file is invalid (fix it first):")
        for e in res["errors"]:
            print(f"  row {e['row']}: {e['message']}")
        return 1
    print(f"  valid: {res['rows']} rows, {res['txns_covered']}/{res['txns_total']} txns covered "
          f"({res['txns_missing']} missing = scored as wrong), {res['uncompressed_mb']} MB")
    if res["txns_missing"]:
        print("  WARNING: uncovered bank lines; a complete file always scores better.")
    if a.phase == "final" and not a.confirm:
        print("REFUSED: FINAL is a single scored submission; pass --confirm <FINAL owner> after manual approval.")
        return 1

    token = os.environ.get("HK_TOKEN")
    api = os.environ.get("HK_API", HK_API_DEFAULT).rstrip("/")
    when = time.strftime("%Y-%m-%d %H:%M")
    row = f"| | {when} | {a.phase.upper()} | {git_sha()} | {os.path.basename(a.submission)} | {a.note} | | | <LIVE real> | {'subido' if not a.dry_run else 'dry-run'} | {a.confirm or '-'} |"
    if a.dry_run:
        print(f"dry-run: would upload to {api or '<HK_API unset>'} with token {'set' if token else 'MISSING'}")
        print("SUBMISSIONS.md row:\n" + row)
        return 0
    if not token:
        print("REFUSED: HK_TOKEN is not set.")
        return 1
    if not api:
        print("REFUSED: HK_API is not set (base URL of the official /enviar script).")
        return 1
    result = upload(a.submission, a.phase, token, api)
    print(json.dumps(result, indent=1)[:4000])
    print("\nSUBMISSIONS.md row (fill LIVE real with the score above):\n" + row)
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    sys.exit(main())
