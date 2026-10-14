# 🎵 NicotinePlus Plugins

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.6+-green.svg)](https://www.python.org/)
[![Nicotine+](https://img.shields.io/badge/Nicotine+-Compatible-brightgreen.svg)](https://nicotine-plus.org/)

> Enhance your Nicotine+ experience with powerful, community-driven plugins.

## 📋 Table of Contents

- [About](#about)
- [Available Plugins](#available-plugins)
- [Installation](#installation)
- [Contributing](#contributing)

## About

This directory contains plugins for **Nicotine+**, a graphical client for the Soulseek file sharing network. 

A plugin is a Python module that extends the functionality of the main application. Plugins can:
- ✨ Add new features
- 🔧 Modify existing features
- 🔗 Integrate with other applications
- 🤖 Automate common tasks

## Available Plugins

### [Book Search](booksearch)

Search lawful ebook and audiobook catalogs from room or private chat without
blocking Nicotine+'s UI. Remote `!book` requests are opt-in, room-filterable,
and rate-limited. The initial provider is Open Library, with a provider
interface for adding other authorized catalogs later.

### [Local AI Chat](localai)

Connect room or private chat to a local Ollama or llama.cpp server without
blocking Nicotine+'s UI. Owner `/ai` prompts work immediately after server
configuration; remote `!ai` prompts remain opt-in, room-filterable,
rate-limited, concurrency-limited, and bounded for chat-safe replies.

### [Beets Post-Download](beetsbridge)

Queue completed music folders for a configured, non-interactive beets import.
Automatic imports are disabled by default and guarded by an allowed download
root, file-type filtering, a settle timer, serialized execution, explicit
copy/move/in-place modes, and bounded subprocess timeouts.

### [Auto Spam Ignore](autospamignore)

Apply configurable rolling message-rate, phrase, and observed-country rules in
joined rooms, with local room and desktop notifications. Enforcement is disabled
by default; matching users can optionally be added to Nicotine+'s persistent
ignore list. Room scopes, trusted-user exemptions, status, and counter-reset
commands are built in.

### [Arr Requests](arrrequests)

Approved-user, read-only lookups for Sonarr, Radarr, and Lidarr from room or
private chat. The staged foundation verifies connections and searches catalogs,
but cannot add, monitor, search for releases, or download titles. Remote access
is disabled by default; API keys stay in plugin settings and results requested
in rooms are returned privately.

### [Wolfram](wolfram)

Query Wolfram|Alpha for compact plaintext answers and step-by-step result pods
from Nicotine+ chat. Remote use is disabled by default and restricted to
approved usernames, optional rooms, cooldowns, and concurrency limits. AppIDs
are kept out of chat, source links, and logs; image mode is intentionally
omitted because Soulseek chat has no safe attachment equivalent.

### [Room Announcer](announcer)

Preview or send a message to explicitly configured, currently joined rooms. Named
room groups, staggered delivery, duplicate cooldowns, cancellation, and local-only
status output keep announcements controlled. Sending is disabled by default, and
the plugin has no remote trigger, auto-join behavior, or recurring schedule.

### [Soulseek HTTP API](api/README.md)

Authenticated searches and download batches through your existing Nicotine+ session.
Works with SickProdigy's [arr-scripts](https://github.com/SickProdigy/arr-scripts)
for optional Lidarr, Sonarr and Radarr downloads. Initial supported target:
Nicotine+ 3.3.11 and Python 3.9+. See the plugin documentation for API settings
and current validation limits.


### 🤖 [Autobuddy](autobuddy)

Automatically manage your buddy list based on channel interactions.

**Features:**
- Auto-add users who join specific channels
- Customizable trigger phrases (e.g., "add me plz")
- Perfect for communities like **SickGaming.net**

**How it works:**
When a user joins a monitored channel and says a trigger phrase, anyone with this plugin enabled will automatically add them to their buddy list.

---

### 📚 [ExamplePlugin](exampleplugin)

A reference implementation demonstrating best practices for Nicotine+ plugin development.

**Perfect for:**
- Learning plugin architecture
- Understanding Nicotine+ API
- Creating custom plugins

---

## Installation

1. Clone or download the plugin files
2. Place them in your Nicotine+ plugins directory
3. Restart Nicotine+ to load the plugins
4. Enable plugins in Preferences → Plugins

## Contributing

Contributions are welcome! Feel free to:
- Report issues
- Suggest new features
- Submit pull requests

---

**Made with ❤️ for the Soulseek community**