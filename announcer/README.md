# Room Announcer

Room Announcer previews or sends one message to a controlled set of Nicotine+
rooms. Soulseek calls these chats **rooms**; this plugin's named **groups** are
local aliases that collect rooms you choose.

Sending is disabled by default. In preview mode, commands show the final message
and targets only in your local client. The plugin never auto-joins rooms and only
targets explicitly configured rooms that you are currently in.

## Configuration

Set **Default rooms** to a comma-separated list used by `/announce`, for example:

```text
Linux, Open Source
```

Set **Named groups JSON** to a JSON object whose values are room lists:

```json
{"opensource": ["Linux", "Open Source"], "media": ["Music", "Books"]}
```

Review previews before enabling **Actually send announcements**. Messages are
queued with a configurable delay between rooms, and an identical message to the
same targets is blocked during the duplicate cooldown. The prefix and final
message-length limit are also configurable.

## Commands

- `/announce <message>` — default rooms
- `/announcegroup <group> <message>` — one named group
- `/announceall <message>` — union of all explicitly configured rooms
- `/announcecancel` — cancel all queued sends
- `/announcestatus` — show mode, configured targets, and queue size

Commands are owner-side slash commands, not remote chat triggers. Preview and
status output are local echoes and are not posted to the room. Keep intervals
conservative and use this for occasional, relevant announcements where room
rules permit them; the plugin intentionally has no recurring schedule.

## Requirements

Nicotine+ 3.3.11 or newer and Python 3.9 or newer. No additional packages or
credentials are required.
