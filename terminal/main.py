"""
Invest-Pi · Web Terminal
WebSocket-basiertes Terminal mit PTY-Anbindung.
Auth via Passwort oder WebAuthn (Face ID / Fingerprint).
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import pty
import secrets
import select
import signal
import struct
import termios
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import webauthn
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    ResidentKeyRequirement,
    PublicKeyCredentialDescriptor,
)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes

ROOT = os.environ.get("ROOT_PATH", "")
app = FastAPI(title="Invest-Pi Terminal", root_path=ROOT)

STATIC = Path(__file__).parent / "static"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

PASSWORD_HASH = hashlib.sha256(b"pokepi2026").hexdigest()

RP_NAME = "Invest-Pi Terminal"


def _get_rp_id(request: Request) -> str:
    return request.headers.get("host", "localhost").split(":")[0]


def _get_origin(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", "http")
    host = request.headers.get("host", "localhost")
    return f"{proto}://{host}"

SESSIONS: dict[str, bool] = {}
WEBAUTHN_CREDS_FILE = DATA_DIR / "webauthn_creds.json"
WEBAUTHN_CHALLENGES: dict[str, bytes] = {}


def _load_creds() -> list[dict]:
    if WEBAUTHN_CREDS_FILE.exists():
        return json.loads(WEBAUTHN_CREDS_FILE.read_text())
    return []


def _save_creds(creds: list[dict]):
    WEBAUTHN_CREDS_FILE.write_text(json.dumps(creds, indent=2))


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = True
    return token


def verify_session(request: Request):
    token = request.cookies.get("terminal_session", "")
    if not token or token not in SESSIONS:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/login")
async def login_page():
    creds = _load_creds()
    has_passkey = len(creds) > 0
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terminal Login</title>
<style>
body{{background:#0f1117;color:#e6e8ee;font-family:-apple-system,system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0}}
.box{{background:#1a1d27;border:1px solid #2a2d3a;border-radius:16px;padding:36px 32px;width:360px;text-align:center}}
h2{{margin:0 0 6px;font-size:20px;font-weight:600}}
.sub{{color:#8b90a0;font-size:12.5px;margin-bottom:24px}}
input{{width:100%;padding:12px 14px;border-radius:10px;border:1px solid #2a2d3a;background:#1f2330;color:#e6e8ee;font-size:14px;margin-bottom:12px;outline:none;box-sizing:border-box}}
input:focus{{border-color:#3b82f6}}
.btn{{width:100%;padding:11px;border-radius:10px;border:none;font-size:14px;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px}}
.btn-primary{{background:#3b82f6;color:white;margin-bottom:8px}}
.btn-primary:hover{{background:#5491ff}}
.btn-bio{{background:#1f2330;color:#e6e8ee;border:1px solid #2a2d3a;margin-bottom:8px}}
.btn-bio:hover{{background:#2a2d3a}}
.btn-bio svg{{width:20px;height:20px}}
.divider{{display:flex;align-items:center;gap:12px;margin:16px 0;color:#555;font-size:11px}}
.divider::before,.divider::after{{content:'';flex:1;border-top:1px solid #2a2d3a}}
.err{{color:#ef4655;font-size:12px;margin-top:8px;display:none}}
.setup{{margin-top:16px;padding-top:16px;border-top:1px solid #2a2d3a}}
.btn-setup{{background:transparent;color:#8b90a0;border:1px dashed #2a2d3a;font-size:12px;padding:8px}}
.btn-setup:hover{{color:#e6e8ee;border-color:#555}}
.hidden{{display:none}}
</style></head><body>
<div class="box">
<h2>🖥️ Terminal</h2>
<p class="sub">Invest-Pi · Raspberry Pi 5</p>
<form onsubmit="return loginPw()">
<input type="password" id="pw" placeholder="Passwort" autofocus>
<button type="submit" class="btn btn-primary">Anmelden</button>
</form>
<div id="bio-section" class="{'hidden' if not has_passkey else ''}">
<div class="divider">oder</div>
<button class="btn btn-bio" onclick="loginBio()">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 3.08-1.29 1.94-3.5 3.22-6 3.22z"/></svg>
Face ID / Biometrie
</button>
</div>
<div class="err" id="err"></div>
<div class="setup">
<button class="btn btn-setup" onclick="registerBio()">+ Face ID einrichten</button>
</div>
</div>
<script>
const ROOT = '{ROOT}';
function showErr(msg) {{
    const e = document.getElementById('err');
    e.textContent = msg;
    e.style.display = 'block';
    setTimeout(() => e.style.display = 'none', 4000);
}}
function loginPw() {{
    const pw = document.getElementById('pw').value.trim();
    if (!pw) return false;
    fetch(ROOT + '/auth/password', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{password: pw}})
    }}).then(r => {{
        if (r.ok) location.href = ROOT + '/';
        else showErr('Falsches Passwort');
    }}).catch(() => showErr('Verbindungsfehler'));
    return false;
}}
function bufToB64(buf) {{
    return btoa(String.fromCharCode(...new Uint8Array(buf)))
        .replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
}}
function b64ToBuf(b64) {{
    b64 = b64.replace(/-/g,'+').replace(/_/g,'/');
    while(b64.length%4) b64+='=';
    const bin = atob(b64);
    const buf = new Uint8Array(bin.length);
    for(let i=0;i<bin.length;i++) buf[i]=bin.charCodeAt(i);
    return buf.buffer;
}}
async function registerBio() {{
    try {{
        const opts = await fetch(ROOT + '/auth/webauthn/register-options').then(r => r.json());
        opts.challenge = b64ToBuf(opts.challenge);
        opts.user.id = b64ToBuf(opts.user.id);
        if (opts.excludeCredentials) {{
            opts.excludeCredentials = opts.excludeCredentials.map(c => ({{...c, id: b64ToBuf(c.id)}}));
        }}
        const cred = await navigator.credentials.create({{publicKey: opts}});
        const resp = await fetch(ROOT + '/auth/webauthn/register-complete', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
                id: cred.id,
                rawId: bufToB64(cred.rawId),
                response: {{
                    attestationObject: bufToB64(cred.response.attestationObject),
                    clientDataJSON: bufToB64(cred.response.clientDataJSON)
                }},
                type: cred.type
            }})
        }});
        if (resp.ok) {{
            document.getElementById('bio-section').classList.remove('hidden');
            showErr('');
            const e = document.getElementById('err');
            e.textContent = '✓ Face ID eingerichtet!';
            e.style.color = '#34d399';
            e.style.display = 'block';
            setTimeout(() => {{ e.style.display='none'; e.style.color=''; }}, 3000);
        }} else showErr('Registrierung fehlgeschlagen');
    }} catch(e) {{ showErr('Biometrie nicht verfügbar: ' + e.message); }}
}}
async function loginBio() {{
    try {{
        const opts = await fetch(ROOT + '/auth/webauthn/auth-options').then(r => r.json());
        opts.challenge = b64ToBuf(opts.challenge);
        if (opts.allowCredentials) {{
            opts.allowCredentials = opts.allowCredentials.map(c => ({{...c, id: b64ToBuf(c.id)}}));
        }}
        const assertion = await navigator.credentials.get({{publicKey: opts}});
        const resp = await fetch(ROOT + '/auth/webauthn/auth-complete', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
                id: assertion.id,
                rawId: bufToB64(assertion.rawId),
                response: {{
                    authenticatorData: bufToB64(assertion.response.authenticatorData),
                    clientDataJSON: bufToB64(assertion.response.clientDataJSON),
                    signature: bufToB64(assertion.response.signature),
                    userHandle: assertion.response.userHandle ? bufToB64(assertion.response.userHandle) : null
                }},
                type: assertion.type
            }})
        }});
        if (resp.ok) location.href = ROOT + '/';
        else showErr('Authentifizierung fehlgeschlagen');
    }} catch(e) {{ showErr('Biometrie fehlgeschlagen: ' + e.message); }}
}}
</script></body></html>""")


