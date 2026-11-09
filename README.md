<p align="center">
  <img src="docs/assets/banner.svg" alt="hermes-whatsapp-voice: call your Hermes agent on WhatsApp, or let it call you" width="100%">
</p>

<p align="center">
  <a href="https://github.com/ashwanthbalakrishnan5/hermes-whatsapp-voice/releases/latest"><img alt="Release" src="https://img.shields.io/github/v/release/ashwanthbalakrishnan5/hermes-whatsapp-voice?color=25d366&label=release"></a>
  <a href="https://github.com/ashwanthbalakrishnan5/hermes-whatsapp-voice/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/ashwanthbalakrishnan5/hermes-whatsapp-voice/ci.yml?branch=main&label=CI"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/github/license/ashwanthbalakrishnan5/hermes-whatsapp-voice?color=blue"></a>
  <a href="https://github.com/ashwanthbalakrishnan5/hermes-whatsapp-voice/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/ashwanthbalakrishnan5/hermes-whatsapp-voice?style=flat&color=f5c518"></a>
  <a href="https://github.com/NousResearch/hermes-agent"><img alt="Hermes plugin" src="https://img.shields.io/badge/Hermes-plugin-7c3aed"></a>
  <img alt="Linux and macOS" src="https://img.shields.io/badge/platforms-Linux%20%7C%20macOS-555">
</p>

<p align="center">
  <a href="#quick-start"><b>Quick start</b></a> ·
  <a href="#how-it-works"><b>How it works</b></a> ·
  <a href="docs/configuration.md"><b>Configuration</b></a> ·
  <a href="CONTRIBUTING.md"><b>Contributing</b></a> ·
  <a href="SECURITY.md"><b>Security</b></a>
</p>

