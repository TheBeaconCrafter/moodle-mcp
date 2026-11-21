# Getting your Moodle token

Most schools sign you in to Moodle with single sign-on (SSO), so there's no Moodle password this tool could use. It uses the same thing the official Moodle phone app uses instead: a **per-user web-service token**, about 32 letters and digits, tied to your account.

Pick one method. Methods A and B work everywhere the Moodle app works; Method C (further down) is for sites without single sign-on. All of them end with `moodle auth`.

## Method A: Security keys page (easiest)

1. Sign in to Moodle in your browser.
2. Click your avatar (top right) → **Preferences** → **Security keys**.
   Or go straight to `https://YOUR-MOODLE/user/managetoken.php`.
3. Find the row named **Moodle mobile web service** and click **Reset**.
4. Moodle shows the new key **once**. Copy it now, because the page only lists key names afterwards.
5. In a terminal, run:

   ```bash
   moodle auth
   ```

   Paste the key at the hidden prompt. You won't see it as you type, which is normal.

> [!NOTE]
> **No "Moodle mobile web service" row?** Sign in to the Moodle phone app once (App Store / Google Play, search "Moodle"), then refresh the Security keys page and the row appears.
>
> Resetting the key signs the phone app out. Just sign in to it again.

## Method B: the app launch link

Use this method if Method A isn't available at your school.

1. While signed in to Moodle in your browser, open:

   ```text
   https://YOUR-MOODLE/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=1&urlscheme=moodlemobile&confirmed=1
   ```

2. The page shows a link like *"Click here to launch the app"*. **Don't click it.** Right-click it and choose **Copy Link Address**. It starts with `moodlemobile://token=`.
3. Run `moodle auth` and paste the whole link. The tool decodes the token from it.

## Check it worked

```bash
moodle whoami
```

```json
{
  "site": "Your School Moodle",
  "release": "4.5.x",
  "fullname": "Your Name",
  "functions_available": 443
}
```

## Where it's stored

`~/.config/moodle-mcp/config.json`. On macOS and Linux its permissions are set so only your user account can read it; on Windows it lives in your profile folder and relies on that folder's permissions. Change the location with the `MOODLE_MCP_CONFIG` environment variable.

You can skip the file and set `MOODLE_URL` + `MOODLE_TOKEN` instead, which suits a server or a CI job. On your own computer, stick with `moodle auth`: typing `export MOODLE_TOKEN=…` into a terminal leaves the token in your shell history. If `MOODLE_URL` points at a different site from the one you saved, the saved token is ignored, so a mistyped URL can't receive it.

```bash
moodle logout    # deletes the file
```

## Keep it safe

The token can do anything the Moodle app can do as you, including submitting work. Don't share it, commit it, or paste it into a chat. If you think it got out, press **Reset** on the Security keys page. The old token stops working immediately, and you run `moodle auth` with the new one.

Tokens also expire on their own, often after a few months. When a tool says `invalidtoken`, repeat Method A. A server that's already running picks up the new token without a restart. `moodle status` tells you how old the token is.

## Method C: Moodle username and password (sites without SSO)

If you sign in to Moodle with a username and password that Moodle itself checks, rather than a Microsoft, Google or campus login page, you can skip the token hunt:

```bash
moodle auth --password             # asks for username and password; only the token is saved
moodle auth --password --remember  # also keeps the password in your system keychain, so the token renews itself
```

`--remember` needs the optional keychain support: `uv tool install --force "moodle-mcp[keyring] @ git+https://github.com/TheBeaconCrafter/moodle-mcp"`. [openai-tunnel.md](openai-tunnel.md#5-stay-signed-in) covers headless servers without a keychain.
