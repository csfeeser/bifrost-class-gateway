#!/usr/bin/env bash
#
# install-pi-environment.sh
#
# Provisions a training VM for the "LLM Mechanics and Agentic AI" course:
#   - Node.js + npm
#   - Connection details for the course Bifrost gateway, which serves the
#     model (Claude Haiku 4.5) to every student VM
#   - The Pi agent harness packages (@earendil-works/pi-agent-core, @earendil-works/pi-ai)
#   - A verified working reference script confirming the full stack functions
#
# Students run this once at the start of the course (Lab 1). It is safe to
# re-run: anything already installed is detected and skipped.
#
# The student's personal Bifrost virtual key must already be on the VM before
# this runs, in ANY of these places (checked in this order):
#   1. the BIFROST_API_KEY environment variable
#   2. a BIFROST_API_KEY=... line in ~/.bifrost-env
#   3. a BIFROST_API_KEY=... line in ~/.env
#   4. a line holding just the bare key (sk-bf-...) in ~/.bifrost-env or ~/.env
# Never put the gateway's own .env (provider API key, admin password) on a
# student VM; the script refuses to run if it finds one.
#
# The gateway address changes every class, so it is NOT built into this
# script. Provide it the same way, as BIFROST_BASE_URL (environment variable,
# or a BIFROST_BASE_URL=... line in ~/.bifrost-env or ~/.env), e.g.:
#   BIFROST_BASE_URL=https://aux1-<gateway-vm>.lms-us-east-1.alta3.com/v1
# A trailing /v1 is added if it's missing.
#
# Usage: bash install-pi-environment.sh   (as the student user -- NOT with sudo)

set -euo pipefail

# ---- Configuration -----------------------------------------------------

readonly REQUIRED_MEM_MB=1500
readonly REQUIRED_CPUS=1
readonly REQUIRED_DISK_GB=3

readonly BIFROST_MODEL="anthropic/claude-haiku-4-5-20251001"
readonly BIFROST_ENV_FILE="${HOME}/.bifrost-env"
readonly LAB_DIR="${HOME}/pi-agent-lab"

readonly PI_AGENT_CORE_VERSION="0.87.0"
readonly PI_AI_VERSION="0.87.0"
readonly TYPEBOX_VERSION="1.3.27"

# Minimum Node.js required by the pinned Pi packages ("engines" in their package.json).
readonly REQUIRED_NODE_VERSION="22.19.0"

# ---- Helpers -------------------------------------------------------------

log() {
	echo ">>> $*"
}

fail() {
	echo "ERROR: $*" >&2
	exit 1
}

# True if node is on PATH and at least REQUIRED_NODE_VERSION.
node_version_ok() {
	command -v node >/dev/null 2>&1 || return 1
	local current
	current=$(node -v | sed 's/^v//')
	[[ "$(printf '%s\n' "${REQUIRED_NODE_VERSION}" "${current}" | sort -V | head -1)" == "${REQUIRED_NODE_VERSION}" ]]
}

