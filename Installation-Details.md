## Installation

Run this on your Klipper host over SSH:

```
wget -O - https://raw.githubusercontent.com/sakitume/BBox-KTC/main/install.sh | bash
```

This will:

1. Clone this repository to `~/BBox-KTC`.
2. Symlink the BBox Klipper extension Python files into `~/klipper/klippy/extras/`.
3. Symlink the BBox KTC configuration (`.cfg`) files into `~/printer_data/config/bbox_toolchanger/`.
4. Create `~/printer_data/config/bt_config/` and seed it with example config
   from this repo's `examples/` folder — **only for files that don't already
   exist there**, so re-running the installer never overwrites your edits.
   The first time `bt_customize.cfg` is created this way, the installer
   reminds you at the end to go edit it for your hardware (dock positions,
   tool-detect pin, etc.) — it won't repeat that reminder on later runs once
   the file exists, whether or not you've gotten around to editing it yet.
5. Install the web based panel to `~/printer_data/config/bbox-ui/`.
6. Look for a Mainsail Nginx site config and, after showing you the exact
   change and asking for confirmation, add the `/bbox/` location block
   needed to serve the UI (backing up the file first). If it can't find or
   confidently patch that file, it prints the block for you to add by hand.
7. Look for `printer.cfg` and, after showing you the exact change and
   defaulting to **yes** on confirmation (just press Enter), add the
   required `[include ...]` lines (backing up the file first). If it can't find the file, or finds
   it already partially configured in a way it can't safely reconcile, it
   prints the block for you to add by hand instead.
8. Restart Klipper.

It's safe to re-run at any time — from the existing clone
(`cd ~/BBox-KTC && ./install.sh`) or via the one-liner above.
Re-running detects an existing install and offers a small menu instead of
installing again:

- **Update** — Pulls new commits (if any) and re-links/re-patches everything
  above. Anything already configured (Nginx, `moonraker.conf`, `printer.cfg`)
  is left alone.
- **Uninstall** — reverses everything this script added: the `printer.cfg`
  block, the Nginx block, the `moonraker.conf` entry (each confirmed and
  backed up first, same as installing them), the extras/config symlinks, and
  the installed UI. You're asked separately whether to keep `bt_config/`,
  since it may hold your hand-edited customizations. The clone at
  `~/BBox-KTC` itself is left in place — remove it yourself
  (`rm -rf ~/BBox-KTC`) if you don't plan to reinstall.

### Required `printer.cfg` includes

```
#--[ Begin: BBox Toolchanger config files ]-----------------------------------------------
[include bbox_toolchanger/bt_base.cfg]
[include bbox_toolchanger/bt_utils.cfg]

# The bt_config/bt_customize.cfg file should be edited to match your printer's configuration
[include bt_config/bt_customize.cfg]
[bbox_toolchanger]
#--[ End: BBox Toolchanger config files ]-------------------------------------------------
```

The `[include bt_config/bt_customize.cfg]` is what allows you to customize the installation.
Be sure you edit the `bt_config/bt_customize.cfg` file with tool (docking bay locations) and
tool-detect mcu pin. 

### Nginx (if you skipped the automatic step, or aren't using Mainsail's Nginx)

Add this inside the `server { }` block of your Nginx site config, then run
`sudo nginx -t && sudo systemctl reload nginx`:

```nginx
location /bbox/ {
    alias /home/YOUR_USER/printer_data/config/bbox-ui/;
    index index.html;
    try_files $uri $uri/ /bbox/index.html;
}
```

The UI is then available at `http://<your-printer>.local/bbox/`.

## Updating

`install.sh` offers to add the `[update_manager]` entry below to
`moonraker.conf` for you (showing the exact change and backing up the file
first). If you skip that or want to add it by hand:

```
[update_manager BBox-KTC]
type: git_repo
channel: dev
path: ~/BBox-KTC
origin: https://github.com/sakitume/BBox-KTC.git
managed_services: klipper
primary_branch: main
install_script: install.sh
```

This gets you a normal "Update" button in Mainsail. `install_script` tells
Moonraker to automatically re-run `install.sh` right after pulling changes —
so new/updated files (a new extra, a changed UI build, a new example config)
get relinked/reinstalled without you doing anything else. `install.sh`
restarts Klipper at the end of every run, and `managed_services: klipper`
restarts it again afterward — harmless, just a couple of extra seconds of
downtime.

The `[update_manager]` entry only covers updates triggered **through
Moonraker** (Mainsail's Update button or its API) — that's what runs
`install_script` for you automatically. To check for and apply updates
yourself over SSH instead, just re-run the install script from the existing
clone and choose **Update** when prompted:

```
cd ~/BBox-KTC && ./install.sh
```

This pulls any new commits and re-links/re-patches everything, without
needing a manual `git pull` first.

Klipper and Moonraker restarts go through Moonraker's own REST API
(`/machine/services/restart`), the same mechanism Mainsail's own restart
buttons use — no `sudo` needed for either, regardless of how your printer's
`sudo` is configured. The one place this installer does need `sudo` is
writing the Nginx config during the automatic `/bbox/` setup; if that fails
(e.g. `sudo` requires a password on your system), it fails gracefully and
prints the block for you to add by hand instead of aborting the install.