@app.post("/auth/password")
async def auth_password(request: Request):
    body = await request.json()
    pw = body.get("password", "")
    if hashlib.sha256(pw.encode()).hexdigest() != PASSWORD_HASH:
        raise HTTPException(status_code=401, detail="Wrong password")
    token = _create_session()
    response = JSONResponse({"ok": True})
    response.set_cookie("terminal_session", token, max_age=31536000, httponly=True, samesite="strict", path="/")
    return response


@app.get("/auth/webauthn/register-options")
async def webauthn_register_options(request: Request):
    rp_id = _get_rp_id(request)
    creds = _load_creds()
    exclude = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
        for c in creds
    ]
    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=b"investpi-user",
        user_name="investpi",
        user_display_name="Invest-Pi User",
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.PREFERRED,
            resident_key=ResidentKeyRequirement.PREFERRED,
        ),
        exclude_credentials=exclude,
    )
    WEBAUTHN_CHALLENGES["register"] = options.challenge
    opts_dict = json.loads(webauthn.options_to_json(options))
    return JSONResponse(opts_dict)


@app.post("/auth/webauthn/register-complete")
async def webauthn_register_complete(request: Request):
    body = await request.json()
    challenge = WEBAUTHN_CHALLENGES.pop("register", None)
    if not challenge:
        raise HTTPException(400, "No pending challenge")

    rp_id = _get_rp_id(request)
    origin = _get_origin(request)
    try:
        verification = webauthn.verify_registration_response(
            credential=body,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=False,
        )
    except Exception as e:
        raise HTTPException(400, f"Verification failed: {e}")

    creds = _load_creds()
    creds.append({
        "credential_id": bytes_to_base64url(verification.credential_id),
        "public_key": bytes_to_base64url(verification.credential_public_key),
        "sign_count": verification.sign_count,
        "name": f"Device {len(creds) + 1}",
    })
    _save_creds(creds)
    return JSONResponse({"ok": True})


