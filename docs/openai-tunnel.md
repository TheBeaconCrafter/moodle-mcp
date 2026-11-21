# Using moodle-mcp from ChatGPT, Codex or the OpenAI API

ChatGPT can't start a program on your computer the way Claude Desktop does. OpenAI's answer is the **Secure MCP Tunnel**: you run a small program, [`tunnel-client`](https://github.com/openai/tunnel-client), next to moodle-mcp. It opens an outbound connection to OpenAI and passes requests from ChatGPT to moodle-mcp. Nothing on your machine listens on the internet, and you don't need a public URL, port forwarding or a domain.

```text
ChatGPT / Codex / Responses API
        │  (OpenAI's tunnel endpoint)
        ▼
tunnel-client  ──outbound HTTPS──  runs on your computer or a small server
        │  stdio, or HTTP on 127.0.0.1
        ▼
moodle-mcp  ──HTTPS + your token──►  your school's Moodle
```

This guide covers setting it up, keeping it running, and keeping it signed in to Moodle for months at a time.

**Fastest way: the quickstart.** One command installs everything and walks you through the rest in the terminal, asking before each change:

```bash
# macOS / Linux
curl -LsSf https://raw.githubusercontent.com/TheBeaconCrafter/moodle-mcp/main/scripts/quickstart.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/TheBeaconCrafter/moodle-mcp/main/scripts/quickstart.ps1 | iex
```

It installs uv and moodle-mcp if they're missing, signs you in to Moodle, lets you pick read-only or writes, installs OpenAI's `tunnel-client` as a normal command you can use for other tunnels too (see [Installing and updating tunnel-client](#installing-and-updating-tunnel-client)), asks for your tunnel id and runtime API key, writes the tunnel profile, runs `tunnel-client doctor` and sets the tunnel up to start on its own (a systemd user service, a launchd agent, or a Windows Startup entry). If moodle-mcp is already installed, just run `moodle quickstart`. You can read the scripts in [`scripts/`](../scripts) first if you'd rather not pipe them into a shell.

The sections below do the same by hand, and explain what the quickstart sets up.

> [!NOTE]
> tunnel-client is OpenAI's software and changes on its own schedule. The commands below follow its README at the time of writing. If a flag has moved, `tunnel-client --help` and `tunnel-client doctor --explain` are the source of truth.

## 1. Before you start

- moodle-mcp installed and signed in. `moodle whoami` should print your name ([Quick start](../README.md#quick-start)). On a machine that runs unattended, read [step 5](#5-stay-signed-in) before you sign in.
- A ChatGPT plan that lets you add custom MCP servers (or an OpenAI API project, if you use the Responses API).
- A **tunnel id** (`tunnel_…`) and a **runtime API key** with Tunnels **Read** + **Use**, both from the OpenAI Platform. [The tunnel id and the runtime API key](#the-tunnel-id-and-the-runtime-api-key) explains what they are and how to set them up.
- `tunnel-client` installed. `moodle tunnel install` does it for you (see [below](#installing-and-updating-tunnel-client)), or install it from its [releases page](https://github.com/openai/tunnel-client).

Keep the tunnel API key the same way you keep your Moodle token: out of chats, repos and shell history. Put it in an environment file only you can read, as [step 4](#4-keep-it-running) does.

### The tunnel id and the runtime API key

You create both on the [OpenAI Platform](https://platform.openai.com), under your organization's settings. They're separate from your ChatGPT login.

**The tunnel** is a record OpenAI keeps for one private connection. ChatGPT sends requests to the tunnel, and OpenAI hands them to whichever `tunnel-client` is signed in to it. Create it in [Tunnels](https://platform.openai.com/settings/organization/tunnels) and copy its id (`tunnel_…`). When you create it, attach your **ChatGPT workspace**, or it won't show up in ChatGPT's connector picker. A new tunnel takes 25–30 seconds before it's ready.

**The runtime API key** is how `tunnel-client` proves to OpenAI that it may serve that tunnel. It only lets `tunnel-client` pick up requests for a tunnel and send the answers back. It doesn't give anyone access to your Moodle; that stays on your machine, behind moodle-mcp. Create it in [API keys](https://platform.openai.com/settings/organization/api-keys):

1. Choose **Create new secret key**, then the **Restricted** permission type.
2. Set **Tunnels** to **Read** and **Use**, and leave everything else at **None**. Don't pick **All**, and don't use an admin key: the key sits on disk for as long as the tunnel runs, so give it as little as possible.
3. Copy the key. It starts with `sk-` and is shown only once.

The **key's permissions aren't the whole story**: the person or service account that owns the key also needs Tunnels **Read** + **Use** through their role in the organization. In an organization you own, that's already the case. Elsewhere, an admin grants it under [Roles](https://platform.openai.com/settings/organization/people/roles) / [Groups](https://platform.openai.com/settings/organization/people/groups). Make a new key after the role changes.

| Who / what | Needs | For |
| --- | --- | --- |
| The runtime API key | Restricted: Tunnels **Read** + **Use** | `tunnel-client run` and `doctor` |
| The key's owner | Tunnels **Read** + **Use** (organization role) | the key to actually work |
| Whoever creates the tunnel | Tunnels **Read** + **Manage** | creating and editing tunnel records |
| Whoever adds the connector in ChatGPT | Tunnels **Read** + **Use** | picking the tunnel in ChatGPT |

If you're the owner of your own organization, you're usually all four, and the only thing to get right is the Restricted key.

**Where it's kept.** The quickstart saves the key to `~/.config/moodle-mcp/tunnel-runtime.key`, readable only by your user account. The tunnel profile points at that file (`api_key: file:…`), so the key never appears in a command line, a service file or an environment variable.

**Changing it.** Create a new key, run `moodle quickstart` again and answer **No** to *Keep the runtime API key saved earlier?*, then revoke the old key on the API keys page. If the key leaks, revoke it first. A leaked runtime key can serve your tunnel, but it can't read your Moodle.

**When it's wrong**, `tunnel-client doctor` or the service log says so:

| Symptom | Likely cause |
| --- | --- |
| `401` from the control plane | The key was mistyped, revoked, or belongs to another organization |
| `403` while running | The key, or its owner, lacks Tunnels **Use** for this tunnel |
| The tunnel shows on the Platform but not in ChatGPT | The tunnel has no ChatGPT workspace attached, or the person adding the connector lacks Tunnels **Use** |
| The tunnel can't be found or isn't ready | Wrong tunnel id, or the tunnel was created less than 30 seconds ago |

OpenAI's [tunnel permissions guide](https://github.com/openai/tunnel-client/blob/master/docs/permissions.md) has the full details.

### Installing and updating tunnel-client

`moodle tunnel install` (or the quickstart) puts tunnel-client where you'd install it by hand, so the `tunnel-client` command works on its own and for other tunnels:

| OS | Where | How it updates |
| --- | --- | --- |
| macOS | Homebrew, `openai/tools/tunnel-client`. OpenAI supports only this on macOS, because its release ZIPs aren't notarized | `brew upgrade` |
| Linux | each version in `~/.local/share/tunnel-client/versions/<tag>/`, with `~/.local/bin/tunnel-client` linking to the current one | unpacks the new version and switches the link; the previous version stays for rollback |
| Windows | `%LOCALAPPDATA%\Programs\tunnel-client`, added to your user PATH | renames the old files aside, so it works even while the tunnel runs |

Downloads are checked against the SHA-256 in the release's `SHA256SUMS.txt` before anything is unpacked. The background service points at the stable command path, so it keeps working across updates.

```bash
moodle tunnel status    # installed version, where it lives, and whether a newer release exists
moodle tunnel update    # install the newest release, then restart the moodle tunnel service
```

If tunnel-client was already installed some other way, moodle-mcp uses that copy and leaves updating it to you.

## 2. Connect the tunnel

There are two ways for tunnel-client to reach moodle-mcp. Use **stdio** unless you have a reason not to: it has fewer moving parts, and tunnel-client restarts moodle-mcp if it ever exits.

### Option A: stdio (recommended)

tunnel-client starts `moodle-mcp` itself and talks to it over stdin/stdout.

First put the tunnel API key in an environment file only you can read. Creating the file with `install` makes it private from the start, and typing the key in an editor keeps it out of your shell history:

```bash
install -d -m 700 ~/.config/moodle-mcp
install -m 600 /dev/null ~/.config/moodle-mcp/tunnel.env
"${EDITOR:-nano}" ~/.config/moodle-mcp/tunnel.env   # add one line: CONTROL_PLANE_API_KEY=your-key
```

Then load it and set up the profile:

```bash
set -a; . ~/.config/moodle-mcp/tunnel.env; set +a
tunnel-client init --sample sample_mcp_stdio_local --profile moodle \
  --tunnel-id tunnel_0123456789abcdef0123456789abcdef \
  --mcp-command "moodle-mcp"
tunnel-client doctor --profile moodle --explain
tunnel-client run --profile moodle
```

If tunnel-client can't find `moodle-mcp`, give the full path from `which moodle-mcp` (`where moodle-mcp` on Windows).

### Option B: Streamable HTTP

Run moodle-mcp as a local HTTP server and point tunnel-client at it. A shared secret stops other programs on the same machine from using it.

```bash
# once: make a random secret in a file only you can read (umask 077: private from the moment it exists)
install -d -m 700 ~/.config/moodle-mcp
(umask 077; python -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.config/moodle-mcp/http-token)

export MOODLE_MCP_HTTP_TOKEN="$(cat ~/.config/moodle-mcp/http-token)"
export MOODLE_MCP_AUTH_HEADER="Bearer $MOODLE_MCP_HTTP_TOKEN"
moodle-mcp --http --port 8765 &          # listens on 127.0.0.1:8765/mcp only

tunnel-client run --profile moodle \
  --mcp.server-url http://127.0.0.1:8765/mcp \
  --mcp.extra-headers "Authorization: env:MOODLE_MCP_AUTH_HEADER"
```

In HTTP mode moodle-mcp listens on 127.0.0.1 only, rejects requests whose `Host` header isn't local, and rejects any request without the `MOODLE_MCP_HTTP_TOKEN` bearer token. It won't start without that token (at least 16 characters), because any program on the machine could otherwise use your Moodle account through it. `--no-auth` overrides that, but only use it if nothing else runs on the machine. Run `moodle-mcp --http --help` to see every option.

Check the `--mcp.extra-headers` value against your tunnel-client version. If requests come back `401 unauthorized`, the header isn't arriving.

## 3. Add it in ChatGPT

1. In ChatGPT, open the settings for connectors or plugins and add a **custom MCP server**.
2. Pick **Tunnel** as the connection type and choose your tunnel, or paste its `tunnel_…` id. Don't paste `http://127.0.0.1…`, because ChatGPT can't reach that.
3. Start a chat, enable the connector, and ask *"what's due this week?"*.

Codex and the Responses API connect to the same tunnel id. See the [tunnel-client README](https://github.com/openai/tunnel-client) for the exact settings.

### Read-only by default

moodle-mcp starts [read-only](../README.md#read-only-mode). ChatGPT can read everything, but it can't submit assignments or write to Moodle. Leave it that way unless you're about to submit something. To allow writes, add the flag to the command and restart:

```bash
tunnel-client init … --mcp-command "moodle-mcp --allow-writes"   # stdio
moodle-mcp --http --port 8765 --allow-writes                     # HTTP
```

Content from Moodle (forum posts, files, feedback) is written by other people, and a model can be talked into things by text it reads. Read-only mode makes sure that text can't change your Moodle account, whatever the model decides to do.

## 4. Keep it running

ChatGPT can only reach moodle-mcp while tunnel-client is running. Only one tunnel-client may run per tunnel id, so start it from one place only.

Each recipe below uses the stdio profile from Option A and reads the tunnel key from `~/.config/moodle-mcp/tunnel.env`. If you haven't made that file yet, create it the same way:

```bash
install -d -m 700 ~/.config/moodle-mcp
install -m 600 /dev/null ~/.config/moodle-mcp/tunnel.env
"${EDITOR:-nano}" ~/.config/moodle-mcp/tunnel.env   # add one line: CONTROL_PLANE_API_KEY=your-key
```

<details open>
<summary><b>Linux (systemd user service)</b></summary>

`~/.config/systemd/user/moodle-tunnel.service`:

```ini
[Unit]
Description=OpenAI Secure MCP Tunnel for moodle-mcp
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=%h/.config/moodle-mcp/tunnel.env
# moodle-mcp lives in ~/.local/bin after `uv tool install`; systemd doesn't read your shell PATH.
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/tunnel-client run --profile moodle
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now moodle-tunnel
loginctl enable-linger "$USER"        # keep it running when you're logged out
journalctl --user -u moodle-tunnel -f # logs
```

Change `ExecStart` if `which tunnel-client` prints a different path.

</details>

<details>
<summary><b>macOS (launchd)</b></summary>

`~/Library/LaunchAgents/com.moodle-mcp.tunnel.plist` (replace `YOU` with your user name):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.moodle-mcp.tunnel</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-c</string>
    <string>set -a; . "$HOME/.config/moodle-mcp/tunnel.env"; exec "$HOME/.local/bin/tunnel-client" run --profile moodle</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/Users/YOU/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/moodle-tunnel.log</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.moodle-mcp.tunnel.plist
launchctl kickstart -k gui/$(id -u)/com.moodle-mcp.tunnel   # restart after changes
```

A Mac that sleeps drops the tunnel until it wakes up. For a tunnel that's always reachable, use a machine that stays on.

</details>

<details>
<summary><b>Windows (Task Scheduler)</b></summary>

Save this as `%USERPROFILE%\.config\moodle-mcp\tunnel.ps1`:

```powershell
Get-Content "$env:USERPROFILE\.config\moodle-mcp\tunnel.env" | ForEach-Object {
  $k, $v = $_ -split '=', 2; if ($k) { [Environment]::SetEnvironmentVariable($k, $v, 'Process') }
}
while ($true) {
  tunnel-client run --profile moodle
  Start-Sleep -Seconds 10   # restart if it exits
}
```

Then register it to start when you sign in:

```powershell
$action  = New-ScheduledTaskAction -Execute "pwsh.exe" -Argument "-NoProfile -WindowStyle Hidden -File `"$env:USERPROFILE\.config\moodle-mcp\tunnel.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "moodle-tunnel" -Action $action -Trigger $trigger
```

Use `powershell.exe` instead of `pwsh.exe` if you don't have PowerShell 7.

</details>

## 5. Stay signed in

A Moodle token doesn't last forever. Many sites expire mobile-app tokens after a few months, and pressing **Reset** on the Security keys page (or signing in to the phone app again, at some schools) replaces it. moodle-mcp has three ways to cope, from least to most hands-off.

### Re-sign in without restarting anything (every site)

When Moodle rejects the token, moodle-mcp re-reads `~/.config/moodle-mcp/config.json` and retries. When a tool starts failing with `invalidtoken`, run this on the machine that runs the tunnel:

```bash
moodle auth
```

The next request from ChatGPT works. There's no need to restart tunnel-client or moodle-mcp.

### Find out before ChatGPT does (every site)

`moodle status` checks the saved sign-in, shows how old the token is, and exits with status 1 when it no longer works:

```console
$ moodle status
✓ Signed in as Sam Student on Example University Moodle.
  Token saved 41 days ago.
  Auto-renew: off. When the token expires, run `moodle auth` again; running servers pick it up.
```

Run it on a schedule so you hear about an expired token from your own machine first, for example in a daily cron job:

```cron
0 8 * * *  ~/.local/bin/moodle status >/dev/null || notify-send "moodle-mcp" "Moodle sign-in expired: run moodle auth"
```

`moodle status --json` gives the same information as JSON, for scripts.

### Renew automatically (sites with a Moodle password)

If your school signs you in to Moodle with a **Moodle username and password**, not single sign-on (SSO) through Microsoft, Google or a campus login page, moodle-mcp can get a new token by itself whenever the old one expires:

```bash
moodle auth --password --remember        # asks for username and password (hidden)
moodle status                            # "Auto-renew: on"
```

- The password goes into your operating system's keychain (macOS Keychain, Windows Credential Manager, or the Secret Service on Linux desktops), never into `config.json`.
- **No keychain?** Headless Linux servers usually have none. Then moodle-mcp writes the password to `~/.config/moodle-mcp/password` by itself: created with mode `600` in a `700` folder, so only your user account can read it. `moodle auth` tells you which of the two it used.
- When Moodle rejects the token, moodle-mcp signs in through `login/token.php` with the remembered password, saves the new token and retries the request.
- `moodle logout`, or a later `moodle auth` without `--remember`, removes the remembered password.

**Your own password file (optional).** If you'd rather manage the file yourself, for example to hand it to a systemd service as a credential, point `MOODLE_MCP_PASSWORD_FILE` at it:

```bash
install -m 600 /dev/null ~/.config/moodle-mcp/password
"${EDITOR:-nano}" ~/.config/moodle-mcp/password   # type the password, save
```

```ini
# in moodle-tunnel.service, under [Service]
Environment=MOODLE_MCP_PASSWORD_FILE=%h/.config/moodle-mcp/password
```

Then sign in once with `MOODLE_MCP_PASSWORD_FILE=~/.config/moodle-mcp/password moodle auth --password --remember`. With the variable set, nothing is written to a keychain.

On macOS and Linux, moodle-mcp refuses a password file that belongs to another user or that group/other can read or write (anything looser than `chmod 600`); `moodle status` says why auto-renew is off. Windows has no such permission bits, so there it isn't checked: keep the file inside your user profile folder. `moodle logout` never deletes a `MOODLE_MCP_PASSWORD_FILE`, because you manage it; it does delete the automatic `~/.config/moodle-mcp/password`.

> [!IMPORTANT]
> A remembered password is worth more to an attacker than a token, which you can reset from the Security keys page. Only use auto-renew on a machine you trust, and leave the server read-only.

**Sites with SSO** can't renew on their own. Moodle never sees an SSO password, and it has no web-service call that extends a token. Use `moodle status` on a schedule and run `moodle auth` when it fails. That takes about a minute every few months.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| ChatGPT says the connector is unavailable | tunnel-client isn't running, or two copies are running for the same tunnel id. Check the service logs and `tunnel-client doctor --profile moodle --explain` |
| `mcp_initialization_required` in tunnel-client logs | Your tunnel-client or ChatGPT speaks an older MCP version. Update tunnel-client. For stdio, try adding `--mcp.stdio-send-initialized-notification` to `run` |
| `401 unauthorized` (HTTP mode) | The bearer header isn't reaching moodle-mcp. Check `MOODLE_MCP_AUTH_HEADER` and the `--mcp.extra-headers` syntax |
| `421 Misdirected Request` / invalid Host (HTTP mode) | Point tunnel-client at `http://127.0.0.1:PORT/mcp` (or `localhost`), not the machine's LAN name |
| `notoken` | The service runs as a different user, or before `moodle auth`. Sign in as the user the service runs as |
| `invalidtoken` | The token expired. Run `moodle auth` (no restart needed), or set up auto-renew |
| A tool returns `readonly` | That's the default. Add `--allow-writes` to the moodle-mcp command if you mean it |
| Large files time out | Requests through the tunnel have a 120-second deadline and a 10 MiB size limit. `moodle_file_text` returns text in slices, so ask for the next slice |
