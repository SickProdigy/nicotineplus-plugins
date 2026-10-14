# Wolfram

Wolfram adds compact Wolfram|Alpha text answers to Nicotine+ room and private
chat. It adapts the existing Sick-Cogs integration to Nicotine+'s plugin API and
uses only Python's standard library.

## Setup

Create a Wolfram|Alpha AppID through the Wolfram developer portal and enter it
in the plugin's **App ID** setting. Never paste the AppID into chat, logs, issues,
or screenshots. Nicotine+ stores plugin settings in its configuration, so
protect that file appropriately.

## Commands

- `/wolfram <question>` asks a factual, mathematical, or scientific question.
- `/wa <question>` is an alias.
- `/wolframsolve <question>` requests step-by-step result pods.
- `/wasolve <question>` is an alias.

Examples:

```text
/wolfram 2+2
/wolfram population of Japan
/wolfram derivative of x^3
/wolframsolve x^2 - 5x + 6 = 0
```

The plugin requests Wolfram's Full Results XML endpoint with plaintext pods,
compacts whitespace, omits geo-IP-location answers, limits result sections, and
splits long answers into bounded Soulseek chat messages. Its optional source
link points to the public Wolfram input page and does not contain the AppID.

## Remote queries

Remote use is disabled by default. To enable it:

1. Add exact comma-separated Nicotine+ usernames to **Approved users**.
2. Optionally add a comma-separated room allowlist.
3. Enable **Allow remote queries**.

Approved users type `!wolfram <question>`. Room-triggered answers are returned
privately to avoid flooding the room. Per-user cooldowns and a global
concurrency cap protect the AppID quota.

## Errors

Authentication rejection, rate limiting, timeout, network failure, provider
failure, malformed XML, and no-result responses receive distinct safe messages.
The AppID is never logged.

## Images

The first Nicotine+ version is text-only. Wolfram's Simple API places the AppID
in the image request URL, which must not be posted to chat, and Soulseek chat
does not provide Discord's safe file-attachment workflow. Image support would
need a separate authenticated proxy or local temporary-file transfer design.

## Install for local review

Copy the complete `wolfram` directory into the Nicotine+ user plugins directory,
restart Nicotine+, enable **Wolfram**, configure the AppID, and try `/wolfram
2+2`. No third-party Python packages are required.
