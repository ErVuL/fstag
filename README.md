# fstag

A lightweight desktop app to **tag files with colored labels** and browse them in a file-system UI. Built with Python and Tkinter — no external dependencies.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## Features

- **Colored tags** — create, rename, recolor, and delete tags with a preset color palette
- **Tag filtering** — filter files by one or more tags (match *any* or *all*)
- **Search** — real-time debounced search across file names, paths, and tags
- **Directory browsing** — navigate folders with breadcrumb navigation and an "Up" button
- **Virtual scrolling** — handles large directories smoothly by only rendering visible rows
- **Move/rename detection** — tracks files by content fingerprint so tags survive renames and moves
- **Batch tagging** — select multiple files and assign/remove tags in one click
- **Right-click context menu** — add/remove tags, open files or folders
- **Auto-reconcile** — re-scans the filesystem when the window regains focus
- **Cross-platform** — works on Linux, macOS, and Windows

## Installation

```bash
pip install .
```

Or install in development mode:

```bash
pip install -e .
```

## Usage

```bash
# Tag and browse files in the current directory
fstag

# Tag and browse files in a specific directory
fstag ~/Documents
```

You can also run it as a module:

```bash
python -m fstag ~/Projects
```

## How it works

All tag data is stored in a single `.fstag.json` file at the root of the managed directory. This file is portable — copy it alongside your files and the tags follow.

```
my-project/
  .fstag.json      # tag data (auto-managed)
  report.pdf
  notes.txt
  src/
    main.py
```

Files are tracked by relative path and a content fingerprint (partial SHA-256). If a file is moved or renamed, fstag matches it by fingerprint and preserves its tags.

## Project structure

```
fstag/
  __init__.py    # Package metadata
  __main__.py    # CLI entry point and argument parsing
  store.py       # JSON-based tag store with filesystem reconciliation
  ui.py          # Tkinter UI with virtual scrolling
```

## License

[MIT](LICENSE)
