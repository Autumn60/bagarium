# bagarium

A local catalog for ROS 2 bags (mcap / sqlite3).
bagarium scans directories, indexes bag metadata (time range, topics, message counts, size) into SQLite,
and lets you tag, annotate, search and browse bags from a CLI or a local web UI.

- **No ROS required**: works with a plain `pip install` (no need to source ROS 2)
- **Supported layouts**: bag directories with `metadata.yaml`, and standalone `.mcap` / `.db3` files
- **Bags are never modified**: tags and notes live in the catalog database

## Installation

```bash
pip install -e .          # for development
# or
pipx install .
```

## Usage

```bash
# Index bags (re-running only updates what changed).
# Directories given here are registered as roots.
bagarium scan ~/rosbags /mnt/hdd/bags
bagarium scan                    # rescan all registered roots

# Registered roots
bagarium roots                   # list
bagarium roots add ~/rosbags     # register without scanning
bagarium roots rm ~/rosbags      # unregister (indexed bags are kept)

# List / search
bagarium ls
bagarium ls outdoor                         # substring match on name, path and note
bagarium ls -t calib -t lidar               # tags (AND)
bagarium ls --topic '/camera/*/image_raw'   # topic name (substring or glob)
bagarium ls --type PointCloud2              # message type
bagarium ls --since 2026-10-01 --until 2026-10-07
bagarium ls --root ~/rosbags                # only bags under this folder
bagarium ls --json
bagarium ls -t good --paths | xargs -n1 ros2 bag play   # print paths only, for piping

# Details
bagarium show 12                 # by ID or path
bagarium show ~/rosbags/run_001

# Tags and notes
bagarium tag 12 outdoor lidar
bagarium untag 12 lidar
bagarium note 12 "sunny, good GNSS"
bagarium note 12                 # edit in $EDITOR
bagarium tags                    # list tags

# Extract a time range and/or topics into a new bag (same storage format, playable with ros2 bag play)
bagarium extract 12 -s 60 -d 30                       # 30 s starting 60 s into the bag
bagarium extract 12 -s 1:00 -e 1:30 -t '/sensing/*' -x '*/image_raw'
bagarium extract 12 -s 2026-09-03T16:40:00 -e 2026-09-03T16:41:00 -o ~/rosbags/cut

# Several bags at once: same topics / range applied to each, one output per bag
bagarium extract 3 5 8 -s 0 -d 60 -t '/sensing/*' -O ~/rosbags/cuts
bagarium extract $(bagarium ls -t outdoor --paths) -x '*/image_raw' --suffix no_images

# Move bags (ids, tags and notes are kept)
bagarium mv 12 /mnt/hdd/archive              # into an existing folder
bagarium mv 12 ~/rosbags/run_renamed         # rename (single bag, new path)
bagarium mv -n $(bagarium ls -t done --paths) /mnt/hdd/archive   # dry run

# Import (copy) bags from another disk; run again later to pick up only new bags
bagarium import -n /media/me/SSD/rosbags ~/rosbags/ssd    # dry run
bagarium import /media/me/SSD/rosbags ~/rosbags/ssd
bagarium import /media/me/SSD/rosbags ~/rosbags/ssd -i '2026-09-*/*' -x '*/derivatives/*'   # pick bags

# Web UI (http://127.0.0.1:8000/)
bagarium serve
```

### How the index is updated

- Bags whose size and mtime are unchanged are not re-read (use `--force` to override).
- Bags that disappear from a scanned directory are kept and flagged as **missing**, along with their tags and notes.
  - Show them with `bagarium ls --missing`; remove them from the catalog with `bagarium prune`.
- If a bag is moved or renamed, the next scan recognizes it as **moved** when it finds a bag with identical content
  (start/end time, message count and size), and carries its tags and notes over.
