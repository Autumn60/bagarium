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
# Index bags (re-running only updates what changed)
bagarium scan ~/rosbags /mnt/hdd/bags

# List / search
bagarium ls
bagarium ls outdoor                         # substring match on name, path and note
bagarium ls -t calib -t lidar               # tags (AND)
bagarium ls --topic '/camera/*/image_raw'   # topic name (substring or glob)
bagarium ls --type PointCloud2              # message type
bagarium ls --since 2026-10-01 --until 2026-10-07
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

### Catalog database location

Defaults to `~/.local/share/bagarium/bagarium.db`. Override with `--db PATH` or the `BAGARIUM_DB` environment variable.

## Web UI

- List: search by keyword, topic, message type and date; click tags to filter
- Detail: metadata, topic list (message counts and average rates), tag and note editing
- JSON API: `/api/bags`, `/api/bags/{id}` (spec at `/api/docs`)

The server listens on `127.0.0.1` only by default. There is no authentication, so be careful when exposing it with `--host 0.0.0.0`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
