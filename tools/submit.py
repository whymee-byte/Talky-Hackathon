"""Upload a submission to the leaderboard — refuses if tools/validate.py fails.

    HK_TOKEN=… python tools/submit.py out/live_….csv.gz --phase live
    python tools/submit.py out/final_….csv.gz --phase final --confirm "javier"      # FINAL needs an explicit confirmation
    python tools/submit.py out/live_….csv.gz --phase live --dry-run                 # validate + show what would be sent

Protocol (official script of https://hackathon.usetalky.com/enviar, 4 steps):
  1) POST {API}/submissions/upload-url  json {phase: LIVE|FINAL, filename}  -> 201 {sub_id, upload: {url, fields}}
  2) POST multipart to upload.url: every field of upload.fields, then `file` last (presigned S3 POST)
  3) POST {API}/submissions/{sub_id}/score  json {}
  4) GET  {API}/submissions/{sub_id} every 5 s until status in {scored, received, invalid, error} (max 10 min)
Auth: `Authorization: Bearer $HK_TOKEN`. HK_API overrides the base URL (default https://hackathon.usetalky.com/api).
Limits (web /reglas): 1 submission / 10 s per phase; asking for an upload URL already consumes a slot; poll >= 5 s.
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
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
import validate  # noqa: E402
from src.common import safe_stdout, git_sha  # noqa: E402

HK_API_DEFAULT = "https://hackathon.usetalky.com/api"
POLL_SECONDS = 5
MAX_WAIT_S = 600
FINAL_STATUS = {"scored", "received", "invalid", "error"}


def _request(method: str, url: str, token: str | None = None, body: bytes | None = None, ctype: str | None = None,
             timeout: int = 30):
    req = urllib.request.Request(url, data=body, method=method, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
    except urllib.error.HTTPError as ex:
        raise SystemExit(f"HTTP {ex.code} on {method} {url.split('?')[0]}: {ex.read()[:500]!r}")


def _multipart(fields: dict, fname: str, content: bytes) -> tuple[bytes, str]:
    """multipart/form-data body: all presigned fields first, the file last (S3 requirement)."""
    boundary = "----hk" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload(path: str, phase: str, token: str, api: str) -> dict:
    fname = os.path.basename(path)
    print(f"1/4 reserving slot + presigned upload ({phase}, {fname})")
    _, up = _request("POST", f"{api}/submissions/upload-url", token,
                     json.dumps({"phase": phase, "filename": fname}).encode(), "application/json")
    sub_id = up["sub_id"]
    print(f"2/4 uploading to S3 (sub_id={sub_id})")
    with open(path, "rb") as fh:
        body, ctype = _multipart(up["upload"]["fields"], fname, fh.read())
    _request("POST", up["upload"]["url"], None, body, ctype, timeout=900)
    print("3/4 queueing scoring")
    _, sub = _request("POST", f"{api}/submissions/{sub_id}/score", token, b"{}", "application/json")
    print(f"4/4 polling every {POLL_SECONDS}s (max {MAX_WAIT_S}s)")
    deadline = time.time() + MAX_WAIT_S
    while str(sub.get("status")) not in FINAL_STATUS and time.time() < deadline:
        time.sleep(POLL_SECONDS)
        _, sub = _request("GET", f"{api}/submissions/{sub_id}", token)
    return sub


def _dotenv_token():
    """TALKY_TOKEN / HK_TOKEN from the git-ignored .env at the repo root (never printed)."""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(p):
        return None
    for ln in open(p, encoding="utf-8"):
        k, _, v = ln.strip().partition("=")
        if k in ("TALKY_TOKEN", "HK_TOKEN") and v:
            return v.strip().strip('"')
    return None


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

    token = os.environ.get("HK_TOKEN") or os.environ.get("TALKY_TOKEN") or _dotenv_token()
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
    result = upload(a.submission, a.phase.upper(), token, api)
    print(json.dumps(result, indent=1)[:4000])
    print("\nSUBMISSIONS.md row (fill LIVE real with the score above):\n" + row)
    return 0 if result.get("status") in ("scored", "received") else 1


if __name__ == "__main__":
    sys.exit(main())
