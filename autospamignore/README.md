# Auto Spam Ignore

Auto Spam Ignore provides configurable local moderation for public rooms that
Nicotine+ has joined. It can detect excessive message rates, selected phrases,
and observed user country codes. The default rate trigger is 20 messages in 60
seconds in the same room; both values are editable in Nicotine+'s plugin
settings.

Detection starts in **log-only mode**. Enable **Enforce ignores** after reviewing
the room list, exemptions, and threshold. Enforced detections call Nicotine+'s
normal `ignore_user` API, so the username is added to the regular persistent
ignore list and can be reviewed or removed through Nicotine+.

Every detection is written to the log. Local room echoes and high-priority
desktop notifications are enabled by default and can be disabled independently.
The local room echo is visible only in your client and is not sent publicly.

## Behavior

- Only public messages visible to this Nicotine+ client are evaluated.
- The owner's username is always exempt.
- Buddies are exempt by default.
- Private-room owners and operators are exempt by default.
- Additional usernames can be supplied as a comma-separated exemption list.
- A comma-separated room list can restrict monitoring. An empty list means all
  joined rooms.
- `per_room` scope counts independently in each room. `all_rooms` combines a
  user's messages across all monitored joined rooms.
- The message threshold and rolling-window seconds are independently editable.
- A rolling window is used rather than fixed clock-minute buckets.
- **Blocked countries** accepts comma-separated two-letter codes such as
  `RU,CN`. It uses the country Nicotine+ observed for the room user and applies
  a username ignore; it does not create an IP-range or permanent national
  network block. Unknown countries do not match. VPNs and geolocation errors can
  make country data inaccurate.
- **Blocked phrases** accepts comma-separated phrases and compares them
  case-insensitively after whitespace normalization.
- `contains` phrase mode matches text anywhere in a message. `exact` requires
  the complete normalized message to equal a configured phrase.
- Country and phrase rules honor the same room filters, owner/buddy/staff
  exemptions, explicit user exemptions, enforcement switch, and notifications
  as the rate rule.
- Ignoring is username-based, not IP-based.
- The plugin never automatically unignores anyone.

This is a rules engine, not a semantic classifier. Legitimate users can meet a
rate rule or quote a blocked phrase, and country data can be misleading. Use
log-only observation and explicit exemptions before enabling enforcement.

## Commands

- `/spamstatus` shows the mode, threshold, window, scope, monitored rooms,
  country rules, phrase-rule count, notification state, and active counters.
- `/spamreset` clears all rolling counters and rule-alert suppression state.
- `/spamreset <username>` clears counters and rule-alert state for one user.

Resetting counters does not modify Nicotine+'s existing ignore list.

## Install for local review

Copy the complete `autospamignore` directory into the Nicotine+ user plugins
directory, restart Nicotine+, enable **Auto Spam Ignore**, and leave enforcement
off while reviewing its log output. No third-party Python packages are required.
