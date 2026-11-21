#!/bin/sh
# moodle-mcp quickstart for macOS and Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/TheBeaconCrafter/moodle-mcp/main/scripts/quickstart.sh | sh
#
# Installs uv (if missing) and moodle-mcp, asking first, then starts `moodle quickstart`: a guided setup
# that signs you in to Moodle and connects it to ChatGPT through OpenAI's Secure MCP Tunnel.
set -eu

# Everything runs inside main(), called on the last line, so a download cut off halfway runs nothing.
main() {
  REPO="git+https://github.com/TheBeaconCrafter/moodle-mcp"

  if [ ! -r /dev/tty ]; then
    echo "This setup asks questions, so it needs a terminal." >&2
    exit 1
  fi

  ask() {
    printf '%s [Y/n] ' "$1" > /dev/tty
    read -r answer < /dev/tty || answer=n
    case "$answer" in "" | y | Y | yes | Yes) return 0 ;; *) return 1 ;; esac
  }

  # uv installs into ~/.local/bin, which a fresh shell may not have on PATH yet.
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export PATH

  if ! command -v uv > /dev/null 2>&1; then
    ask "moodle-mcp is installed with uv (Astral's Python tool installer), which isn't here yet. Install uv from astral.sh?" \
      || { echo "Stopped. See https://docs.astral.sh/uv/ to install uv yourself."; exit 1; }
    if command -v curl > /dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget > /dev/null 2>&1; then
      wget -qO- https://astral.sh/uv/install.sh | sh
    else
      echo "Neither curl nor wget is available to download uv." >&2
      exit 1
    fi
  fi

  if command -v moodle > /dev/null 2>&1; then
    question="Update moodle-mcp to the latest version?"
  else
    question="Install moodle-mcp (the moodle and moodle-mcp commands)?"
  fi
  if ask "$question"; then
    uv tool install --force "$REPO"
    uv tool update-shell > /dev/null 2>&1 || true
  elif ! command -v moodle > /dev/null 2>&1; then
    echo "Stopped: moodle-mcp isn't installed."
    exit 1
  fi

  BIN="$(uv tool dir --bin 2> /dev/null || echo "$HOME/.local/bin")"
  MOODLE="$BIN/moodle"
  [ -x "$MOODLE" ] || MOODLE="moodle"
  exec "$MOODLE" quickstart < /dev/tty
}

main "$@"
