#!/usr/bin/env python3
"""
make-student-keys.py -- create one Bifrost virtual key per student.

Run this on the gateway VM (the one running Bifrost), as the student user:

    python3 make-student-keys.py 12

That creates student-01 ... student-12, each limited to Claude Haiku 4.5 with
its own daily budget and rate limit. Keys that already exist are reused, not
replaced, so it is safe to run again (e.g. to add more students: run it with
a bigger number).

It writes, into ~/bifrost/student-keys/:
  roster.json             -- read by the key website (key-site.py, port 2225)
  roster.csv              -- the same data, for spreadsheets
  student-NN.bifrost-env  -- one ready-to-copy ~/.bifrost-env per student

The gateway URL students should use is worked out from this VM's hostname
(its aux1 address). Override it with --gateway-url if that's ever wrong.
"""

import argparse
import csv
import http.cookiejar
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

ADMIN_BASE = "http://localhost:8090"  # Bifrost's admin API, reachable only on this VM
BIFROST_ENV = os.path.expanduser("~/bifrost/.env")
OUT_DIR = os.path.expanduser("~/bifrost/student-keys")
PROVIDER = "anthropic"
MODEL = "claude-haiku-4-5-20251001"

# Lab VMs are published as https://aux1-<host>-lab-<id>.<AUX_DOMAIN> (port 2224).
# Override with the AUX_DOMAIN environment variable.
AUX_DOMAIN = os.environ.get("AUX_DOMAIN", "lms-us-east-1.alta3.com")


def die(message):
	print(f"ERROR: {message}", file=sys.stderr)
	sys.exit(1)


def read_env_file(path):
	values = {}
	with open(path) as f:
		for line in f:
			line = line.strip()
			if line and not line.startswith("#") and "=" in line:
				name, value = line.split("=", 1)
				values[name.strip()] = value.strip()
	return values