@app.get("/auth/webauthn/auth-options")
async def webauthn_auth_options(request: Request):
    rp_id = _get_rp_id(request)
    creds = _load_creds()
    if not creds:
        raise HTTPException(400, "No passkeys registered")

    allow = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
        for c in creds
    ]
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    WEBAUTHN_CHALLENGES["auth"] = options.challenge
    opts_dict = json.loads(webauthn.options_to_json(options))
    return JSONResponse(opts_dict)


@app.post("/auth/webauthn/auth-complete")
async def webauthn_auth_complete(request: Request):
    body = await request.json()
    challenge = WEBAUTHN_CHALLENGES.pop("auth", None)
    if not challenge:
        raise HTTPException(400, "No pending challenge")

    creds = _load_creds()
    cred_id = body.get("id", "")
    matched = next((c for c in creds if c["credential_id"] == cred_id), None)
    if not matched:
        raise HTTPException(401, "Unknown credential")

    rp_id = _get_rp_id(request)
    origin = _get_origin(request)
    try:
        verification = webauthn.verify_authentication_response(
            credential=body,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=base64url_to_bytes(matched["public_key"]),
            credential_current_sign_count=matched["sign_count"],
            require_user_verification=False,
        )
    except Exception as e:
        raise HTTPException(401, f"Auth failed: {e}")

    matched["sign_count"] = verification.new_sign_count
    _save_creds(creds)

    token = _create_session()
    response = JSONResponse({"ok": True})
    response.set_cookie("terminal_session", token, max_age=31536000, httponly=True, samesite="strict", path="/")
    return response


@app.get("/")
async def index(request: Request):
    token = request.cookies.get("terminal_session", "")
    if not token or token not in SESSIONS:
        return RedirectResponse(f"{ROOT}/login")
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class PtyProcess:
    def __init__(self, cols: int = 120, rows: int = 40):
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["HOME"] = os.path.expanduser("~")
        env["USER"] = os.environ.get("USER", "pi")

        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(os.environ.get("WORKDIR", env["HOME"]))
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
    cookie_token = ws.cookies.get("terminal_session", "")
    valid = (cookie_token in SESSIONS) or (token in SESSIONS)
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
async def system_info(request: Request, _=Depends(verify_session)):
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
