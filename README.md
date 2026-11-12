# DiskMan

See what fills your disks and clean up safely. Single-file Python (stdlib only) web app for macOS, Linux and Windows.

- Treemap, sunburst, charts, file explorer; scans several disks in parallel, separately or merged
- Fast name search over every file, duplicates (full content hash), old files, empty folders, growth over time
- Relationship graph: folders, file types, Docker containers and apps
- Cleanup rules, suggestions, browser caches (Chrome, Edge, Brave, Chromium, Vivaldi, Firefox), Docker prune
- Scheduled auto-clean: daily, weekly, monthly, yearly

## Run

```bash
python3 diskman.py                 # opens in your browser
python3 diskman.py --install --lan # background service; open the LAN address once to set a login
```

## Docker / CasaOS

```bash
docker build -t diskman:2.0.0 .
docker compose up -d   # see docker-compose.yml: host / mounted at /host
```

Tests: `python3 test_cache.py && python3 test_multi.py && python3 test_tools.py`