class Admin:
	"""A logged-in session against Bifrost's admin API."""

	def __init__(self, username, password):
		self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
		status, body = self.call("POST", "/api/session/login", {"username": username, "password": password})
		if status != 200:
			die(f"Could not log in to Bifrost as {username} (HTTP {status}): {body}")

	def call(self, method, path, body=None):
		data = json.dumps(body).encode() if body is not None else None
		request = urllib.request.Request(ADMIN_BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
		# Bifrost briefly locks its database right after starting; retry that.
		for attempt in range(5):
			try:
				with self.opener.open(request, timeout=30) as response:
					return response.status, json.loads(response.read() or b"{}")
			except urllib.error.HTTPError as error:
				text = error.read().decode(errors="replace")
				if "database is locked" in text and attempt < 4:
					time.sleep(3)
					continue
				return error.code, text
			except urllib.error.URLError as error:
				die(f"Could not reach Bifrost at {ADMIN_BASE} ({error.reason}). Is the bifrost container running? (docker ps)")

	def list_virtual_keys(self):
		keys, offset = [], 0
		while True:
			status, body = self.call("GET", f"/api/governance/virtual-keys?limit=100&offset={offset}")
			if status != 200:
				die(f"Could not list virtual keys (HTTP {status}): {body}")
			page = body.get("virtual_keys") or []
			keys += page
			offset += len(page)
			if not page or offset >= body.get("total_count", 0):
				return keys


def detect_gateway_url():
	# This VM's aux1 address: https://aux1-<host>-lab-<id>.<AUX_DOMAIN>,
	# where "hostname -f" is "<host>.<id>".
	fqdn = socket.getfqdn()
	if "." not in fqdn:
		return None
	host, vm_id = fqdn.split(".", 1)
	return f"https://aux1-{host}-lab-{vm_id}.{AUX_DOMAIN}/v1"


def check_gateway_url(url):
	try:
		with urllib.request.urlopen(url.removesuffix("/v1") + "/health", timeout=15) as response:
			return response.status == 200
	except Exception:
		return False


def main():
	parser = argparse.ArgumentParser(description="Create one Bifrost virtual key per student.")
	parser.add_argument("count", type=int, help="number of students (keys student-01 .. student-NN)")
	parser.add_argument("--budget", type=float, default=2.00, help="max spend per key per day, in USD (default 2.00)")
	parser.add_argument("--rpm", type=int, default=60, help="max requests per minute per key (default 60)")
	parser.add_argument("--gateway-url", help="gateway URL students use (default: this VM's aux1 address)")
	args = parser.parse_args()

	if not 1 <= args.count <= 99:
		die("count must be between 1 and 99")

	gateway_url = (args.gateway_url or detect_gateway_url() or "").rstrip("/")
	if not gateway_url:
		die("Could not work out this VM's aux1 address from its hostname; pass --gateway-url https://aux1-.../v1")
	if not gateway_url.endswith("/v1"):
		gateway_url += "/v1"
	if not check_gateway_url(gateway_url):
		die(f"The gateway isn't answering at {gateway_url} -- check the bifrost container is running and published on port 2224, or pass --gateway-url")
	print(f"Gateway URL for students: {gateway_url}")

	env = read_env_file(BIFROST_ENV)
	admin = Admin(env["BIFROST_ADMIN_USERNAME"], env["BIFROST_ADMIN_PASSWORD"])

	status, body = admin.call("GET", f"/api/providers/{PROVIDER}/keys")
	if status != 200 or not body.get("keys"):
		die(f"Bifrost has no {PROVIDER} API key configured (HTTP {status}): {body}")
	provider_key_id = body["keys"][0]["id"]

	existing = {key["name"]: key for key in admin.list_virtual_keys()}
	roster = []
	for number in range(1, args.count + 1):
		name = f"student-{number:02d}"
		key = existing.get(name)
		if key:
			action = "reused"
		else:
			status, body = admin.call("POST", "/api/governance/virtual-keys", {
				"name": name,
				"description": "Course student key (make-student-keys.py)",
				"provider_configs": [{"provider": PROVIDER, "allowed_models": [MODEL], "weight": 1, "key_ids": [provider_key_id]}],
				"budgets": [{"max_limit": args.budget, "reset_duration": "1d"}],
				"rate_limit": {"request_max_limit": args.rpm, "request_reset_duration": "1m"},
				"is_active": True,
			})
			if status != 200:
				die(f"Could not create {name} (HTTP {status}): {body}")
			key = body.get("virtual_key", body)
			action = "created"
		if not key.get("is_active", True):
			action += " (WARNING: this key is deactivated in Bifrost)"
		print(f"  {name}: {action}")
		roster.append({"name": name, "key": key["value"], "gateway_url": gateway_url})

	os.makedirs(OUT_DIR, mode=0o700, exist_ok=True)

	def write_private(filename, text):
		path = os.path.join(OUT_DIR, filename)
		temp = path + ".tmp"
		fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
		with os.fdopen(fd, "w") as f:
			f.write(text)
		os.replace(temp, path)

	for student in roster:
		write_private(f"{student['name']}.bifrost-env",
			f"BIFROST_API_KEY={student['key']}\nBIFROST_BASE_URL={student['gateway_url']}\n")
	write_private("roster.json", json.dumps({"gateway_url": gateway_url, "students": roster}, indent=2) + "\n")
	with open(os.path.join(OUT_DIR, "roster.csv.tmp"), "w", newline="") as f:
		os.chmod(f.name, 0o600)
		writer = csv.writer(f)
		writer.writerow(["name", "bifrost_api_key", "bifrost_base_url"])
		for student in roster:
			writer.writerow([student["name"], student["key"], student["gateway_url"]])
	os.replace(os.path.join(OUT_DIR, "roster.csv.tmp"), os.path.join(OUT_DIR, "roster.csv"))

	print(f"\nWrote {len(roster)} students to {OUT_DIR}/ (roster.json, roster.csv, student-NN.bifrost-env)")


if __name__ == "__main__":
	main()
