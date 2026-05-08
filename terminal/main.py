"""
Invest-Pi · Web Terminal
WebSocket-basiertes Terminal mit PTY-Anbindung.
Auth via TERMINAL_TOKEN in .env (Bearer-Token oder Query-Param).
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import os
import pty
import secrets
import select
import signal
import struct
import termios
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Invest-Pi Terminal")

STATIC = Path(__file__).parent / "static"
TOKEN = os.environ.get("TERMINAL_TOKEN", "")

if not TOKEN:
    TOKEN = secrets.token_urlsafe(32)
    print(f"\n⚠️  Kein TERMINAL_TOKEN gesetzt! Generierter Token:\n    {TOKEN}\n")


def verify_token(request: Request):
    auth = request.headers.get("Authorization", "")
    cookie_token = request.cookies.get("terminal_token", "")
    query_token = request.query_params.get("token", "")

    valid = False
    for candidate in [auth.replace("Bearer ", ""), cookie_token, query_token]:
        if candidate and hmac.compare_digest(candidate, TOKEN):
            valid = True
            break

    if not valid:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/login")
async def login_page():
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Terminal Login</title>
<style>
body{background:#0f1117;color:#e6e8ee;font-family:-apple-system,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0}
.box{background:#1a1d27;border:1px solid #2a2d3a;border-radius:14px;padding:32px;width:340px;text-align:center}
h2{margin-bottom:16px;font-size:18px}
input{width:100%;padding:10px 14px;border-radius:10px;border:1px solid #2a2d3a;background:#1f2330;color:#e6e8ee;font-size:14px;margin-bottom:12px;outline:none}
input:focus{border-color:#3b82f6}
button{width:100%;padding:10px;border-radius:10px;border:none;background:#3b82f6;color:white;font-size:14px;font-weight:600;cursor:pointer}
button:hover{background:#5491ff}
.err{color:#ef4655;font-size:12px;margin-top:8px;display:none}
</style></head><body>
<div class="box">
<h2>Invest-Pi Terminal</h2>
<form onsubmit="return tryLogin()">
<input type="password" id="tok" placeholder="Token eingeben..." autofocus>
<button type="submit">Verbinden</button>
</form>
<div class="err" id="err">Token ungültig</div>
</div>
<script>
function tryLogin(){
    const t=document.getElementById('tok').value.trim();
    if(!t)return false;
    document.cookie='terminal_token='+t+';path=/;max-age=86400;SameSite=Strict';
    fetch('/api/system-info',{headers:{'Authorization':'Bearer '+t}})
        .then(r=>{if(r.ok)location.href='/';else throw 0})
        .catch(()=>{document.getElementById('err').style.display='block'});
    return false;
}
</script></body></html>""")


@app.get("/")
async def index(request: Request):
    cookie_token = request.cookies.get("terminal_token", "")
    query_token = request.query_params.get("token", "")

    valid = False
    for candidate in [cookie_token, query_token]:
        if candidate and hmac.compare_digest(candidate, TOKEN):
            valid = True
            break

    if not valid:
        return RedirectResponse("/login")

    response = FileResponse(STATIC / "index.html")
    if query_token and hmac.compare_digest(query_token, TOKEN):
        response.set_cookie("terminal_token", query_token, max_age=86400, httponly=True, samesite="strict")
    return response


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class PtyProcess:
    def __init__(self, cols: int = 120, rows: int = 40):
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["HOME"] = os.path.expanduser("~investpi")
        env["USER"] = "investpi"

        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(env["HOME"] + "/invest-pi")
            os.execvpe("/bin/bash", ["/bin/bash", "--login"], env)
        else:
            self._set_size(cols, rows)

    def _set_size(self, cols: int, rows: int):
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)

    def resize(self, cols: int, rows: int):
        self._set_size(cols, rows)
        os.kill(self.pid, signal.SIGWINCH)

    def write(self, data: bytes):
        os.write(self.fd, data)

    def read(self, size: int = 4096) -> bytes | None:
        ready, _, _ = select.select([self.fd], [], [], 0.02)
        if ready:
            try:
                return os.read(self.fd, size)
            except OSError:
                return None
        return b""

    def terminate(self):
        try:
            os.kill(self.pid, signal.SIGHUP)
            os.waitpid(self.pid, os.WNOHANG)
        except (OSError, ChildProcessError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


@app.websocket("/ws/terminal")
async def terminal_ws(ws: WebSocket, token: str = Query(default="")):
    cookie_token = ws.cookies.get("terminal_token", "")
    valid = False
    for candidate in [token, cookie_token]:
        if candidate and hmac.compare_digest(candidate, TOKEN):
            valid = True
            break
    if not valid:
        await ws.close(code=4001, reason="Unauthorized")
        return
    await ws.accept()

    cols = 120
    rows = 40
    try:
        init = await asyncio.wait_for(ws.receive_json(), timeout=5)
        cols = init.get("cols", 120)
        rows = init.get("rows", 40)
    except Exception:
        pass

    proc = PtyProcess(cols=cols, rows=rows)

    async def read_pty():
        loop = asyncio.get_event_loop()
        try:
            while True:
                data = await loop.run_in_executor(None, proc.read)
                if data is None:
                    await ws.close(1000, "process exited")
                    break
                if data:
                    await ws.send_bytes(data)
                else:
                    await asyncio.sleep(0.01)
        except (WebSocketDisconnect, Exception):
            pass

    read_task = asyncio.create_task(read_pty())

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.receive":
                if "bytes" in msg and msg["bytes"]:
                    proc.write(msg["bytes"])
                elif "text" in msg and msg["text"]:
                    import json
                    try:
                        payload = json.loads(msg["text"])
                        if payload.get("type") == "resize":
                            proc.resize(payload["cols"], payload["rows"])
                        elif payload.get("type") == "input":
                            proc.write(payload["data"].encode())
                    except (json.JSONDecodeError, KeyError):
                        proc.write(msg["text"].encode())
            elif msg["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        read_task.cancel()
        proc.terminate()


@app.get("/api/system-info")
async def system_info(request: Request, _=Depends(verify_token)):
    import subprocess
    import psutil

    info = {}
    try:
        info["hostname"] = os.uname().nodename
        info["cpu_pct"] = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        info["ram_used"] = f"{mem.used / 1e9:.1f}G"
        info["ram_total"] = f"{mem.total / 1e9:.1f}G"
        info["ram_pct"] = mem.percent
        try:
            temp = psutil.sensors_temperatures()
            if "cpu_thermal" in temp:
                info["cpu_temp"] = temp["cpu_thermal"][0].current
        except Exception:
            pass
        uptime_s = int(psutil.boot_time())
        import time
        up = int(time.time()) - uptime_s
        days = up // 86400
        hours = (up % 86400) // 3600
        mins = (up % 3600) // 60
        info["uptime"] = f"{days}d {hours:02d}:{mins:02d}"
        info["load"] = os.getloadavg()[0]
    except Exception as e:
        info["error"] = str(e)

    try:
        ip_result = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=3
        )
        info["ip"] = ip_result.stdout.strip().split()[0] if ip_result.stdout.strip() else "?"
    except Exception:
        info["ip"] = "?"

    try:
        result = subprocess.run(
            ["systemctl", "list-timers", "--no-pager", "--plain"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l for l in result.stdout.splitlines() if "invest-pi" in l]
        info["active_timers"] = len(lines)
    except Exception:
        info["active_timers"] = 0

    return info


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8022)