Live voice calls with [Hermes](https://github.com/NousResearch/hermes-agent) over WhatsApp. Call your
bot's number and talk to your agent, or let Hermes ring you when something can't wait. It works with
the personal-number setup most Hermes users already have (WhatsApp linked as a device): no WhatsApp
Business or Cloud API account needed.

## Features

<table>
<tr>
<td width="50%" valign="top">

**📞 Call your bot**<br>
Talk to Hermes like a person. The voice hands real work to Hermes mid-call: "move my 3pm to
tomorrow", "what did Sam say about the invoice?"

</td>
<td width="50%" valign="top">

**🔔 Hermes calls you**<br>
For things that matter, Hermes rings instead of texting: an urgent reminder, an alert, a cron job
that needs a decision. No answer? It falls back to another channel.

</td>
</tr>
<tr>
<td valign="top">

**⚡ Natural and fast**<br>
Speech-to-speech with Gemini Live: ready about 0.2 s after you pick up, sub-second replies, and you
can talk over it.

</td>
<td valign="top">

**🧠 Your assistant, not a stranger**<br>
The voice takes its name, personality and memory from your Hermes. Optional chat-session mode makes
the call part of your WhatsApp chat with it.

</td>
</tr>
<tr>
<td valign="top">

**🔒 Owner-only by design**<br>
It answers only your number and calls only your number. No tool takes a destination, so a prompt
injection can't make it call anyone else.

</td>
<td valign="top">

**🧩 Nothing to build**<br>
Prebuilt, attested sidecar binaries download on first run. Pair, configure and watch calls from a
tab in the Hermes dashboard.

</td>
</tr>
<tr>
<td valign="top">

**🌍 Speaks your language**<br>
Pin the languages you speak, and your main one for speech recognition. Tamil, Hindi, Portuguese,
Japanese and two dozen more.

</td>
<td valign="top">

**🧪 Whole calls under test**<br>
Every PR runs simulated calls (dropped sockets, slow tools, hangups) in CI, and real-model tests run
before each release.

</td>
</tr>
</table>

After each call you get a short summary on WhatsApp, and every call keeps a record with its
transcript and timing (`hermes whatsapp-voice calls`). `hermes whatsapp-voice stats` lists each
call's length, Gemini tokens and estimated cost, with totals.

## Quick start

### Requirements

- Hermes `>= 0.21.6` on Linux or macOS.
- A separate WhatsApp number for the bot: Hermes's WhatsApp `bot` mode, or any spare number. In
  `self-chat` mode the bot is your own number, and you can't call yourself.
- Gemini set up in Hermes with an API key. The plugin reuses the key of Hermes's Gemini provider
  (`GOOGLE_API_KEY`, else `GEMINI_API_KEY`), so there is nothing extra to enter. Without one,
  `hermes model` (choose Gemini) adds it. Google login (Cloud Code OAuth) does not work for the
  Live API. The live models have a free tier, but Google may use free-tier data. On the paid tier a
  call costs roughly 3 to 5 cents a minute with the default settings (estimated from real calls; each
  call's tokens and estimated cost are in `hermes whatsapp-voice stats`).
- Optional, but needed for the voice agent to reach Hermes: the Hermes
  [API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) enabled.
  It must have an `API_SERVER_KEY`.

### Install

**Let your agent do it.** Paste this into Hermes (or any coding agent on that machine):

```text
Set up live WhatsApp voice calls for my Hermes by following this runbook step by step:
https://raw.githubusercontent.com/ashwanthbalakrishnan5/hermes-whatsapp-voice/main/skills/setup/SKILL.md
```

The [runbook](skills/setup/SKILL.md) is written for agents, and it stops to ask you only for your
number and to scan a QR code. After installing, Hermes can also load it as
`hermes-whatsapp-voice:setup`.

**Or by hand:**

```bash
hermes plugins install ashwanthbalakrishnan5/hermes-whatsapp-voice --enable
```

#### 1. Set your number

This is the only number that will ever be called, and the only caller that will be answered.

```bash
hermes config set plugins.entries.hermes-whatsapp-voice.settings.owner_number +15551234567
```

#### 2. Link the sidecar to the bot's WhatsApp number (once)

Easiest: open the Hermes dashboard, go to the **WhatsApp Voice** tab, click **Pair with
WhatsApp**, and scan the QR from the **bot's** phone (WhatsApp > Linked devices > Link a device).
From a terminal instead:

```bash
hermes whatsapp-voice pair                        # shows a QR code
hermes whatsapp-voice pair --phone +15550001111   # the BOT's number; prints a pair code
```

The first run also fetches the sidecar binary for your platform from this repo's GitHub release.
You don't need Rust or any other toolchain. The download is checked against checksums pinned in
this repo (`sidecar/release-checksums.txt`). If the GitHub CLI is installed, its GitHub build attestation is checked too, which
proves it was built from this repository by its release workflow. Prebuilt binaries cover linux
amd64/arm64 and macOS arm64. To build it yourself instead (Rust 1.94+, cmake, a C compiler), run
`hermes whatsapp-voice install --from-source`.

> **Why a separate link when Hermes is already logged in to WhatsApp?** WhatsApp lets each linked
> device keep only one live connection. Sharing Hermes's WhatsApp session would make the two
> connections knock each other offline, and Hermes's messaging would break. Only the bot's phone
> can approve a new device, so the link happens once. It uses one of WhatsApp's four linked-device
> slots.

#### 3. Enable the Hermes API server (recommended)

```bash
# in Hermes's .env (~/.hermes/.env by default)
API_SERVER_ENABLED=true
API_SERVER_KEY=<a long random string>
```

Restart the gateway after this.

#### 4. Run the sidecar

```bash
hermes whatsapp-voice serve      # foreground, to try it out
hermes whatsapp-voice service    # prints a systemd user unit (Linux) or launchd plist (macOS)
```

To keep it running, save the printed unit and enable it as the comments in it describe. Check it
with:

```bash
hermes whatsapp-voice status
hermes whatsapp-voice call "Testing the new voice line"
```

## Using it

Call your bot's number from your phone. Or ask Hermes to call you:

> Call me at 7:30 tomorrow about the flight check-in, and if I don't answer, set an urgent reminder.

`whatsapp_call_owner` works from cron runs too, so a scheduled job can escalate to a phone call:

```bash
hermes cron create "0 9 * * 1-5" \
  "If anything on my calendar today is marked urgent, call me about it with whatsapp_call_owner."
```

The tool waits until the call is answered, declined or unanswered, and returns that status. On
`no_answer`/`rejected`/`failed` it tells the agent to use another channel.

## How it works

```
 your phone ──WhatsApp call──▶ bot number ──▶ hermes-wa-voice (linked device, Rust)
                                                 │  call audio (16 kHz in, 24 kHz out, resampled)
                                                 ▼
                                            Gemini Live (speech-to-speech)
                                                 │  ask_hermes(...) tool calls
                                                 ▼
                                            Hermes API server (/v1/runs)
```

- **`hermes-wa-voice`** is a small sidecar built on
  [whatsapp-rust](https://github.com/oxidezap/whatsapp-rust). It links to the bot's WhatsApp number
  as one more linked device (WhatsApp allows four), answers calls from your number only, and places
  calls to your number only.
- Call audio is bridged to **Gemini Live** (`gemini-3.8-live` by default) for natural, interruptible
  speech-to-speech.
- The voice model gets two tools:
  - `ask_hermes`, which runs a Hermes turn through the API server and speaks the result when it
    arrives;
  - `end_call`, which hangs up after a goodbye.
- The **Hermes plugin** adds two agent tools: `whatsapp_call_owner(reason, details?)` and
  `whatsapp_call_status(call_id?)`. It also adds a `hermes whatsapp-voice` command for setup.

## Configure

Everything is configurable from the **WhatsApp Voice** tab in the Hermes dashboard, the Desktop
app's plugin settings, or `hermes config set`. That covers the voice, the model, your own
instructions (language, tone, call length), and any Gemini Live option via `live_config`. The
voice takes its name and personality from your own Hermes. Full reference:
[docs/configuration.md](docs/configuration.md).

## What this plugin does that you should know about

- **Long-running process.** The sidecar runs as a separate process that you start (`serve`, or the
  systemd/launchd unit). The plugin itself only talks to it over loopback HTTP, authenticated with a
  random token stored in the plugin's data directory.
- **Stored credentials.** The sidecar's WhatsApp linked-device session is stored at
  `<hermes home>/plugin-data/hermes-whatsapp-voice/whatsapp.db` (the folder is private, mode 700).
  Anyone with that file can act as that linked device. Unlink it any time from the bot's phone
  (Linked devices).
- **Network calls.**
  - WhatsApp servers (signaling and call media relays).
  - `generativelanguage.googleapis.com` (Gemini), using the Gemini key Hermes already has. It
    receives call audio during calls, plus, as text: the persona briefing (Hermes's name,
    personality and what it knows about you), the reason and details of an outgoing call, and the
    results of `ask_hermes` requests.
  - Your local Hermes API server, using `API_SERVER_KEY`, for `ask_hermes` (which runs a full
    Hermes turn with your tools), the persona briefing (fetched at startup and after each call),
    and the post-call summary. That summary run asks Hermes to note anything worth remembering, so
    it can write to Hermes's memory.
  - `github.com`, to download the sidecar binary on first `pair`/`serve`/`install` (or from the
    dashboard's Pair button).
  - If you change `voice_api_url`, that address instead of Gemini's: it gets the Gemini key and
    the call audio.

  Nothing else, and no telemetry.
- **Who it talks to.** It only answers calls from `owner_number` and only calls `owner_number`.
  Neither tool takes a destination parameter, so a prompt injection cannot make it call anyone else.
  Calls from other numbers are left alone, so the bot's other devices can still take them.
- **Commands it runs.**
  - The dashboard tab's backend (inside the Hermes dashboard, behind its login): `hermes config
    set` for this plugin's settings (only keys declared in `plugin.yaml`); the sidecar's `pair`;
    and `systemctl --user restart` or `launchctl kickstart` for the sidecar service.
  - The CLI: `gh attestation verify` when the GitHub CLI is logged in, and `cargo build` for
    `install --from-source`.
  - The setup skill (only when an agent follows it) appends `API_SERVER_ENABLED` and a generated
    `API_SERVER_KEY` to Hermes's `.env` if they're missing.
- **Chat-session mode** (`context_mode: core_session`, off by default). `serve` also runs a
  loopback service (`context_listen`, same token) inside Hermes's own process. For each call it
  sends Gemini your WhatsApp chat's system prompt, recent history (`history_chars`) and the tool
  schemas of `voice_tools`, runs the voice's tool calls through Hermes in that chat (only tools
  listed in `voice_tools`), and writes the call into the chat. It never answers Hermes's approval
  prompts: a command that needs approval is denied, and the voice tells you to ask again in the text
  chat. It relies on Hermes internals listed in `CORE_PATCHES.md`.
- **Recordings** (`record_calls`, off by default) save your side of each call as a WAV file in the
  plugin's private data folder. Nothing is uploaded; delete them any time.
- **Call stats** (`call_stats`, on by default) append each call's length, token counts and
  estimated cost to `call_stats.jsonl` in the same folder: numbers only, no transcript, reason or
  audio.
- **Secrets** are read through Hermes (`get_secret`) and passed to the sidecar in its environment.
  The plugin never reads `.env` itself. The sidecar's local API only binds to a loopback address.

## Caveats

- WhatsApp calling here uses an **unofficial**, reverse-engineered protocol (via whatsapp-rust).
  WhatsApp updates can break it, and automated use may violate WhatsApp's Terms of Service. Use a
  number you can afford to lose.
- One call at a time. Group calls and video are not supported.
- A Gemini Live connection lasts about 10 minutes. The sidecar resumes the session transparently, but
  a resume can fail and end a long call.

## Development

```bash
python -m pip install -r requirements-dev.txt
ruff check . && python -m pytest tests/ -q
cd sidecar && cargo test
```

### Testing calls

`cargo test` runs whole calls through the real call code (`sidecar/src/calls/sim.rs`) against a fake
phone, a fake Gemini Live server and a fake Hermes. The fakes speak the real wire formats, so each
test scripts a situation that has broken calls before: a `goAway` or a dropped socket mid-call, a
slow or cancelled tool, a hangup, a silent owner, the chat session being unavailable. It needs no
keys, costs nothing and runs in CI on every PR.

Every call (real or simulated) records how it went in `GET /calls` under `metrics`: time to the
first audio, each reply delay, the longest silence, tool calls in a row without speaking, token
usage and who ended the call. After a real call that went wrong, read those first.

The real model is tested only on request, since it costs money (a few cents a run):

```bash
cd sidecar
GEMINI_API_KEY=... cargo test live_model -- --ignored --nocapture
# HWV_MODEL=<model> to try another model; HWV_TEST_AUDIO=question.wav (16 kHz mono) to also
# play a spoken question, e.g. one saved with record_calls.
```

Before a release, also make one real call from your phone and check its `metrics`: WhatsApp's
protocol itself can only be tested with a real phone.

Layout: the repo root is the Hermes plugin (`plugin.yaml`, `__init__.py`, `tools.py`, `cli.py`,
`sidecar_client.py`), and `sidecar/` is the Rust sidecar. Tagging `vX.Y.Z` builds attested release
binaries (`.github/workflows/release.yml`). Then commit the release's `SHA256SUMS` as
`sidecar/release-checksums.txt`. Keep `version` in `plugin.yaml` and `sidecar/Cargo.toml` in step
with the tag.

## Contributing

Bug reports, fixes and features are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Issues labelled
[good first issue](https://github.com/ashwanthbalakrishnan5/hermes-whatsapp-voice/labels/good%20first%20issue) are a good place to start.
Security issues go through [private reporting](SECURITY.md), not public issues.

<a href="https://github.com/ashwanthbalakrishnan5/hermes-whatsapp-voice/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=ashwanthbalakrishnan5/hermes-whatsapp-voice" alt="Contributors">
</a>

## Star history

<a href="https://star-history.com/#ashwanthbalakrishnan5/hermes-whatsapp-voice&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=ashwanthbalakrishnan5/hermes-whatsapp-voice&type=Date&theme=dark">
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=ashwanthbalakrishnan5/hermes-whatsapp-voice&type=Date">
    <img alt="Star history chart" src="https://api.star-history.com/svg?repos=ashwanthbalakrishnan5/hermes-whatsapp-voice&type=Date">
  </picture>
</a>

## Credits

- [whatsapp-rust](https://github.com/oxidezap/whatsapp-rust) (MIT) does the WhatsApp protocol and the
  call media work.
- Gemini Live provides the voice.

## License

MIT
