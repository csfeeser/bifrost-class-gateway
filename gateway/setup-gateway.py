#!/usr/bin/env python3
"""
setup-gateway.py -- turn this VM into the class's Bifrost gateway.

Run it on the VM that will serve the class (as the student user, not root):

    python3 setup-gateway.py

The only thing it needs is the Anthropic API key. Put it in ~/bifrost/.env as
ANTHROPIC_API_KEY=sk-ant-... beforehand, or the script will ask for it.

What it does, in this order (so nothing is ever reachable unprotected):
  1. Generates the Bifrost admin login and the key-page login, if missing.
  2. Starts Bifrost reachable ONLY from this VM, and turns on its admin login
     and "virtual key required" setting.
  3. Checks both are on, then publishes Bifrost on port 2224 (aux1).
  4. Starts the student key page on port 2225 (aux2).
  5. Checks everything through the public aux1/aux2 addresses.

Safe to run again: it keeps existing logins, keys, and data.
"""

import getpass
import http.cookiejar
import json
import os
import secrets
import shutil
import socket
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request

BIFROST_IMAGE = "maximhq/bifrost:v2.2.4"
PYTHON_IMAGE = "python:3.13-alpine"
GATEWAY_CONTAINER = "bifrost"
KEYSITE_CONTAINER = "bifrost-keysite"
GATEWAY_PORT = 2224   # aux1
KEYSITE_PORT = 2225   # aux2
LOCAL_PORT = 8090     # admin API, reachable only from this VM
MODEL = "claude-haiku-4-5-20251001"

# Lab VMs are published as https://aux1-<host>-lab-<id>.<AUX_DOMAIN> (port 2224)
# and https://aux2-... (port 2225). Override with the AUX_DOMAIN environment variable.
AUX_DOMAIN = os.environ.get("AUX_DOMAIN", "lms-us-east-1.alta3.com")

HOME = os.path.expanduser("~")
BIFROST_DIR = os.path.join(HOME, "bifrost")
ENV_FILE = os.path.join(BIFROST_DIR, ".env")
KEYSITE_ENV = os.path.join(BIFROST_DIR, "keysite.env")
DATA_DIR = os.path.join(BIFROST_DIR, "data")
KEYS_DIR = os.path.join(BIFROST_DIR, "student-keys")
BIN_DIR = os.path.join(BIFROST_DIR, "bin")
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = f"http://localhost:{LOCAL_PORT}"


def log(message):
	print(f">>> {message}", flush=True)


def die(message):
	print(f"ERROR: {message}", file=sys.stderr, flush=True)
	sys.exit(1)


def run(*args, check=True):
	result = subprocess.run(args, capture_output=True, text=True)
	if check and result.returncode != 0:
		die(f"`{' '.join(args)}` failed:\n{result.stderr.strip() or result.stdout.strip()}")
	return result


def write_private(path, text):
	temp = path + ".tmp"
	fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(fd, "w") as f:
		f.write(text)
	os.replace(temp, path)


def read_env(path):
	values = {}
	if os.path.exists(path):
		for line in open(path).read().replace("\r", "").splitlines():
			line = line.strip()
			if line and not line.startswith("#") and "=" in line:
				name, value = line.split("=", 1)
				values[name.strip()] = value.strip().strip("\"'")
	return values


def strong_password(length=24):
	# Meets Bifrost's rules: 12+ chars, upper, lower, digit, special.
	symbols = "-_.@%+"
	alphabet = string.ascii_letters + string.digits + symbols
	while True:
		password = "".join(secrets.choice(alphabet) for _ in range(length))
		if (any(c.isupper() for c in password) and any(c.islower() for c in password)
				and any(c.isdigit() for c in password) and any(c in symbols for c in password)):
			return password


def http_call(method, url, body=None, opener=None, headers=None, timeout=30):
	data = json.dumps(body).encode() if body is not None else None
	request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
	try:
		with (opener or urllib.request.build_opener()).open(request, timeout=timeout) as response:
			return response.status, response.read().decode(errors="replace")
	except urllib.error.HTTPError as error:
		return error.code, error.read().decode(errors="replace")
	except (urllib.error.URLError, OSError) as error:
		return 0, str(error)


def wait_for_health(url, seconds=60):
	for _ in range(seconds):
		if http_call("GET", url + "/health", timeout=5)[0] == 200:
			return True
		time.sleep(1)
	return False


def aux_urls():
	# https://auxN-<host>-lab-<id>.<AUX_DOMAIN>, where `hostname -f` is "<host>.<id>".
	fqdn = socket.getfqdn()
	if "." not in fqdn:
		return None, None
	host, vm_id = fqdn.split(".", 1)
	return (f"https://aux1-{host}-lab-{vm_id}.{AUX_DOMAIN}",
	        f"https://aux2-{host}-lab-{vm_id}.{AUX_DOMAIN}")