- Unreadable bags are still registered with an `error` (see the reason with `bagarium show`).
- Bags nested inside a bag directory (e.g. `<bag>/derivatives/<cut>`) are indexed as separate bags.

### Extraction

- Times are seconds from the bag start (`90`, `1:30`, `1:02:03`) or local date-times; both ends are inclusive.
- Topic patterns match the whole name, with glob wildcards (`/tf` does not match `/tf_static`).
- The output is a bag directory with `metadata.yaml`, in the source's storage format
  (sqlite3 output uses the Humble schema, so any ROS 2 distro can play it). Split bags are merged,
  and zstd-compressed bags (file or message mode) are written uncompressed.
- The output defaults to `<bag>_extract_<timestamp>` next to the source, and is added to the catalog
  with a note describing where it came from (`--no-index` to skip).
- With several bags, time offsets count from each bag's own start, outputs are named `<bag>_<suffix>`
  (next to each source, or in `--output-dir`; clashing names get `_2`, `_3`, ...), bags that have no
  matching topics, do not overlap the range, or are missing/unreadable are skipped, and a bag that fails
  does not stop the rest.

### Moving

- Like `mv`: an existing directory receives the bags under their own names; a single bag with a
  path that does not exist yet is renamed (standalone `.mcap` / `.db3` keep their extension).
  Several bags always go into the folder, which is created if needed.
- Bags nested in a moved bag directory (e.g. `derivatives/`) move along and are updated too.
- Within a filesystem it is a rename. Across filesystems the bag is copied to a hidden staging folder
  next to the destination, checked, put in place, and only then is the source deleted; a failed or
  cancelled copy leaves the source untouched.
- Moves are refused when the destination exists, when a bag would move into itself, or when two
  selected bags have the same name. Moving outside the registered roots works, but those bags are not
  revisited by `bagarium scan` (a note is printed).

### Importing

- Copies every bag under the source to the destination, keeping the folder layout below the source
  (so same-named bags in different folders do not clash). The source is never modified.
- Bags already in the catalog with the same start/end time, message count and size are skipped,
  so importing the same disk again only copies new recordings. (Identical bags within the source
  itself are each copied.)
- Copies go through a hidden staging folder and are size-checked before being put in place;
  timestamps are kept. Nested bags are copied as separate bags at their relative location.
- Pick bags with `--include` / `--exclude` globs on the path relative to the source, or with the
  checkboxes in the web UI's preview. A nested bag can be imported before its parent; the parent can
  still be imported into the same folder later.
- Stops before copying if the destination does not have enough free space.
- The destination is registered as a root, imported bags are added to the catalog, and tags/notes of
  sources that were already catalogued are carried over.

### Catalog database location

Defaults to `~/.local/share/bagarium/bagarium.db`. Override with `--db PATH` or the `BAGARIUM_DB` environment variable.

## Web UI

- List: search by keyword, topic, message type, date and registered folder (dropdown); click tags to filter; select bags to extract from or move
- Detail: metadata, topic list (filterable, with message counts and average rates), tag and note editing,
  and extraction (pick topics and a time range; runs in the background)
- Extract: batch extraction from the selected bags, with topics shown across all of them
- Move: move the selected bags (or one bag from its page) to a folder
- Import: preview an import from another disk, pick the bags to copy, and queue it
- Jobs: scans and extractions started from the UI are queued and run one at a time, in order.
  The page shows the running job's progress, the queue (remove queued jobs, cancel the running one)
  and finished jobs with per-bag results. Outputs of queued extractions are reserved, so jobs queued
  with the same names do not collide. The queue lives in the server process and is lost on restart.
- Scan: register / remove folders and scan them in the background with a progress bar (cancellable)
- JSON API: `/api/bags`, `/api/bags/{id}`, `/api/jobs`, `/api/jobs/{id}` (spec at `/api/docs`)

The server listens on `127.0.0.1` only by default. There is no authentication, so be careful when exposing it with `--host 0.0.0.0`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
