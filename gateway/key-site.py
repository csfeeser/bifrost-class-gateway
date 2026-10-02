#!/usr/bin/env python3
"""
key-site.py -- a password-protected page listing one copy-paste block per
student, served on port 2225 (this VM's aux2 address).

Each block is a single command that writes that student's ~/.bifrost-env
(their virtual key and the gateway URL). Clicking a block copies it and grays
it out; which blocks are grayed out is saved on the server in copied.json, so
it survives reloads and works from any browser.

Reads roster.json written by make-student-keys.py. Re-running that script
updates this page on the next reload -- no restart needed.

Environment:
  KEYSITE_USERNAME, KEYSITE_PASSWORD  -- the login for this page (required)
  KEYSITE_DATA_DIR                    -- folder holding roster.json (default /data)
  KEYSITE_PORT                        -- port to listen on (default 8000)
"""

import base64
import hmac
import html
import json
import os
import shlex
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USERNAME = os.environ["KEYSITE_USERNAME"]
PASSWORD = os.environ["KEYSITE_PASSWORD"]
DATA_DIR = os.environ.get("KEYSITE_DATA_DIR", "/data")
PORT = int(os.environ.get("KEYSITE_PORT", "8000"))
ROSTER = os.path.join(DATA_DIR, "roster.json")
COPIED = os.path.join(DATA_DIR, "copied.json")

lock = threading.Lock()


def load_json(path, default):
	try:
		with open(path) as f:
			return json.load(f)
	except FileNotFoundError:
		return default


def save_copied(copied):
	temp = COPIED + ".tmp"
	with open(temp, "w") as f:
		json.dump(copied, f, indent=2)
	os.replace(temp, COPIED)


def command_for(student):
	# One line, so a single paste into the student's terminal does everything.
	lines = [f"BIFROST_API_KEY={student['key']}", f"BIFROST_BASE_URL={student['gateway_url']}"]
	return (
		"printf '%s\\n' " + " ".join(shlex.quote(line) for line in lines)
		+ " > ~/.bifrost-env && chmod 600 ~/.bifrost-env"
		+ f" && echo {shlex.quote(student['name'] + ' key saved')}"
	)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Student Keys</title>
<style>
  :root {
    --bg: #f6f7f9; --card: #ffffff; --text: #1d2330; --muted: #5d6676; --border: #d9dde4;
    --code-bg: #0f1724; --code-text: #e6edf6; --accent: #2563eb; --done: #16a34a; --done-bg: #e8f5ec;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #11151c; --card: #1a2029; --text: #e6e9ef; --muted: #9aa3b2; --border: #2c3442;
      --code-bg: #0a0e14; --code-text: #dbe4ef; --accent: #60a5fa; --done: #4ade80; --done-bg: #16241b;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.5 system-ui, sans-serif; }
  main { max-width: 980px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 1.6rem; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 20px; }
  .steps { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; margin-bottom: 20px; }
  .steps ol { margin: 6px 0 0; padding-left: 22px; }
  .progress { font-weight: 600; margin: 0 0 12px; }
  .bar { height: 8px; background: var(--border); border-radius: 4px; overflow: hidden; margin-bottom: 20px; }
  .bar > div { height: 100%; background: var(--done); width: 0; transition: width .2s; }
  .student { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; }
  .head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
  .name { font-weight: 700; font-size: 1.05rem; }
  .badge { font-size: .85rem; font-weight: 600; color: var(--done); background: var(--done-bg); border-radius: 999px; padding: 1px 10px; display: none; }
  .undo { margin-left: auto; font: inherit; font-size: .85rem; color: var(--muted); background: none; border: 1px solid var(--border); border-radius: 6px; padding: 2px 10px; cursor: pointer; display: none; }
  pre { margin: 0; background: var(--code-bg); color: var(--code-text); border-radius: 8px; padding: 12px 14px;
        font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; word-break: break-all;
        cursor: pointer; border: 2px solid transparent; }
  pre:hover { border-color: var(--accent); }
  .hint { font-size: .85rem; color: var(--muted); margin-top: 6px; }
  .student.copied pre { opacity: .35; filter: grayscale(1); }
  .student.copied .badge, .student.copied .undo { display: inline-block; }
  .student.copied .hint { display: none; }
  .empty { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; }
</style>
</head>
<body>
<main>
  <h1>Student Keys</h1>
  <p class="sub">Gateway: __GATEWAY__</p>
  <div class="steps">
    <strong>For each student VM:</strong>
    <ol>
      <li>Open a terminal on the student's VM.</li>
      <li>Click the next block that is <em>not</em> grayed out. It copies itself and turns gray.</li>
      <li>Paste it into the student's terminal and press Enter. You should see <code>student-NN key saved</code>.</li>
    </ol>
  </div>
  <p class="progress" id="progress"></p>
  <div class="bar"><div id="bar"></div></div>
  <div id="list"></div>
</main>
<script>
const DATA = __DATA__;
const list = document.getElementById("list");

function render() {
  if (!DATA.students.length) {
    list.innerHTML = '<div class="empty">No students yet. Run make-student-keys.py on the gateway VM, then reload this page.</div>';
  }
  for (const s of DATA.students) {
    const card = document.createElement("div");
    card.className = "student" + (DATA.copied[s.key] ? " copied" : "");
    card.innerHTML = `
      <div class="head">
        <span class="name"></span>
        <span class="badge"></span>
        <button class="undo" type="button">Undo</button>
      </div>
      <pre></pre>
      <div class="hint">Click to copy</div>`;
    card.querySelector(".name").textContent = s.name;
    card.querySelector("pre").textContent = s.command;
    card.querySelector(".badge").textContent = DATA.copied[s.key] ? "Copied " + DATA.copied[s.key] : "";
    card.querySelector("pre").addEventListener("click", () => copy(s, card));
    card.querySelector(".undo").addEventListener("click", () => mark(s, card, false));
    list.appendChild(card);
  }
  updateProgress();
}

function updateProgress() {
  const done = DATA.students.filter(s => DATA.copied[s.key]).length;
  const total = DATA.students.length;
  document.getElementById("progress").textContent = `${done} of ${total} copied`;
  document.getElementById("bar").style.width = total ? (100 * done / total) + "%" : "0";
}

async function copy(s, card) {
  try {
    await navigator.clipboard.writeText(s.command);
  } catch {
    // Fallback for browsers that block the clipboard API.
    const area = document.createElement("textarea");
    area.value = s.command;
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    if (!ok) { alert("Couldn't copy automatically. Select the text and press Ctrl+C."); return; }
  }
  mark(s, card, true);
}

async function mark(s, card, copied) {
  const response = await fetch("copied", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: s.key, copied }),
  });
  if (!response.ok) { alert("Couldn't save. Reload the page and try again."); return; }
  DATA.copied = await response.json();
  card.classList.toggle("copied", !!DATA.copied[s.key]);
  card.querySelector(".badge").textContent = DATA.copied[s.key] ? "Copied " + DATA.copied[s.key] : "";
  updateProgress();
}