# Prints the value of NAME from a KEY=value file, without sourcing the file.
# Tolerates "export NAME=...", spaces around "=", surrounding quotes, and CRLF
# line endings.
read_env_value() {
	local name=$1 file=$2
	[[ -f "${file}" ]] || return 1
	local line
	line=$(tr -d '\r' < "${file}" | grep -E "^[[:space:]]*(export[[:space:]]+)?${name}[[:space:]]*=" | tail -1) || return 1
	line=${line#*=}
	line=$(printf '%s' "${line}" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')
	line=${line#\"}; line=${line%\"}
	line=${line#\'}; line=${line%\'}
	[[ -n "${line}" ]] || return 1
	printf '%s' "${line}"
}

# Prints a bare virtual key from a file that holds just "sk-bf-..." on a line
# of its own, with no NAME= in front of it.
read_bare_key() {
	local file=$1
	[[ -f "${file}" ]] || return 1
	local line
	line=$(tr -d '\r' < "${file}" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' | grep -E '^sk-bf-[A-Za-z0-9_-]+$' | tail -1) || return 1
	printf '%s' "${line}"
}

# Prints the variable names (never the values) defined in a KEY=value file.
list_env_names() {
	tr -d '\r' < "$1" | sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=.*/\2/p' | sort -u | paste -sd' ' -
}

# Fails if a file holds the gateway's own server secrets. Those belong only on
# the gateway host; ~/.bifrost-env is loaded into every terminal on this VM.
refuse_server_secrets() {
	local file=$1
	[[ -f "${file}" ]] || return 0
	if tr -d '\r' < "${file}" | grep -qE '^[[:space:]]*(export[[:space:]]+)?(ANTHROPIC_API_KEY|OPENAI_API_KEY|BIFROST_ADMIN_PASSWORD|BIFROST_SETUP_TOKEN)[[:space:]]*='; then
		fail "${file} contains the gateway's server secrets (provider API key or admin password), not a student key. Remove that file from this VM and give it only its own sk-bf- virtual key -- flag your instructor."
	fi
}

# ---- Preflight -------------------------------------------------------------

# LAB_DIR is built from $HOME, which is /root under sudo -- the student would
# end up with no lab directory of their own.
if [[ ${EUID} -eq 0 ]]; then
	fail "Run this as your normal user, not with sudo or as root (it installs into your home directory)."
fi

# ---- Step 1: Verify the VM has enough resources ---------------------------

log "Checking VM resources against course requirements..."

mem_total_mb=$(awk '/MemTotal/ {printf "%d", $2 / 1024}' /proc/meminfo)
cpu_count=$(nproc)
disk_free_gb=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')

if (( mem_total_mb < REQUIRED_MEM_MB )) || (( cpu_count < REQUIRED_CPUS )) || (( disk_free_gb < REQUIRED_DISK_GB )); then
	echo ""
	echo "This VM is too small for this course."
	echo ""
	echo "  Detected:  ${mem_total_mb}MB RAM, ${cpu_count} vCPU, ${disk_free_gb}GB free disk"
	echo "  Required:  >=${REQUIRED_MEM_MB}MB RAM, >=${REQUIRED_CPUS} vCPU, >=${REQUIRED_DISK_GB}GB free disk"
	echo ""
	exit 1
fi

log "VM resources OK (${mem_total_mb}MB RAM, ${cpu_count} vCPU, ${disk_free_gb}GB free disk)."

# ---- Step 2: Install Node.js ----------------------------------------------

if node_version_ok; then
	log "Node.js already installed ($(node -v)), skipping."
else
	sudo -v || fail "This script needs sudo access to install Node.js."
	log "Installing Node.js..."
	sudo apt-get update -qq
	sudo apt-get install -y -qq nodejs npm
	if ! node_version_ok; then
		# Older distro releases ship a Node.js too old for the Pi packages.
		log "Distro Node.js is older than v${REQUIRED_NODE_VERSION}; installing Node.js 22 from NodeSource..."
		sudo apt-get remove -y -qq npm || true
		curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
		sudo apt-get install -y -qq nodejs
	fi
	node_version_ok || fail "Node.js v${REQUIRED_NODE_VERSION} or newer is required (found $(node -v 2>/dev/null || echo none))."
fi

log "node: $(node -v), npm: $(npm -v)"

# ---- Step 3: Find the Bifrost gateway connection details ------------------

log "Looking for your Bifrost gateway key..."

refuse_server_secrets "${BIFROST_ENV_FILE}"
refuse_server_secrets "${HOME}/.env"

if [[ -n "${BIFROST_API_KEY:-}" ]]; then
	key_source="the BIFROST_API_KEY environment variable"
elif BIFROST_API_KEY=$(read_env_value BIFROST_API_KEY "${BIFROST_ENV_FILE}"); then
	key_source="${BIFROST_ENV_FILE}"
elif BIFROST_API_KEY=$(read_env_value BIFROST_API_KEY "${HOME}/.env"); then
	key_source="${HOME}/.env"
elif BIFROST_API_KEY=$(read_bare_key "${BIFROST_ENV_FILE}"); then
	key_source="${BIFROST_ENV_FILE} (bare key)"
elif BIFROST_API_KEY=$(read_bare_key "${HOME}/.env"); then
	key_source="${HOME}/.env (bare key)"
else
	echo "" >&2
	echo "No Bifrost key found. Looked for a BIFROST_API_KEY=sk-bf-... line (or a bare sk-bf-... line) in:" >&2
	for candidate in "${BIFROST_ENV_FILE}" "${HOME}/.env"; do
		if [[ -f "${candidate}" ]]; then
			names=$(list_env_names "${candidate}")
			echo "  ${candidate}: exists; variables defined: ${names:-(none)}" >&2
		else
			echo "  ${candidate}: does not exist" >&2
		fi
	done
	echo "  BIFROST_API_KEY environment variable: not set" >&2
	echo "" >&2
	fail "No Bifrost key found -- flag your instructor."
fi

[[ "${BIFROST_API_KEY}" == sk-bf-* ]] \
	|| fail "The key found in ${key_source} doesn't look like a Bifrost virtual key (they start with sk-bf-) -- flag your instructor."

log "Found key in ${key_source} (${BIFROST_API_KEY:0:6}...)."

if [[ -n "${BIFROST_BASE_URL:-}" ]]; then
	url_source="the BIFROST_BASE_URL environment variable"
elif BIFROST_BASE_URL=$(read_env_value BIFROST_BASE_URL "${BIFROST_ENV_FILE}"); then
	url_source="${BIFROST_ENV_FILE}"
elif BIFROST_BASE_URL=$(read_env_value BIFROST_BASE_URL "${HOME}/.env"); then
	url_source="${HOME}/.env"
else
	fail "No gateway address found. Set BIFROST_BASE_URL (e.g. https://aux1-<gateway-vm>.lms-us-east-1.alta3.com/v1) in your environment, ${BIFROST_ENV_FILE}, or ~/.env -- flag your instructor."
fi

[[ "${BIFROST_BASE_URL}" =~ ^https?:// ]] \
	|| fail "The gateway address found in ${url_source} (${BIFROST_BASE_URL}) must start with http:// or https:// -- flag your instructor."
BIFROST_BASE_URL=${BIFROST_BASE_URL%/}
[[ "${BIFROST_BASE_URL}" == */v1 ]] || BIFROST_BASE_URL="${BIFROST_BASE_URL}/v1"

log "Found gateway address in ${url_source}: ${BIFROST_BASE_URL}"

# ---- Step 4: Make the connection details available to every lab ----------

# Lab scripts read BIFROST_BASE_URL and BIFROST_API_KEY from the environment,
# so write both to ~/.bifrost-env and load that file from ~/.bashrc. Any other
# lines already in the file are kept; only the two gateway settings (and any
# bare key line) are replaced with normalized NAME=value lines.
log "Saving gateway settings to ${BIFROST_ENV_FILE}..."
{
	echo "# Course Bifrost gateway settings -- written by install-pi-environment.sh"
	if [[ -f "${BIFROST_ENV_FILE}" ]]; then
		tr -d '\r' < "${BIFROST_ENV_FILE}" | grep -vE \
			-e '^[[:space:]]*(export[[:space:]]+)?BIFROST_(API_KEY|BASE_URL)[[:space:]]*=' \
			-e '^[[:space:]]*sk-bf-' \
			-e '^# Course Bifrost gateway settings' || true
	fi
	echo "BIFROST_BASE_URL=${BIFROST_BASE_URL}"
	echo "BIFROST_API_KEY=${BIFROST_API_KEY}"
} > "${BIFROST_ENV_FILE}.tmp"
chmod 600 "${BIFROST_ENV_FILE}.tmp"
mv "${BIFROST_ENV_FILE}.tmp" "${BIFROST_ENV_FILE}"

if ! grep -qF '.bifrost-env' "${HOME}/.bashrc" 2>/dev/null; then
	cat >> "${HOME}/.bashrc" <<'EOF'

# Course Bifrost gateway settings (added by install-pi-environment.sh)
if [ -f "$HOME/.bifrost-env" ]; then set -a; . "$HOME/.bifrost-env"; set +a; fi
EOF
fi
export BIFROST_BASE_URL BIFROST_API_KEY

# ---- Step 5: Confirm the gateway accepts the key ---------------------------

log "Checking the gateway accepts your key..."
models_json=$(mktemp)
trap 'rm -f "${models_json}"' EXIT
http_code=$(curl -sS -m 15 -o "${models_json}" -w '%{http_code}' \
	-H "Authorization: Bearer ${BIFROST_API_KEY}" "${BIFROST_BASE_URL}/models" 2>/dev/null) || http_code="000"

case "${http_code}" in
	200) ;;
	000) fail "Could not reach the gateway at ${BIFROST_BASE_URL} (address taken from ${url_source}) -- flag your instructor." ;;
	401) fail "The gateway rejected your key (HTTP 401) -- flag your instructor." ;;
	*)   fail "The gateway returned HTTP ${http_code} for ${BIFROST_BASE_URL}/models -- flag your instructor." ;;
esac
grep -qF "\"${BIFROST_MODEL}\"" "${models_json}" \
	|| fail "Your key works, but it is not allowed to use ${BIFROST_MODEL} -- flag your instructor."

log "Gateway OK: your key can use ${BIFROST_MODEL}."

# ---- Step 6: Install the Pi agent harness packages -------------------------

log "Setting up ${LAB_DIR}..."
mkdir -p "${LAB_DIR}"
cd "${LAB_DIR}"

if [[ ! -f package.json ]]; then
	npm init -y --silent >/dev/null
fi

log "Installing pinned Pi packages..."
# --loglevel=error (not --silent) so a failed install still prints why on stderr.
npm install --no-fund --no-audit --loglevel=error --save-exact \
	"@earendil-works/pi-agent-core@${PI_AGENT_CORE_VERSION}" \
	"@earendil-works/pi-ai@${PI_AI_VERSION}" \
	"typebox@${TYPEBOX_VERSION}" >/dev/null

# ---- Step 7: Write and run a reference harness to confirm the stack works --

cat > "${LAB_DIR}/reference-agent.mjs" << 'EOF'
import { Agent } from "@earendil-works/pi-agent-core";
import { createModels, createProvider } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { Type } from "typebox";

const bifrostModel = {
	id: "anthropic/claude-haiku-4-5-20251001",
	name: "Claude Haiku 4.5 (via Bifrost)",
	api: "openai-completions",
	provider: "bifrost",
	baseUrl: process.env.BIFROST_BASE_URL,
	reasoning: false,
	input: ["text"],
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
	contextWindow: 200000,
	maxTokens: 1024,
	compat: { supportsDeveloperRole: false, supportsReasoningEffort: false },
};

const bifrost = createProvider({
	id: "bifrost",
	name: "Bifrost",
	baseUrl: process.env.BIFROST_BASE_URL,
	auth: { apiKey: { name: "Bifrost", resolve: async () => ({ auth: { apiKey: process.env.BIFROST_API_KEY } }) } },
	models: [bifrostModel],
	api: openAICompletionsApi(),
});

const models = createModels();
models.setProvider(bifrost);
const model = models.getModel("bifrost", "anthropic/claude-haiku-4-5-20251001");

const agent = new Agent({
	initialState: {
		systemPrompt: "You are a helpful assistant.",
		model,
		tools: [
			{
				name: "get_weather",
				label: "Get Weather",
				description: "Get current weather for a city",
				parameters: Type.Object({ city: Type.String() }),
				execute: async (id, params) => ({
					content: [{ type: "text", text: `Sunny, 72F in ${params.city}` }],
					details: {},
				}),
			},
		],
	},
	streamFn: models.streamSimple.bind(models),
});

let sawToolCall = false;
agent.subscribe((event) => {
	if (event.type === "tool_execution_start") sawToolCall = true;
});

await agent.prompt("What's the weather in Boston? Use the tool.");

if (agent.state.errorMessage) {
	console.error("FAIL:", agent.state.errorMessage);
	process.exit(1);
}
if (!sawToolCall) {
	console.error("FAIL: model did not call the tool");
	process.exit(1);
}

console.log("OK: agent harness verified");
EOF

log "Running verification..."
# Retry once in case of a transient network error between the VM and the gateway.
verified=false
for attempt in 1 2; do
	if node "${LAB_DIR}/reference-agent.mjs"; then
		verified=true
		break
	fi
	if (( attempt < 2 )); then
		log "Verification attempt ${attempt} failed; retrying..."
	fi
done
[[ "${verified}" == true ]] || fail "Agent harness verification failed -- flag your instructor."

# ---- Done -------------------------------------------------------------

echo ""
echo "Environment ready:"
echo "  - Model:      ${BIFROST_MODEL} (served by the Bifrost gateway at ${BIFROST_BASE_URL})"
echo "  - Settings:   ${BIFROST_ENV_FILE} (loaded by every new terminal)"
echo "  - Lab dir:    ${LAB_DIR}"
echo "  - Packages:   @earendil-works/pi-agent-core@${PI_AGENT_CORE_VERSION}, @earendil-works/pi-ai@${PI_AI_VERSION}"
echo ""
echo "Open a new terminal (or run: source ~/.bashrc) before starting the next step."
echo ""
