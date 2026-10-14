# Arr Requests

Arr Requests is a staged Nicotine+ request gateway for approved users. Version
0.1.0 is intentionally **lookup only**: it can verify connections and search
Sonarr, Radarr, and Lidarr catalogs, but it cannot add, monitor, search for, or
download any title.

## Current commands

- `/arrstatus` checks configured Arr connections without exposing API keys.
- `/arrlookup tv <query>` searches Sonarr.
- `/arrlookup movie <query>` searches Radarr.
- `/arrlookup music <query>` searches Lidarr artists.
- Approved users can type `!request tv|movie|music <query>` after remote requests
  are enabled. Results are returned privately, even if requested in a room.

Unapproved users receive no response. Approved-user matching is
case-insensitive, and each approved user has a configurable cooldown.

## API support

- Sonarr: `GET /api/v3/system/status` and `GET /api/v3/series/lookup`
- Radarr: `GET /api/v3/system/status` and `GET /api/v3/movie/lookup`
- Lidarr: `GET /api/v1/system/status` and `GET /api/v1/artist/lookup`

Authentication uses the standard `X-Api-Key` header. Keys are plugin settings
and are never included in chat messages, URLs, or logs. Nicotine+ stores plugin
settings in its configuration, so protect that file appropriately.

## Configuration

Set each application's base URL and API key. If Nicotine+ is containerized,
`127.0.0.1` refers to the Nicotine+ container. Prefer a shared internal Compose
network and service URLs such as `http://sonarr:8989`, `http://radarr:7878`, and
`http://lidarr:8686`. Avoid publishing Arr APIs directly to the internet.

Leave **Allow remote requests** disabled while testing `/arrstatus` and owner
lookups. Then populate **Approved users** with exact comma-separated Nicotine+
usernames before enabling remote lookups.

## Why adding requests is not implemented yet

A safe create-and-search workflow needs explicit decisions for every service:

- root folder;
- quality or metadata profile;
- language profile where applicable;
- tags identifying Nicotine+ requests;
- monitoring mode and season handling;
- minimum availability;
- whether to launch an automatic search immediately;
- duplicate/exclusion handling;
- request audit history and cancellation;
- per-user quotas and optional owner approval.

Hard-coding any of these can put titles in the wrong library, use an unwanted
profile, or launch downloads unexpectedly. The next stage should discover and
validate profiles/root folders, save a pending request record, notify the owner,
and require `/requestapprove <id>` before any Arr POST request. Automatic
approval can be considered later for selected users.

## Ebooks

No ebook adapter is enabled. Readarr was the familiar Arr-style book manager,
but its official repository is archived. A later adapter should target whichever
maintained book service you choose and be reviewed against that service's actual
API rather than assuming Readarr remains suitable.

## Install for local review

Copy the complete `arrrequests` directory into the Nicotine+ user plugins
directory, restart Nicotine+, enable **Arr Requests**, add one service URL/key,
and run `/arrstatus`. No third-party Python packages are required.