render();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
	def authorized(self):
		header = self.headers.get("Authorization", "")
		if header.startswith("Basic "):
			try:
				user, _, password = base64.b64decode(header[6:]).decode().partition(":")
			except Exception:
				return False
			return hmac.compare_digest(user, USERNAME) and hmac.compare_digest(password, PASSWORD)
		return False

	def send(self, status, body, content_type):
		data = body.encode()
		self.send_response(status)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(data)))
		self.send_header("Cache-Control", "no-store")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.send_header("Referrer-Policy", "no-referrer")
		self.end_headers()
		self.wfile.write(data)

	def deny(self):
		self.send_response(401)
		self.send_header("WWW-Authenticate", 'Basic realm="Student Keys", charset="UTF-8"')
		self.send_header("Content-Length", "0")
		self.end_headers()

	def do_GET(self):
		if self.path == "/health":
			return self.send(200, "OK", "text/plain")
		if not self.authorized():
			return self.deny()
		if self.path not in ("/", "/index.html"):
			return self.send(404, "Not found", "text/plain")
		roster = load_json(ROSTER, {"gateway_url": "(none yet)", "students": []})
		with lock:
			copied = load_json(COPIED, {})
		students = [{"name": s["name"], "key": s["key"], "command": command_for(s)} for s in roster["students"]]
		data = json.dumps({"students": students, "copied": copied}).replace("<", "\\u003c")
		page = PAGE.replace("__GATEWAY__", html.escape(roster.get("gateway_url", ""))).replace("__DATA__", data)
		self.send(200, page, "text/html; charset=utf-8")

	def do_POST(self):
		if not self.authorized():
			return self.deny()
		if self.path != "/copied":
			return self.send(404, "Not found", "text/plain")
		try:
			body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
			key, copied = body["key"], bool(body["copied"])
		except Exception:
			return self.send(400, "Bad request", "text/plain")
		known = {s["key"] for s in load_json(ROSTER, {"students": []})["students"]}
		if key not in known:
			return self.send(404, "Unknown student key", "text/plain")
		with lock:
			state = load_json(COPIED, {})
			if copied:
				state[key] = datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")
			else:
				state.pop(key, None)
			save_copied(state)
		self.send(200, json.dumps(state), "application/json")

	def log_message(self, fmt, *args):
		# One line per request, without echoing anything sensitive.
		print(f"{self.address_string()} {self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)


if __name__ == "__main__":
	print(f"Student key site listening on port {PORT}, data in {DATA_DIR}", flush=True)
	ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
