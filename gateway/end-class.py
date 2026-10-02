#!/usr/bin/env python3
"""
end-class.py -- shut off the class's student keys when the class is over.

Run it on the gateway VM:

    python3 ~/bifrost/bin/end-class.py

It lists every student key and what it spent in its current budget period,
asks you to confirm, then:
  - deletes the student-NN virtual keys from Bifrost, so they stop working
    immediately, even on student VMs that still have them, and
  - clears the student key page (roster, per-student files, grayed-out state).

Options:
  --all            also delete virtual keys not named student-NN (e.g. test keys)
  --stop-gateway   also stop Bifrost and the key page (setup-gateway.py starts them again)
  --dry-run        show what would happen, change nothing
  --yes            don't ask for confirmation

The Anthropic key and admin logins in ~/bifrost stay, so the next class can be
set up on this VM with make-student-keys.py.
"""

import argparse
import glob
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ADMIN_BASE = "http://localhost:8090"
BIFROST_ENV = os.path.expanduser("~/bifrost/.env")
KEYS_DIR = os.path.expanduser("~/bifrost/student-keys")
CONTAINERS = ("bifrost-keysite", "bifrost")
STUDENT_NAME = re.compile(r"^student-\d+$")


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
	def __init__(self, username, password):
		self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
		status, body = self.call("POST", "/api/session/login", {"username": username, "password": password})
		if status != 200:
			die(f"Could not log in to Bifrost as {username} (HTTP {status}): {body}")

	def call(self, method, path, body=None):
		data = json.dumps(body).encode() if body is not None else None
		request = urllib.request.Request(ADMIN_BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
		try:
			with self.opener.open(request, timeout=30) as response:
				return response.status, json.loads(response.read() or b"{}")
		except urllib.error.HTTPError as error:
			return error.code, error.read().decode(errors="replace")
		except urllib.error.URLError as error:
			die(f"Could not reach Bifrost at {ADMIN_BASE} ({error.reason}). Is it running? (docker ps)")

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


def spent(key):
	budgets = key.get("budgets") or []
	if not budgets:
		return "n/a (no budget set)"
	b = budgets[0]
	return f"${b.get('current_usage', 0):.2f} of ${b.get('max_limit', 0):.2f} per {b.get('reset_duration', '?')}"


def main():
	parser = argparse.ArgumentParser(description="Shut off the class's student keys.")
	parser.add_argument("--all", action="store_true", help="also delete keys not named student-NN")
	parser.add_argument("--stop-gateway", action="store_true", help="also stop Bifrost and the key page")
	parser.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
	parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
	args = parser.parse_args()

	env = read_env_file(BIFROST_ENV)
	admin = Admin(env["BIFROST_ADMIN_USERNAME"], env["BIFROST_ADMIN_PASSWORD"])

	keys = sorted(admin.list_virtual_keys(), key=lambda k: k["name"])
	doomed = [k for k in keys if args.all or STUDENT_NAME.match(k["name"])]
	kept = [k for k in keys if k not in doomed]
	page_files = sorted(glob.glob(os.path.join(KEYS_DIR, "*")))

	if doomed:
		print(f"Virtual keys to delete ({len(doomed)}):")
		for k in doomed:
			print(f"  {k['name']:<16} spent {spent(k)}{'' if k.get('is_active', True) else '  (already deactivated)'}")
	else:
		print("No student keys to delete.")
	if kept:
		print(f"Keeping {len(kept)} other key(s): {', '.join(k['name'] for k in kept)}  (use --all to delete these too)")
	print(f"Key page files to clear: {len(page_files)} in {KEYS_DIR}")
	if args.stop_gateway:
		print(f"Then stop: {', '.join(CONTAINERS)}")

	if args.dry_run:
		print("\nDry run: nothing changed.")
		return
	if not doomed and not page_files and not args.stop_gateway:
		print("\nNothing to do.")
		return
	if not args.yes:
		answer = input("\nDeleted keys can't be brought back. Type yes to continue: ").strip().lower()
		if answer != "yes":
			print("Cancelled; nothing changed.")
			return

	failed = []
	for k in doomed:
		status, body = admin.call("DELETE", f"/api/governance/virtual-keys/{k['id']}")
		if status == 200:
			print(f"  deleted {k['name']}")
		else:
			failed.append(k["name"])
			print(f"  FAILED to delete {k['name']} (HTTP {status}): {str(body)[:200]}")

	for path in page_files:
		os.remove(path)
	print(f"  cleared {len(page_files)} key page file(s)")

	if args.stop_gateway:
		for name in CONTAINERS:
			result = subprocess.run(["docker", "stop", name], capture_output=True, text=True)
			print(f"  {'stopped' if result.returncode == 0 else 'could not stop'} {name}")
		print("  (Run setup-gateway.py to start them again.)")

	if failed:
		die(f"Could not delete: {', '.join(failed)}. Delete them in the Bifrost admin UI under Virtual Keys.")
	print("\nDone. The deleted keys no longer work anywhere.")


if __name__ == "__main__":
	main()
