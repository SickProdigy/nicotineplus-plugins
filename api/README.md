# Soulseek HTTP API for Nicotine+

Control searches and downloads through your existing Nicotine+ session while
keeping its normal GUI and sharing configuration. This plugin uses Python's
standard library; it does not install another Soulseek client.

The initial supported target is **Nicotine+ 3.3.11 with Python 3.9 or newer**.
Newer versions are accepted but require validation because the plugin uses
internal search, download and event interfaces. Tests cover local HTTP requests,
the 3.3.11 search-token behavior and download journal recovery. Live Soulseek
downloads and GUI installation still need deployment verification.

An additional offline smoke check passes against the actual upstream 3.3.11 core:
HTTP dispatch, native search tokens, binary Soulseek result parsing, native
download enqueue and completion. Run it with
`python -B tests/verify_nicotine_core.py --source /path/to/nicotine-plus-3.3.11`.
It uses isolated temporary configuration and disables Soulseek network sends.
Add `--arr-scripts /path/to/arr-scripts` to run all three actual workers against
this plugin/core, with simulated completed files and Arr API responses. This
checks integration and import payloads; it does not prove live peer transfers
or acceptance by a deployed Arr instance.

## Install

Download the `api` directory from the public
[nicotineplus-plugins repository](https://github.com/SickProdigy/nicotineplus-plugins).
Put the whole directory, including `PLUGININFO` and `__init__.py`, in Nicotine+'s
plugin directory. In Nicotine+'s plugin preferences, enable **Soulseek HTTP API**
and open its settings.

Configure:

| Setting | Default | Purpose |
| --- | --- | --- |
| Listen address | `0.0.0.0` | Accept connections from Arr on the private Docker network; use `127.0.0.1` for local-only access |
| HTTP API port | `12339` | Port used by clients |
| API key | Empty | Required, at least 32 characters |
| Completed download root | Empty | Empty uses Nicotine+'s download directory; choose a shared directory for Arr |

Generate a key locally, for example `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
Existing saved settings are preserved on upgrade. If you installed an earlier
version, change the saved listen address from `127.0.0.1` to `0.0.0.0` for Docker
connections; updating the plugin alone does not replace that setting.
The first enable without a key logs a configuration message and does not start
the listener. After setting the key or changing settings, disable/re-enable the
plugin. Changing settings does not hot-reconfigure a running listener.

For Docker, listen on `0.0.0.0` inside Nicotine+'s container, expose port `12339`
only to the relevant private Docker network, and mount the completed download
directory into the Arr containers. A loopback-only listener is unreachable from
a separate container. For a remote host, use a private network or an HTTPS
reverse proxy. The built-in server speaks HTTP and requires a key on every request.

Your Soulseek login stays in Nicotine+. Nicotine+ must be running and connected
for searches and transfers; the plugin does not start or reconnect it.

## arr-scripts

In the Arr application's `/config/extended.conf`:

```bash
enableSoulseek="true"
soulseekClient="nicotine"
soulseekUrl="http://nicotine:12339"
soulseekApiKey="THE_PLUGIN_API_KEY"
soulseekDownloadPath="/downloads/soulseek"
```

Mount Nicotine+'s completed root as `/downloads/soulseek` in each participating
Arr container. The paths can differ inside Nicotine+ because the API's batch
destination is relative to its root. Lidarr additionally selects
`dlClientSource="soulseek"`, `"all"` or a list such as `"deezer,soulseek"`.
Movie and TV fallback uses the Radarr and Sonarr wanted lists.

The import/matching worker lives in
[arr-scripts](https://github.com/SickProdigy/arr-scripts), not in this plugin.
The plugin has no Arr API keys or knowledge of movies, episodes or albums.

## API v1

Authenticate with `X-API-Key` or `Authorization: Bearer <key>`.
Keys in query parameters are not accepted. POST requests use `application/json`.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/v1/application/version` | Plugin version as a JSON string |
| GET | `/api/v1/application` | Nicotine version, connection status and capabilities |
| POST | `/api/v1/searches` | Start a bounded global search with a caller-provided UUID |
| GET | `/api/v1/searches/{uuid}` | Search status and public file results |
| POST | `/api/v1/transfers/downloads/batches` | Download selected search results to a relative directory |
| GET | `/api/v1/transfers/downloads/batches/{uuid}` | Durable batch and transfer states |

Example search body:

```json
{"id":"afddfe36-6c6c-4b49-bc27-b2836822c454","searchText":"Artist Album","searchTimeout":15000}
```

Poll until `isComplete` is true. `responses` contains peer usernames and
`files` entries with `filename`, `size`, and optional audio attributes.
Searches last 1–60 seconds, with at most three concurrent API searches and
100 peers/10,000 public files per search. Locked files are not exposed.

Example download body (use filenames/sizes from an actual response):

```json
{
  "id":"d8b060d4-c86d-4704-8781-239ad4c21f0d",
  "searchId":"afddfe36-6c6c-4b49-bc27-b2836822c454",
  "username":"peer-name",
  "files":[{"filename":"Shared\\Artist\\Album\\01 Song.flac","size":12345678}],
  "options":{"destination":"arr-scripts/lidarr/d8b060d4-c86d-4704-8781-239ad4c21f0d"}
}
```

A batch requires a known search response, a relative destination within the
configured root, distinct local basenames, and at most 250 files. It preserves
Nicotine+'s download filters. It does not cancel, move, delete or redirect other
GUI downloads. Conflicting pre-existing transfers are rejected.

Batch states use `Queued`, `InProgress` and `Completed, Succeeded` or a terminal
failure. Completion requires the expected file and size in the batch directory.
Partial and failed batches remain inspectable; the plugin does not delete files.

## Recovery and limits

Searches and batches are journaled in `api/jobs.sqlite` under Nicotine+'s data
directory. They survive plugin reloads and process restarts. A repeated search
UUID with the same text returns the same search; different text returns 409.
Repeated batch UUIDs return 409; fetch the existing batch to reconcile it.

An HTTP timeout can occur after a write was accepted. Always fetch the same UUID
before retrying. API operations are dispatched onto Nicotine+'s main thread;
HTTP handler threads do not call core methods or touch the journal directly.
Pending searches lose their network tokens on restart and become empty completed
searches, allowing clients to retry later. Existing Nicotine transfers continue
normally. Uncertain or manually cleared transfers are not blindly resubmitted.

This is a small automation API, not a replacement for the entire slskd API.
It has no chat, upload management, arbitrary filesystem operations or shell
execution endpoints. API state is retained locally; the journal may need periodic
archiving as a large library grows.


## Clearing an automation batch (API 0.1.2+)

`POST /api/v1/transfers/downloads/batches/<UUID>/clear` clears only the
terminal Nicotine download rows owned by that batch. It checks every row's
peer/file identity, size, and destination before changing anything. Queued or
active owned transfers return HTTP 409. If Nicotine has reused the peer/file
key for a newer download in another destination, the old batch detaches from
that foreign row without changing it. Repeating a successful request is safe.

The endpoint never deletes files or cancels active downloads. Arr-scripts uses
it before deleting owned completed/failed files and trying another candidate.
Update the API plugin files and disable/re-enable the plugin to activate it.


## Offline status and cancellation (API 0.1.3+)

Batch status includes `peerOffline` and each transfer's Nicotine `status` and
`bytesTransferred`. Offline is true only when all unfinished transfers report
`User logged off`, so an online queue or slow transfer is not called offline.
Nicotine itself automatically retries when the peer returns; clients do not
need to pause or resubmit the batch.

`POST /api/v1/transfers/downloads/batches/<UUID>/cancel` aborts only that batch's
owned transfers, leaving finished rows intact. A transfer in another destination
belongs to another batch and is left unchanged; an owned transfer with changed
metadata returns HTTP 409. Repeating the request is safe. Use the separate
`/clear` operation afterward to clear rows.

An optional JSON body `{"cleanupIncomplete": true}` also removes the cancelled
batch's validated partial files after Nicotine closes their handles. Files must
be within Nicotine's incomplete root, unlinked to other files, and no larger
than their expected size. The endpoint never deletes completed files or personal
transfers. Arr-scripts separately removes its owned completed leftovers.
