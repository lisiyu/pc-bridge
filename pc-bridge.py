#!/usr/bin/env python3
# pc-bridge.py - secure bridge to a Windows PC over Tailscale.
# Stdlib only. Mirrors the Go pc-bridge API and security model:
#   * binds ONLY to the Tailscale interface IP (never 0.0.0.0)
#   * every /v1/* call is authenticated via `tailscale whois <src ip>`
#     against --allow-node (node names or tags); optional Bearer token
#   * file access is confined to dir whitelists:
#     --allow-dir (read-only) and --allow-write-dir (writable, implies read)
#   * command execution is confined to --allow-cmd prefix whitelist
#
# Endpoints:
#   GET  /health
#   GET  /v1/sysinfo
#   GET  /v1/list?path=
#   GET  /v1/read?path=&tail_bytes=
#   POST /v1/write  {"path": "...", "content": "...",
#                    "append": false, "encoding": "text"|"base64"}
#   POST /v1/exec   {"cmd": "...", "timeout": 30}
#
# The console is interactive: type "help" at the bridge> prompt to
# add/remove allowed dirs at runtime (persisted in pc-bridge.allow).
#
# NOTE: like the Go version, --allow-cmd is a PREFIX check and commands run
# through the shell, so keep the whitelist tight (e.g. just "hdc").

import argparse
import json
import os
import platform
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
MAX_OUT = 2 * 1024 * 1024  # 2MB cap on command output


def log(*a):
    print(*a, flush=True)


def find_tailscale():
    """Locate the tailscale CLI (PATH, then default Windows install dir)."""
    for cand in ("tailscale",
                 r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            r = subprocess.run([cand, "version"], capture_output=True,
                               timeout=10)
            if r.returncode == 0:
                return cand
        except Exception:
            pass
    return None


def tailscale_ipv4(ts):
    r = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True,
                       timeout=10)
    if r.returncode != 0:
        raise RuntimeError("tailscale ip -4 failed: " +
                           r.stderr.strip()[:200])
    for line in r.stdout.splitlines():
        line = line.strip()
        if line:
            return line
    raise RuntimeError("no tailscale IPv4 address found")


def whois_allow(ts, src_ip, allow_nodes):
    """Return (ok, detail). detail is node name/tag or failure reason."""
    if not ts:
        return False, "tailscale CLI not available for whois check"
    try:
        r = subprocess.run([ts, "whois", src_ip], capture_output=True,
                           text=True, timeout=10)
    except Exception as e:
        return False, "whois error: %s" % e
    if r.returncode != 0:
        return False, "whois failed: " % (r.stderr.strip()[:120])
    node_name, tags = "", []
    for ln in r.stdout.splitlines():
        s = ln.strip()
        low = s.lower()
        if low.startswith("user:"):
            break  # User section follows Node; its Name: is the login, not the node
        if low.startswith("name:"):
            if not node_name:  # first Name: wins = the Node section's
                node_name = s[5:].strip()
        elif low.startswith("tags:"):
            tags = [t.strip()[4:] if t.strip().lower().startswith("tag:")
                    else t.strip()
                    for t in s[5:].split(",") if t.strip()]
    ident = [node_name] + tags
    for want in allow_nodes:
        if not want:
            continue
        wl = want.lower()
        for got in ident:
            gl = (got or "").lower()
            # exact match, or short name matching FQDN:
            # short name matches the FQDN prefix: node == node.<tailnet>.ts.net
            if gl == wl or gl.startswith(wl + "."):
                return True, node_name or want
    return False, "node %r not in allowlist" % (node_name or src_ip)


