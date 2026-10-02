# bifrost-class-gateway

Give a classroom of lab VMs access to Claude through one shared
[Bifrost](https://github.com/maximhq/bifrost) gateway, without putting a
provider API key on any student machine.

One VM per class runs the gateway. It holds the real Anthropic API key and
gives each student a personal **virtual key** (`sk-bf-...`), limited to one
model and capped by its own daily budget and per-minute rate limit. A
password-protected web page lists a copy-paste setup line for each student VM,
and grays each one out once it has been copied.

```
student VM ──(virtual key)──▶ Bifrost gateway (aux1, :2224) ──(real key)──▶ Anthropic
                                     │
helper's browser ──(login)──▶ student key page (aux2, :2225)
```

## What's here

| Path | Runs on | What it does |
|---|---|---|
| [`gateway/setup-gateway.py`](gateway/setup-gateway.py) | gateway VM | Starts Bifrost and the key page, with the logins and protections in place (see [Security](#security)). Safe to re-run. |
| [`gateway/make-student-keys.py`](gateway/make-student-keys.py) | gateway VM | Creates `student-01` ... `student-NN` virtual keys and the roster the key page shows. |
| [`gateway/key-site.py`](gateway/key-site.py) | gateway VM (in Docker) | The student key page: one click-to-copy block per student. |
| [`gateway/end-class.py`](gateway/end-class.py) | gateway VM | Optional: deletes the class's student keys and empties the key page, for a gateway VM that outlives its class. |
| [`student-vm/install-pi-environment.sh`](student-vm/install-pi-environment.sh) | each student VM | Installs Node.js and the [Pi](https://www.npmjs.com/package/@earendil-works/pi-ai) agent packages, finds the student's key, and checks it works through the gateway. |

Step-by-step instructions for running a class are in
[`gateway/README.md`](gateway/README.md).

## Quick start

On the gateway VM (needs Docker and Python 3; run as a normal user, not root):

```
git clone https://github.com/csfeeser/bifrost-class-gateway.git
python3 bifrost-class-gateway/gateway/setup-gateway.py    # asks for the Anthropic key once
python3 ~/bifrost/bin/make-student-keys.py 12             # 12 students
```

Then open the key page address that `setup-gateway.py` printed, and paste
each student's block into a terminal on that student's VM. Each block writes
`~/.bifrost-env`:

```
BIFROST_API_KEY=sk-bf-...
BIFROST_BASE_URL=https://aux1-<gateway-vm>.<domain>/v1
```

Students then run `student-vm/install-pi-environment.sh`.

## Security

- **The provider key never leaves the gateway VM.** It lives in
  `~/bifrost/.env` (mode 600) and is only read by the Bifrost container.
  `install-pi-environment.sh` refuses to run if it finds that file's contents
  on a student VM.
- **Nothing is reachable until it's locked down.** `setup-gateway.py` first
  starts Bifrost on localhost only, turns on its admin login and requires a
  virtual key for every model request, verifies both, and only then publishes
  it.
- **The admin password is never stored in Bifrost's database.** Bifrost is
  configured with a reference to the `BIFROST_ADMIN_PASSWORD` environment
  variable instead of the value.
- **Every student key is limited:** one model (Claude Haiku 4.5), a daily
  budget (default $2), and a rate limit (default 60 requests/minute). Turn off
  any key at any time in the Bifrost admin UI.
- **The key page needs its own login** (HTTP basic auth over the HTTPS proxy),
  separate from the Bifrost admin login.
- **No secrets are in this repo.** All logins are generated on the gateway VM
  at setup time. `.gitignore` blocks `.env` files, key files, and rosters.

## Capacity

Load-tested in October 2026 with the worst case in the course labs (Lab 8's
runaway loop: 5 rounds, up to 5 tool calls per round), every simulated
student starting at the same moment:

| Simultaneous students | Path | Requests | Result | Median / max latency |
|---|---|---|---|---|
| 10 | through aux1 proxy | 50 | all OK | 1.4 s / 2.1 s |
| 20 | through aux1 proxy | 100 | all OK | 1.4 s / 2.1 s |
| 30 | through aux1 proxy | 150 | all OK | 1.5 s / 2.2 s |
| 60 | through aux1 proxy | 300 | 38 failed (HTTP 502 from the proxy) | 1.5 s / 2.3 s |
| 60 | direct to Bifrost | 300 | all OK | 1.4 s / 2.1 s |

- **Bifrost isn't the bottleneck:** about 130 MB of memory and under 15% of
  one CPU at the 60-student peak (~2,300 requests/minute).
- **The 60-student failures came from the lab platform's aux1 proxy**, not
  Bifrost (they never reached it, and were rejected within ~6 ms). In the test
  every request came from one VM; in a real class, requests come from each
  student's own VM, so a per-client limit in the proxy may not apply.
- **Cost:** about $0.014 per student per full run of that worst-case loop.

## Anthropic key hygiene

Virtual keys live only in the gateway VM's own database, so destroying the
gateway VM shuts off every student key with it. **It does not revoke the
Anthropic API key**, which stays valid until it's revoked in the
[Anthropic Console](https://console.anthropic.com/). Recommended:

- Create the gateway's key in a dedicated Anthropic **Workspace** with a
  **spend limit**, so any forgotten or leaked gateway has a capped cost.
- Use a separate key per class (or rotate the classroom key regularly), and
  revoke it when the class is over.

## Configuration

| Setting | Where | Default |
|---|---|---|
| Students' daily budget | `make-student-keys.py --budget` | `2.00` (USD) |
| Students' rate limit | `make-student-keys.py --rpm` | `60` requests/minute |
| Gateway URL in student blocks | `make-student-keys.py --gateway-url` | this VM's aux1 address |
| Domain of the aux1/aux2 addresses | `AUX_DOMAIN` environment variable | `lms-us-east-1.alta3.com` |
| Bifrost version | `BIFROST_IMAGE` in `setup-gateway.py` | `maximhq/bifrost:v2.2.4` |

The aux1/aux2 addresses assume a lab platform that publishes each VM's ports
2224 and 2225 as `https://aux1-<host>-lab-<id>.<AUX_DOMAIN>` and
`https://aux2-...`, where `hostname -f` is `<host>.<id>`. On other platforms,
pass `--gateway-url` to `make-student-keys.py` and point your own reverse
proxy at ports 2224 and 2225.

## License

[MIT](LICENSE)