def start_gateway(publish):
	run("docker", "rm", "-f", GATEWAY_CONTAINER, check=False)
	ports = ["-p", f"127.0.0.1:{LOCAL_PORT}:8080"]
	if publish:
		ports += ["-p", f"{GATEWAY_PORT}:8080"]
	run("docker", "run", "-d", "--name", GATEWAY_CONTAINER, "--restart", "unless-stopped",
	    "--user", f"{os.getuid()}:{os.getgid()}", "--env-file", ENV_FILE,
	    *ports, "-v", f"{DATA_DIR}:/app/data", BIFROST_IMAGE)
	if not wait_for_health(LOCAL):
		logs = run("docker", "logs", "--tail", "20", GATEWAY_CONTAINER, check=False)
		die(f"Bifrost didn't start. Last log lines:\n{logs.stdout}{logs.stderr}")


def main():
	if os.geteuid() == 0:
		die("Run this as your normal user, not with sudo or as root.")
	if not shutil.which("docker"):
		die("Docker isn't installed on this VM.")
	if run("docker", "info", check=False).returncode != 0:
		die("Your user can't run Docker. (It needs to be in the 'docker' group.)")
	if not aux_urls()[0]:
		die("Couldn't work out this VM's aux1/aux2 addresses from its hostname (`hostname -f`).")
	aux1, aux2 = aux_urls()

	# ---- 1. Secrets ---------------------------------------------------------

	os.makedirs(BIFROST_DIR, mode=0o700, exist_ok=True)
	env = read_env(ENV_FILE)

	if not env.get("ANTHROPIC_API_KEY"):
		log(f"No ANTHROPIC_API_KEY in {ENV_FILE}.")
		key = getpass.getpass("Paste the Anthropic API key (input is hidden), then press Enter: ").strip()
		if not key:
			die("No key entered.")
		env["ANTHROPIC_API_KEY"] = key
	if not env["ANTHROPIC_API_KEY"].startswith("sk-ant-"):
		die("ANTHROPIC_API_KEY doesn't look like an Anthropic key (they start with sk-ant-).")

	generated = []
	if not env.get("BIFROST_SETUP_TOKEN"):
		env["BIFROST_SETUP_TOKEN"] = secrets.token_hex(24)
	if not env.get("BIFROST_ADMIN_USERNAME"):
		env["BIFROST_ADMIN_USERNAME"] = "bifrost-admin"
	if not env.get("BIFROST_ADMIN_PASSWORD"):
		env["BIFROST_ADMIN_PASSWORD"] = strong_password()
		generated.append("Bifrost admin login")
	# Rewritten every run: normalizes line endings/quotes and keeps it private.
	write_private(ENV_FILE, "".join(f"{name}={value}\n" for name, value in env.items()))

	keysite = read_env(KEYSITE_ENV)
	if not keysite.get("KEYSITE_PASSWORD"):
		alphabet = string.ascii_letters + string.digits
		keysite = {
			"KEYSITE_USERNAME": "keys",
			"KEYSITE_PASSWORD": "-".join("".join(secrets.choice(alphabet) for _ in range(6)) for _ in range(3)),
		}
		write_private(KEYSITE_ENV, "".join(f"{name}={value}\n" for name, value in keysite.items()))
		generated.append("key page login")
	log("Logins ready" + (f" (generated: {', '.join(generated)})" if generated else " (kept existing)") + ".")

	# The key page and key script run from ~/bifrost/bin, independent of where this script lives.
	os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
	os.makedirs(KEYS_DIR, mode=0o700, exist_ok=True)
	os.makedirs(BIN_DIR, mode=0o755, exist_ok=True)
	for name in ("key-site.py", "make-student-keys.py", "end-class.py"):
		source = os.path.join(HERE, name)
		if not os.path.exists(source):
			die(f"{name} must be in the same folder as this script ({HERE}).")
		if os.path.abspath(source) != os.path.abspath(os.path.join(BIN_DIR, name)):
			shutil.copy(source, BIN_DIR)

	log("Pulling container images (first time takes a minute)...")
	run("docker", "pull", "-q", BIFROST_IMAGE)
	run("docker", "pull", "-q", PYTHON_IMAGE)

	# ---- 2. Start Bifrost locally and lock it down ------------------------------

	log("Starting Bifrost, reachable only from this VM...")
	start_gateway(publish=False)

	opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
	status, body = http_call("GET", LOCAL + "/api/session/is-auth-enabled")
	auth_on = status == 200 and json.loads(body).get("is_auth_enabled")

	login = {"username": env["BIFROST_ADMIN_USERNAME"], "password": env["BIFROST_ADMIN_PASSWORD"]}
	if auth_on:
		status, body = http_call("POST", LOCAL + "/api/session/login", login, opener)
		if status != 200:
			die(f"Admin login is on but the login in {ENV_FILE} doesn't work (HTTP {status}). "
			    "Was the password in that file changed after the first run?")

	log("Turning on the admin login and requiring a virtual key for every model request...")
	for attempt in range(10):
		status, body = http_call("GET", LOCAL + "/api/config", opener=opener)
		if status != 200:
			die(f"Couldn't read Bifrost's settings (HTTP {status}): {body[:200]}")
		config = json.loads(body)
		if auth_on and config["client_config"].get("enforce_auth_on_inference"):
			break  # already fully set up (a re-run); change nothing
		config["client_config"]["enforce_auth_on_inference"] = True
		# A brand-new Bifrost reports some settings as 0, then rejects them on
		# save; put back its own defaults.
		for name, default in (("log_retention_days", 365), ("async_job_result_ttl", 3600)):
			if not config["client_config"].get(name):
				config["client_config"][name] = default
		update = {"client_config": config["client_config"], "framework_config": config["framework_config"]}
		if auth_on:
			if config.get("auth_config"):
				update["auth_config"] = config["auth_config"]
		else:
			# References, not values: Bifrost reads the login from its environment,
			# so the password is never stored in its database.
			update["auth_config"] = {
				"admin_username": {"value": "", "ref": "env.BIFROST_ADMIN_USERNAME"},
				"admin_password": {"value": "", "ref": "env.BIFROST_ADMIN_PASSWORD"},
				"is_enabled": True,
				"setup_token": env["BIFROST_SETUP_TOKEN"],
			}
		status, body = http_call("PUT", LOCAL + "/api/config", update, opener)
		if status == 200:
			break
		if "database is locked" in body:  # Bifrost's startup sync briefly holds the database
			time.sleep(3)
			continue
		die(f"Couldn't update Bifrost's settings (HTTP {status}): {body[:300]}")
	else:
		die("Bifrost's database stayed locked; run this script again.")

	# Prove it before publishing anything.
	if http_call("GET", LOCAL + "/api/providers")[0] != 401:
		die("The admin login didn't take effect; NOT publishing Bifrost.")
	if http_call("POST", LOCAL + "/api/session/login", login, opener)[0] != 200:
		die("Couldn't log in with the generated admin login; NOT publishing Bifrost.")
	status, body = http_call("GET", LOCAL + "/api/providers/anthropic/keys", opener=opener)
	keys = json.loads(body).get("keys") if status == 200 else None
	if not keys:
		die("Bifrost didn't pick up ANTHROPIC_API_KEY as a provider key; NOT publishing Bifrost.")
	no_key = http_call("POST", LOCAL + "/v1/chat/completions",
	              {"model": f"anthropic/{MODEL}", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
	if no_key[0] != 401:
		die(f"Model requests without a virtual key aren't being refused (HTTP {no_key[0]}); NOT publishing Bifrost.")
	log("Admin login on; virtual key required; Anthropic key loaded.")

	# ---- 3. Publish on aux1 -----------------------------------------------------

	log(f"Publishing Bifrost on port {GATEWAY_PORT} (aux1)...")
	start_gateway(publish=True)

	# ---- 4. Key page on aux2 ----------------------------------------------------

	log(f"Starting the student key page on port {KEYSITE_PORT} (aux2)...")
	run("docker", "rm", "-f", KEYSITE_CONTAINER, check=False)
	run("docker", "run", "-d", "--name", KEYSITE_CONTAINER, "--restart", "unless-stopped",
	    "--user", f"{os.getuid()}:{os.getgid()}", "--env-file", KEYSITE_ENV, "-e", "KEYSITE_DATA_DIR=/data",
	    "-p", f"{KEYSITE_PORT}:8000", "-v", f"{BIN_DIR}:/app:ro", "-v", f"{KEYS_DIR}:/data",
	    PYTHON_IMAGE, "python", "-u", "/app/key-site.py")
	if not wait_for_health(f"http://localhost:{KEYSITE_PORT}", 30):
		logs = run("docker", "logs", "--tail", "20", KEYSITE_CONTAINER, check=False)
		die(f"The key page didn't start. Last log lines:\n{logs.stdout}{logs.stderr}")

	# ---- 5. Check from the outside ----------------------------------------------

	log("Checking the public addresses...")
	checks = [
		("aux1 gateway is up", http_call("GET", aux1 + "/health")[0], 200),
		("aux1 admin pages need a login", http_call("GET", aux1 + "/api/providers")[0], 401),
		("aux1 model requests need a key", http_call("POST", aux1 + "/v1/chat/completions",
			{"model": f"anthropic/{MODEL}", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})[0], 401),
		("aux2 key page is up", http_call("GET", aux2 + "/health")[0], 200),
		("aux2 key page needs a login", http_call("GET", aux2 + "/")[0], 401),
	]
	failed = False
	for label, got, want in checks:
		ok = got == want
		failed |= not ok
		print(f"    {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f" (got HTTP {got}, expected {want})"))
	if failed:
		die("Some public checks failed (see above). Everything is running locally; check the aux1/aux2 proxy for this VM.")

	print(f"""
Gateway ready.

  Students' gateway URL:  {aux1}/v1
  Bifrost admin UI:       {aux1}/      login: {env['BIFROST_ADMIN_USERNAME']} / see BIFROST_ADMIN_PASSWORD in {ENV_FILE}
  Student key page:       {aux2}/      login: {keysite['KEYSITE_USERNAME']} / see KEYSITE_PASSWORD in {KEYSITE_ENV}

Next: create the student keys, e.g. for 12 students:

  python3 {BIN_DIR}/make-student-keys.py 12
""")


if __name__ == "__main__":
	main()