class Handler(BaseHTTPRequestHandler):
    server_version = "pc-bridge/" + VERSION

    # ---- helpers ----
    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def check_auth(self):
        cfg = self.server.cfg
        if cfg["token"]:
            auth = self.headers.get("Authorization", "")
            if auth == "Bearer " + cfg["token"]:
                return True, "token"
        src = self.client_address[0]
        ok, detail = whois_allow(cfg["ts"], src, cfg["allow_node"])
        log("%s %s from %s -> %s (%s)" %
            (self.command, self.path, src,
             "OK" if ok else "403", detail))
        return ok, detail

    def resolve_path(self, p, for_write=False):
        """Resolve p inside the dir whitelist; raise on violation.
        for_write=True confines to write dirs only; otherwise read dirs
        plus write dirs (write implies read)."""
        cfg = self.server.cfg
        if not p:
            raise ValueError("empty path")
        p = os.path.expandvars(os.path.expanduser(p))
        if not os.path.isabs(p):
            p = os.path.abspath(os.path.join(os.path.expanduser("~"), p))
        real = os.path.realpath(p)
        if for_write:
            bases = cfg["allow_write_dirs"]
        else:
            bases = cfg["allow_read_dirs"] + cfg["allow_write_dirs"]
        for base in bases:
            try:
                if os.path.commonpath([real, base]) == base:
                    return real
            except ValueError:
                continue
        raise PermissionError("path outside allowed dirs: %s" % p)

    # ---- routes ----
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        cfg = self.server.cfg

        if u.path == "/health":
            self.send_json(200, {"ok": True, "service": "pc-bridge",
                                 "version": VERSION,
                                 "hostname": platform.node(),
                                 "tailscale_ip": cfg["bind_ip"]})
            return

        ok, detail = self.check_auth()
        if not ok:
            self.send_json(403, {"ok": False, "error": "unauthorized",
                                 "detail": detail})
            return

        try:
            if u.path == "/v1/sysinfo":
                self.send_json(200, {"ok": True,
                                     "hostname": platform.node(),
                                     "os": platform.system(),
                                     "python": platform.python_version(),
                                     "tailscale_ip": cfg["bind_ip"]})
            elif u.path == "/v1/list":
                d = self.resolve_path(q.get("path", [""])[0])
                if not os.path.isdir(d):
                    raise NotADirectoryError("not a directory: %s" % d)
                items = []
                for name in sorted(os.listdir(d)):
                    full = os.path.join(d, name)
                    try:
                        st = os.stat(full)
                        items.append({"name": name,
                                      "dir": os.path.isdir(full),
                                      "size": st.st_size,
                                      "mtime": int(st.st_mtime)})
                    except OSError:
                        items.append({"name": name, "dir": False,
                                      "size": -1, "mtime": 0})
                self.send_json(200, {"ok": True, "path": d, "items": items})
            elif u.path == "/v1/read":
                f = self.resolve_path(q.get("path", [""])[0])
                tail = int(q.get("tail_bytes", ["65536"])[0] or 65536)
                tail = max(1, min(tail, MAX_OUT))
                with open(f, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    fh.seek(max(0, size - tail))
                    data = fh.read()
                text = data.decode("utf-8", errors="replace")
                self.send_json(200, {"ok": True, "path": f, "size": size,
                                     "truncated": size > len(data),
                                     "content": text})
            else:
                self.send_json(404, {"ok": False, "error": "not found"})
        except PermissionError as e:
            self.send_json(403, {"ok": False, "error": str(e)})
        except FileNotFoundError as e:
            self.send_json(404, {"ok": False, "error": str(e)})
        except Exception as e:
            self.send_json(500, {"ok": False,
                                 "error": "%s: %s" % (type(e).__name__, e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        cfg = self.server.cfg
        if u.path == "/v1/exec":
            self.handle_exec()
        elif u.path == "/v1/write":
            self.handle_write()
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def read_json_body(self, max_len=65536):
        length = int(self.headers.get("Content-Length", 0))
        if length > max_len:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def handle_exec(self):
        cfg = self.server.cfg
        ok, detail = self.check_auth()
        if not ok:
            self.send_json(403, {"ok": False, "error": "unauthorized",
                                 "detail": detail})
            return
        try:
            body = self.read_json_body()
            cmd = (body.get("cmd") or "").strip()
            timeout = int(body.get("timeout") or 30)
            timeout = max(1, min(timeout, 300))
            if not cmd:
                raise ValueError("empty cmd")
            # prefix whitelist, same semantics as the Go version
            # NOTE: commands run through the shell, so keep this list tight.
            if not any(cmd.startswith(p) for p in cfg["allow_cmd"] if p):
                raise PermissionError(
                    "command not in allowlist: %.60s" % cmd)
            proc = subprocess.run(cmd, shell=True, capture_output=True,
                                  timeout=timeout)
            out = (proc.stdout or b"") + (proc.stderr or b"")
            trunc = len(out) > MAX_OUT
            out = out[:MAX_OUT]
            self.send_json(200, {
                "ok": True, "exit_code": proc.returncode,
                "truncated": trunc,
                "output": out.decode("utf-8", errors="replace")})
        except subprocess.TimeoutExpired:
            self.send_json(504, {"ok": False, "error": "command timed out"})
        except PermissionError as e:
            self.send_json(403, {"ok": False, "error": str(e)})
        except Exception as e:
            self.send_json(500, {"ok": False,
                                 "error": "%s: %s" % (type(e).__name__, e)})

    def handle_write(self):
        cfg = self.server.cfg
        ok, detail = self.check_auth()
        if not ok:
            self.send_json(403, {"ok": False, "error": "unauthorized",
                                 "detail": detail})
            return
        try:
            body = self.read_json_body(max_len=10 * 1024 * 1024 + 65536)
            f = self.resolve_path(body.get("path") or "", for_write=True)
            if os.path.isdir(f):
                raise ValueError("refusing to write to a directory: %s" % f)
            content = body.get("content", "")
            if body.get("encoding") == "base64":
                import base64
                data = base64.b64decode(content)
            else:
                data = (content if isinstance(content, str)
                        else str(content)).encode("utf-8")
            if len(data) > 10 * 1024 * 1024:
                raise ValueError("content too large (10MB cap)")
            parent = os.path.dirname(f)
            if parent:
                os.makedirs(parent, exist_ok=True)
            mode = "ab" if body.get("append") else "wb"
            with open(f, mode) as fh:
                fh.write(data)
            self.send_json(200, {"ok": True, "path": f,
                                 "bytes": len(data),
                                 "append": bool(body.get("append"))})
        except PermissionError as e:
            self.send_json(403, {"ok": False, "error": str(e)})
        except FileNotFoundError as e:
            self.send_json(404, {"ok": False, "error": str(e)})
        except Exception as e:
            self.send_json(500, {"ok": False,
                                 "error": "%s: %s" % (type(e).__name__, e)})

    def log_message(self, *a):
        pass  # we log auth decisions ourselves


def parse_list(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SAVED_DIRS_FILE = os.path.join(SCRIPT_DIR, "pc-bridge.allow")


def normalize_dir(d):
    """Expand vars/user and resolve to an absolute path. Returns (path, err)."""
    p = os.path.realpath(os.path.expandvars(os.path.expanduser(d.strip())))
    if not p:
        return "", "empty path"
    if not os.path.isdir(p):
        return "", "not a directory: %s" % p
    return p, ""


def load_saved_dirs():
    """Dirs previously added via the console (persisted in pc-bridge.allow).

    Returns a list of (mode, path) with mode 'r' or 'w'. Plain lines
    without a marker (written by older versions) count as read."""
    entries = []
    try:
        with open(SAVED_DIRS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line[:2].upper() in ("R|", "W|"):
                    entries.append((line[0].lower(), line[2:]))
                else:
                    entries.append(("r", line))
    except FileNotFoundError:
        pass
    except OSError as e:
        log("warning: cannot read %s: %s" % (SAVED_DIRS_FILE, e))
    return entries


def save_dir(mode, p):
    try:
        with open(SAVED_DIRS_FILE, "a", encoding="utf-8") as f:
            f.write("%s|%s\n" % (mode.upper(), p))
    except OSError as e:
        log("warning: cannot persist allowed dir: %s" % e)


def unsave_dir(p):
    try:
        with open(SAVED_DIRS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return

    def line_path(ln):
        s = ln.strip()
        return s[2:] if s[:2].upper() in ("R|", "W|") else s

    keep = [ln for ln in lines
            if os.path.normcase(line_path(ln)) != os.path.normcase(p)]
    try:
        with open(SAVED_DIRS_FILE, "w", encoding="utf-8") as f:
            f.writelines(keep)
    except OSError as e:
        log("warning: cannot update %s: %s" % (SAVED_DIRS_FILE, e))


def cmd_allow_dir(cfg, arg, mode):
    p, err = normalize_dir(arg)
    if err:
        print("error: " + err)
        return
    key = "allow_write_dirs" if mode == "w" else "allow_read_dirs"
    cur = cfg[key]
    if os.path.normcase(p) in [os.path.normcase(x) for x in cur]:
        print("already allowed (%s): %s"
              % ("write" if mode == "w" else "read", p))
        return
    cfg[key] = cur + [p]  # atomic replace; request handlers
    save_dir(mode, p)     # keep iterating the old list safely
    print("allowed (%s, effective immediately, saved for next start):"
          % ("write" if mode == "w" else "read"))
    print("  " + p)


def cmd_disallow_dir(cfg, arg):
    p = os.path.realpath(os.path.expandvars(os.path.expanduser(arg.strip())))
    removed = []
    for key in ("allow_read_dirs", "allow_write_dirs"):
        cur = cfg[key]
        new = [x for x in cur if os.path.normcase(x) != os.path.normcase(p)]
        if len(new) != len(cur):
            cfg[key] = new
            removed.append(key)
    if not removed:
        print("not in allowlist: " + p)
        return
    unsave_dir(p)
    print("removed: " + p)


def console_loop(cfg, srv):
    print("")
    print("Console commands (type and press Enter):")
    print("  allow <dir>         add a READ-ONLY directory (takes effect now)")
    print("  allow-write <dir>   add a WRITABLE directory (takes effect now)")
    print("  disallow <dir>      remove a directory from the whitelist")
    print("  dirs                show currently allowed directories")
    print("  help                show this help")
    print("  quit                stop the bridge")
    while True:
        try:
            line = input("bridge> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            break
        if not line:
            continue
        parts = line.split(None, 1)
        cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if cmd in ("allow", "allow-read"):
            if not arg:
                print("usage: allow <dir>")
            else:
                cmd_allow_dir(cfg, arg, "r")
        elif cmd == "allow-write":
            if not arg:
                print("usage: allow-write <dir>")
            else:
                cmd_allow_dir(cfg, arg, "w")
        elif cmd == "disallow":
            if not arg:
                print("usage: disallow <dir>")
            else:
                cmd_disallow_dir(cfg, arg)
        elif cmd in ("dirs", "allowed"):
            print("  read-only:")
            for d in cfg["allow_read_dirs"]:
                print("    [R] " + d)
            print("  writable:")
            for d in cfg["allow_write_dirs"]:
                print("    [W] " + d)
        elif cmd == "help":
            print("  allow <dir> / allow-write <dir> / disallow <dir> /")
            print("  dirs / quit")
        elif cmd in ("quit", "exit"):
            break
        else:
            print("unknown command: %s (type 'help')" % cmd)
    srv.shutdown()


def main():
    ap = argparse.ArgumentParser(description="pc-bridge (python)")
    ap.add_argument("--allow-node", default="",
                    help="comma-separated Tailscale node names/tags")
    ap.add_argument("--token", default="", help="optional Bearer token")
    ap.add_argument("--allow-dir", default="",
                    help="comma-separated READ-ONLY dirs (default: user home)")
    ap.add_argument("--allow-write-dir", default="",
                    help="comma-separated WRITABLE dirs (default: none; "
                         "write implies read)")
    ap.add_argument("--allow-cmd", default="hdc",
                    help="comma-separated command prefixes")
    ap.add_argument("--bind", default="", help="override bind IP (testing)")
    ap.add_argument("--port", type=int, default=18743)
    args = ap.parse_args()

    allow_node = parse_list(args.allow_node)
    if not allow_node and not args.token:
        sys.exit("pc-bridge failed to start: must set --allow-node or "
                 "--token (auth cannot be empty)")

    ts = find_tailscale()
    if not ts and not args.bind:
        sys.exit("pc-bridge failed to start: tailscale CLI not found "
                 "(is Tailscale installed?)")

    bind_ip = args.bind or tailscale_ipv4(ts)

    if args.allow_dir:
        raw_read = [("r", d) for d in parse_list(args.allow_dir)]
    else:
        raw_read = [("r", os.path.expanduser("~"))]
    raw_write = [("w", d) for d in parse_list(args.allow_write_dir)]
    raw_entries = raw_read + raw_write + load_saved_dirs()  # console history
    allow_read_dirs, allow_write_dirs = [], []
    for mode, d in raw_entries:
        p, err = normalize_dir(d)
        if err:
            # a saved dir may point at a drive that is gone; skip, don't die
            log("warning: skipping allowed dir: %s" % err)
            continue
        target = allow_write_dirs if mode == "w" else allow_read_dirs
        if os.path.normcase(p) not in [os.path.normcase(x)
                                       for x in target]:
            target.append(p)
    if not allow_read_dirs and not allow_write_dirs:
        sys.exit("pc-bridge failed to start: no valid --allow-dir")

    cfg = {"allow_node": allow_node, "token": args.token,
           "allow_read_dirs": allow_read_dirs,
           "allow_write_dirs": allow_write_dirs,
           "allow_cmd": parse_list(args.allow_cmd),
           "ts": ts, "bind_ip": bind_ip}

    srv = ThreadingHTTPServer((bind_ip, args.port), Handler)
    srv.cfg = cfg
    srv.daemon_threads = True

    log("pc-bridge.py v%s started" % VERSION)
    log("  listening: http://%s:%d (Tailscale interface only)" %
        (bind_ip, args.port))
    log("  hostname: %s" % platform.node())
    log("  allowed nodes: %s" % ", ".join(allow_node))
    if args.token:
        log("  extra Bearer token auth: enabled")
    log("  read-only dirs: %s" % ("; ".join(allow_read_dirs) or "(none)"))
    log("  writable dirs: %s" % ("; ".join(allow_write_dirs) or "(none)"))
    log("  allowed cmd prefixes: %s" % ", ".join(cfg["allow_cmd"]))
    log("Close this window to stop the bridge.")
    if sys.stdin.isatty():
        # interactive console: server runs in background, this thread
        # takes commands (allow/disallow dirs at runtime)
        t = threading.Thread(target=srv.serve_forever,
                             kwargs={"poll_interval": 0.5}, daemon=True)
        t.start()
        try:
            console_loop(cfg, srv)
        except KeyboardInterrupt:
            pass
        t.join()
    else:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
