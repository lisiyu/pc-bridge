#!/usr/bin/env python3
"""Minimal helper to talk to the pc-bridge on the user's Windows PC.

Usage:
  bridge_call.py health
  bridge_call.py sysinfo
  bridge_call.py list "D:\\work\\project"
  bridge_call.py read "D:\\work\\project\\build.sh" [--tail N]
  bridge_call.py exec "git -C D:\\work\\project status --short" [timeout_s]
  bridge_call.py write "D:\\path\\file" < localfile [--append]

Set PC_BRIDGE_HOST to your PC's Tailscale IP (100.x.y.z) before use.
"""
import json, os, sys, urllib.request

HOST = os.environ.get("PC_BRIDGE_HOST", "100.x.y.z")
PORT = 18743

def tunnel_proxy():
    https = os.environ.get("HTTPS_PROXY", "")
    # reuse runtime proxy, but on port 3130 (tailscale tunnel proxy)
    return https.rsplit(":", 1)[0] + ":3130"

def req(method, path, body=None, timeout=90):
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": tunnel_proxy()}))
    with opener.open(r, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")

def main():
    sub = sys.argv[1]
    if sub == "health":
        print(req("GET", "/health"))
    elif sub == "sysinfo":
        print(req("GET", "/v1/sysinfo"))
    elif sub == "list":
        from urllib.parse import quote
        print(req("GET", "/v1/list?path=" + quote(sys.argv[2])))
    elif sub == "read":
        from urllib.parse import quote
        path = "/v1/read?path=" + quote(sys.argv[2])
        if len(sys.argv) > 3 and sys.argv[3] == "--tail":
            path += "&tail_bytes=" + sys.argv[4]
        out = req("GET", path)
        try:
            obj = json.loads(out)
            print(obj.get("content", out) if obj.get("ok") else out)
        except Exception:
            print(out)
    elif sub == "exec":
        timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 300
        out = req("POST", "/v1/exec",
                  {"cmd": sys.argv[2], "timeout_s": timeout}, timeout=timeout + 30)
        try:
            obj = json.loads(out)
            print("exit_code:", obj.get("exit_code"), "truncated:", obj.get("truncated"))
            print(obj.get("output", out))
        except Exception:
            print(out)
    elif sub == "write":
        path, local = sys.argv[2], sys.argv[3]
        with open(local, "rb") as f:
            data = f.read()
        import base64
        body = {"path": path, "encoding": "base64",
                "content": base64.b64encode(data).decode(),
                "append": "--append" in sys.argv}
        print(req("POST", "/v1/write", body))
    else:
        print("unknown subcommand", file=sys.stderr); sys.exit(1)

if __name__ == "__main__":
    main()
