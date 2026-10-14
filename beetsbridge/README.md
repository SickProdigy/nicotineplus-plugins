# Beets Post-Download

Beets Post-Download queues a completed Nicotine+ music folder for a
non-interactive `beet import`. Nicotine+ supplies the completed file path;
beets supplies metadata matching, tags, library database updates, destination
layout, and enabled beets plugins.

Automatic importing is disabled by default.

## Prerequisites

Beets must be installed in the same operating environment as Nicotine+. If
Nicotine+ runs in Docker, add beets to that image (or a purpose-built image)
and mount the beets config, database, download directory, and organized music
directory into that container. Installing beets only on the Docker host does
not make the `beet` command available inside the container.

Configure and test beets manually before enabling this plugin. A minimal config
might contain:

```yaml
directory: /music
library: /config/beets/library.db
import:
  copy: yes
  write: yes
  resume: no
  quiet_fallback: skip
paths:
  default: $albumartist/$album%aunique{}/$track $title
  singleton: Non-Album/$artist/$title
  comp: Compilations/$album%aunique{}/$track $title
```

Your preferred naming, plugins, duplicate behavior, art handling, and metadata
rules belong in the beets YAML configuration.

## Plugin workflow

1. Nicotine+ announces each completed download.
2. Audio files are grouped by their containing folder.
3. The folder's timer resets whenever another configured audio file completes.
4. After the settle delay, one serialized `beet import` process runs outside the
   Nicotine+ main thread.
5. The exit status is reported in the Nicotine+ log.

The settle delay cannot prove that an album has no remaining slow downloads.
Use a comfortably long delay for automatic imports, or leave automation off and
run `/beetsimport <folder>` after checking the download.

## Settings

- **Allowed download root** is required for automatic imports. Folders outside
  it are rejected. The root itself is not imported, preventing an accidental
  whole-download-tree import.
- **Import mode** is `copy`, `move`, or `inplace`.
  - `copy`: passes `-c -M`; files are copied into the beets library.
  - `move`: passes `-m`; source files can be removed after a successful move.
  - `inplace`: passes `-C -M`; files remain where downloaded.
- **Quiet fallback** is `skip` (safer default) or `asis`. Beets decides what to
  do when matching is uncertain.
- **Config path**, **library path**, and **destination** map to the global beets
  `-c`, `-l`, and `-d` overrides.
- **Incremental** adds `-i` to the import.

Move mode is destructive to the source location by design. Test with copy mode
and backups before enabling it.

## Commands

- `/beetsstatus` shows whether automation is enabled, the mode, and allowed
  root.
- `/beetsimport <folder>` schedules an immediate import. If an allowed root is
  configured, the folder must be beneath it.

## Install for local review

Copy the complete `beetsbridge` directory into the Nicotine+ user plugins
directory, restart Nicotine+, and enable **Beets Post-Download**. Configure the
paths, test `/beetsimport` with copy mode, and only then enable automatic import.
No shell is used to execute beets, and imports are serialized.
