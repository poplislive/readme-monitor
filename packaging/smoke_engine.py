"""Smoke-test a frozen engine as the desktop shell uses it, with no Python environment involved:
start `desktop-serve --demo` (simulation: no GitHub, no Anthropic, no scheduler, a temporary
folder), read the ready line, sign in with the one-time code, load /, /api/session and
/api/state, run a simulated dry run, then close stdin and expect a clean exit.

    python packaging/smoke_engine.py build/engine-dist/engine/rm-engine[.exe]
"""
import http.client
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def req(port, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


def main(exe: str) -> int:
    started = time.monotonic()
    exe = str(Path(exe).resolve())  # Windows CreateProcess does not resolve relative paths with forward slashes
    p = subprocess.Popen([exe,"desktop-serve", "--demo"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8")
    try:
        ready = json.loads(p.stdout.readline())
        assert ready.get("event") == "ready", ready
        port = ready["port"]
        print(f"ready in {time.monotonic() - started:.1f}s on port {port}, engine {ready['version']}")
        origin = {"Origin": f"http://127.0.0.1:{port}"}
        status, headers, _ = req(port, "POST", "/auth", {**origin, "X-RM-Pair": ready["code"]})
        assert status == 204, status
        cookie = re.search(r"rm_session=([^;]+)", headers["Set-Cookie"])[1]
        auth = {"Cookie": f"rm_session={cookie}"}
        status, _, page = req(port, "GET", "/", auth)
        assert status == 200 and b'<div id="root">' in page, (status, page[:200])
        status, _, body = req(port, "GET", "/api/session", auth)
        csrf = json.loads(body)["csrf"]
        status, _, body = req(port, "GET", "/api/state", auth)
        state = json.loads(body)
        assert status == 200 and state["simulation"] is True, status
        print(f"state: platform {state['installation']['platform']}, scheduler {state['installation']['scheduler']}, "
              f"credential store {state['installation']['credential_store']}")
        status, _, body = req(port, "POST", "/api/run",{**auth, **origin, "X-RM-CSRF": csrf, "Content-Type": "application/json"},
                              json.dumps({"kind": "dry_run"}))
        assert status == 200 and json.loads(body)["started"] is True, (status, body)
        for _ in range(120):
            time.sleep(0.5)
            runtime = json.loads(req(port, "GET", "/api/state", auth)[2])["runtime"]
            if runtime["state"] != "running" and runtime["last_run"]:
                break
        print(f"simulated dry run: {runtime['last_run']['outcome'] if runtime['last_run'] else 'none'}")
        p.stdin.write(json.dumps({"id": 1, "cmd": "state"}) + "\n")
        p.stdin.flush()
        print("helper state:", json.loads(p.stdout.readline()))
        p.stdin.close()
        code = p.wait(30)
        assert code == 0, code
        print("clean exit")
        return 0
    finally:
        if p.poll() is None:
            p.kill()
            print("stderr:", p.stderr.read()[-2000:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
