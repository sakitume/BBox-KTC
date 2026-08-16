#!/bin/bash
#
# BBox Klipper Toolchanger installer.
#
# Usage (fresh install, run on the Klipper host over SSH):
#   wget -O - https://raw.githubusercontent.com/sakitume/BBox-KTC/main/install.sh | bash
#
# This clones the repo to ~/BBox-KTC, links the Klipper
# extras and config files into place, seeds a ~/printer_data/config/bt_config
# folder with example config (without ever overwriting a file you already
# have there), installs the UI, and optionally wires up the Nginx /bbox/
# location block, a moonraker.conf [update_manager] entry, and the required
# printer.cfg includes (each shown to you and confirmed before any edit,
# with a backup taken first).
#
# Safe to re-run: re-running from an existing clone detects the install and
# offers a small Update/Uninstall menu instead of installing again. Update
# pulls new commits (if any) and re-links/re-patches; Uninstall reverses
# everything this script added, with the same confirm+backup pattern as
# install, and asks whether to keep bt_config/ (it may hold your
# customizations).
#
# The paths below can be overridden via environment variables, e.g. for
# testing against a scratch directory instead of the real config.

set -eu
export LC_ALL=C

# --- Color support ---------------------------------------------------------
# Off by default whenever stdout isn't a real terminal - piped through
# `wget | bash`, redirected to a log file, or run unattended by Moonraker's
# install_script - or when NO_COLOR is set (https://no-color.org/), so
# escape codes never leak into logs or non-interactive output.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && command -v tput > /dev/null 2>&1 \
   && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
    C_RESET="$(tput sgr0)"
    C_BOLD="$(tput bold)"
    C_CYAN="$(tput setaf 6)"
    C_YELLOW="$(tput setaf 3)"
    C_GREEN="$(tput setaf 2)"
    C_RED="$(tput setaf 1)"
else
    C_RESET="" C_BOLD="" C_CYAN="" C_YELLOW="" C_GREEN="" C_RED=""
fi

function stage_header {
    # $1 = stage title. Marks the start of a new phase of the install/update/
    # uninstall so scrolled-past terminal output is easier to scan.
    echo ""
    echo "${C_CYAN}${C_BOLD}=== $1 ===${C_RESET}"
}

KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"
CONFIG_PATH="${CONFIG_PATH:-${HOME}/printer_data/config}"
INSTALL_PATH="${INSTALL_PATH:-${HOME}/BBox-KTC}"
REPO_URL="${REPO_URL:-https://github.com/sakitume/BBox-KTC.git}"

EXTRAS_PATH="${KLIPPER_PATH}/klippy/extras"
BT_SOFTWARE_DIR="${CONFIG_PATH}/bbox_toolchanger"
BT_USER_CONFIG_DIR="${CONFIG_PATH}/bt_config"
UI_DEST="${CONFIG_PATH}/bbox-ui"
NGINX_CONF="/etc/nginx/sites-enabled/mainsail"
MOONRAKER_CONF="${CONFIG_PATH}/moonraker.conf"

PRINTER_CFG_BEGIN="#--[ Begin: BBox Toolchanger config files ]"
PRINTER_CFG_END="#--[ End: BBox Toolchanger config files ]"
# Matches Klipper's autosave marker line without pinning to its exact
# dash/spacing formatting - just the leading "#*#" sigil (unique to
# Klipper's auto-generated block) with SAVE_CONFIG somewhere after it.
SAVE_CONFIG_MARKER_PATTERN='^#\*#.*SAVE_CONFIG'
NGINX_BLOCK_BEGIN="# BEGIN bbox_toolchanger"
NGINX_BLOCK_END="# END bbox_toolchanger"

# --- Small shared helper -----------------------------------------------

function ask_yes_no {
    # $1 = prompt text (include your own "[y/N] "/"[Y/n] " suffix)
    # $2 = default reply ("y" or "n") used ONLY when a real tty is present
    #      and the user just hits Enter. When no tty is available at all
    #      (e.g. a non-interactive Moonraker-triggered update running
    #      install_script unattended), we always answer "no" - safe/no-op -
    #      regardless of the requested default, since nobody was actually
    #      there to agree to a file edit.
    local prompt="$1" default="${2:-n}" reply=""
    echo -n "$prompt"
    if ! read -r reply < /dev/tty 2>/dev/null; then
        echo "(no tty available, assuming no)"
        return 1
    fi
    [ -z "$reply" ] && reply="$default"
    case "$reply" in
        [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

function insert_before_marker_or_append {
    # Reads a block from stdin and writes it into $1: just above the first
    # line matching the extended-regex pattern $2 if found, otherwise
    # appended at the end of the file. Used to keep our printer.cfg block
    # above Klipper's auto-generated SAVE_CONFIG region, which Klipper
    # truncates and regenerates from that marker onward on every
    # SAVE_CONFIG - anything appended below it would be silently lost on
    # the next save.
    local file="$1" marker="$2" tmpfile line_num
    local block
    block="$(cat)"
    if grep -qE "$marker" "$file" 2>/dev/null; then
        line_num="$(grep -nE "$marker" "$file" | head -n 1 | cut -d: -f1)"
        tmpfile="$(mktemp)"
        head -n "$((line_num - 1))" "$file" > "$tmpfile"
        {
            echo ""
            echo "$block"
            echo ""
        } >> "$tmpfile"
        tail -n "+${line_num}" "$file" >> "$tmpfile"
        cp "$tmpfile" "$file"
        rm -f "$tmpfile"
    else
        {
            echo ""
            echo "$block"
        } >> "$file"
    fi
}

function strip_marked_block {
    # Prints $1 with all lines between (and including) the $2/$3 markers
    # removed.
    local file="$1" begin="$2" end="$3"
    awk -v begin="$begin" -v end="$end" '
        index($0, begin) { skip=1 }
        skip==0 { print }
        index($0, end) { skip=0 }
    ' "$file"
}

function preflight_checks {
    echo "[PRE-CHECK] Checking environment..."

    if [ "$(id -u)" -eq 0 ]; then
        echo "${C_RED}[ERROR]${C_RESET} Do not run this script as root."
        exit 1
    fi

    if ! systemctl list-units --full -all -t service --no-legend 2>/dev/null | grep -qF 'klipper.service'; then
        echo "${C_RED}[ERROR]${C_RESET} klipper.service not found. Install Klipper first."
        exit 1
    fi

    if [ ! -d "$EXTRAS_PATH" ]; then
        echo "${C_RED}[ERROR]${C_RESET} Klipper extras directory not found at $EXTRAS_PATH"
        exit 1
    fi

    if [ ! -d "$CONFIG_PATH" ]; then
        echo "${C_RED}[ERROR]${C_RESET} $CONFIG_PATH not found. Expected a standard printer_data layout."
        exit 1
    fi

    echo "[PRE-CHECK] OK"
}

function check_download {
    if [ -d "$INSTALL_PATH" ]; then
        if [ ! -d "$INSTALL_PATH/.git" ]; then
            echo "${C_RED}[ERROR]${C_RESET} $INSTALL_PATH already exists but is not a git repository."
            echo "${C_RED}[ERROR]${C_RESET} Move it aside or remove it, then re-run this script."
            exit 1
        fi
        echo "[DOWNLOAD] $INSTALL_PATH already exists, skipping clone."
    else
        echo "[DOWNLOAD] Cloning $REPO_URL to $INSTALL_PATH..."
        git clone "$REPO_URL" "$INSTALL_PATH"
    fi
}

function show_version_info {
    local local_sha remote_sha
    local_sha="$(git -C "$INSTALL_PATH" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    echo "[VERSION] Installed: $local_sha"
    # GIT_TERMINAL_PROMPT=0 makes git fail fast instead of blocking on a
    # credential prompt (e.g. HTTPS against a private repo with no cached
    # credentials) - this is just an informational version check, not worth
    # ever hanging the installer over. timeout is a second layer of defense
    # in case something still tries to reach out over the network.
    remote_sha="$(GIT_TERMINAL_PROMPT=0 timeout 5 git ls-remote "$REPO_URL" main 2>/dev/null | awk '{print $1}' | cut -c1-7)" || true
    if [ -n "$remote_sha" ] && [ "$remote_sha" != "$local_sha" ]; then
        echo "[VERSION] Update available: $remote_sha (choose Update below, or: cd $INSTALL_PATH && git pull)"
    fi
}

function detect_install_state {
    INSTALLED=false

    local f target
    for f in "$INSTALL_PATH"/bbox_toolchanger/*.py; do
        [ -e "$f" ] || continue
        target="$EXTRAS_PATH/$(basename "$f")"
        if [ -L "$target" ]; then
            INSTALLED=true
            return
        fi
    done

    if [ -d "$BT_SOFTWARE_DIR" ]; then
        for f in "$BT_SOFTWARE_DIR"/*.cfg; do
            [ -e "$f" ] || continue
            if [ -L "$f" ]; then
                INSTALLED=true
                return
            fi
        done
    fi

    local printer_cfg="$CONFIG_PATH/printer.cfg"
    if [ -f "$printer_cfg" ]; then
        if grep -qF "$PRINTER_CFG_BEGIN" "$printer_cfg" 2>/dev/null; then
            INSTALLED=true
            return
        fi
        if grep -qE '^\s*\[include\s+bbox_toolchanger/bt_base\.cfg\]' "$printer_cfg" 2>/dev/null \
           && grep -qE '^\s*\[include\s+bbox_toolchanger/bt_utils\.cfg\]' "$printer_cfg" 2>/dev/null \
           && grep -qE '^\s*\[include\s+bt_config/bt_customize\.cfg\]' "$printer_cfg" 2>/dev/null \
           && grep -qE '^\[bbox_toolchanger\]' "$printer_cfg" 2>/dev/null; then
            INSTALLED=true
            return
        fi
    fi
}

function link_extras {
    echo "[INSTALL] Linking Klipper extras..."
    for f in "$INSTALL_PATH"/bbox_toolchanger/*.py; do
        [ -e "$f" ] || continue
        ln -sfn "$f" "$EXTRAS_PATH/$(basename "$f")"
    done
    # Force Klipper to re-import fresh source instead of trusting a stale .pyc.
    rm -f "$EXTRAS_PATH"/__pycache__/bbox*.pyc
    echo "[INSTALL] Linked:"
    ls -l "$EXTRAS_PATH" | grep bbox || true
}

function link_config_cfgs {
    echo "[INSTALL] Linking BBox config files into $BT_SOFTWARE_DIR..."
    mkdir -p "$BT_SOFTWARE_DIR"
    for f in "$INSTALL_PATH"/bbox_toolchanger/*.cfg; do
        [ -e "$f" ] || continue
        ln -sfn "$f" "$BT_SOFTWARE_DIR/$(basename "$f")"
    done
}

function install_examples {
    BT_CUSTOMIZE_JUST_CREATED=false
    local examples_dir="$INSTALL_PATH/examples"
    if [ ! -d "$examples_dir" ]; then
        return
    fi
    echo "[INSTALL] Setting up $BT_USER_CONFIG_DIR..."
    mkdir -p "$BT_USER_CONFIG_DIR"
    for src in "$examples_dir"/*; do
        [ -e "$src" ] || continue
        local name dest
        name="$(basename "$src")"
        dest="$BT_USER_CONFIG_DIR/$name"
        if [ -e "$dest" ]; then
            echo "[INSTALL] $dest already exists, leaving it alone."
        else
            cp "$src" "$dest"
            echo "[INSTALL] Created $dest - edit it for your printer."
            [ "$name" = "bt_customize.cfg" ] && BT_CUSTOMIZE_JUST_CREATED=true
        fi
    done
}

function remind_bt_customize_if_needed {
    # Only nag the first time bt_customize.cfg is created fresh from the
    # template - if it already existed (whether or not the user has gotten
    # around to editing it yet), install_examples left it alone and we have
    # nothing new to tell them.
    if [ "${BT_CUSTOMIZE_JUST_CREATED:-false}" = true ]; then
        echo ""
        echo "${C_YELLOW}${C_BOLD}=================================================${C_RESET}"
        echo "${C_YELLOW}${C_BOLD}[NEXT STEP] $BT_USER_CONFIG_DIR/bt_customize.cfg was just created from the${C_RESET}"
        echo "${C_YELLOW}${C_BOLD}[NEXT STEP] default template - edit it to match your printer's hardware (dock${C_RESET}"
        echo "${C_YELLOW}${C_BOLD}[NEXT STEP] positions, tool-detect pin, etc.) before using the toolchanger.${C_RESET}"
        echo "${C_YELLOW}${C_BOLD}=================================================${C_RESET}"
    fi
}

function install_ui {
    local ui_src="$INSTALL_PATH/bbox-ui-dist"
    if [ ! -d "$ui_src" ]; then
        echo "[INSTALL] No bbox-ui-dist found, skipping UI install."
        return
    fi
    echo "[INSTALL] Installing UI to $UI_DEST..."
    mkdir -p "$UI_DEST"
    cp -r "$ui_src"/. "$UI_DEST"/
}

function nginx_block {
    cat <<EOF
    ${NGINX_BLOCK_BEGIN}
    location = /bbox {
        return 301 /bbox/;
    }
    location /bbox/ {
        alias ${UI_DEST}/;
        index index.html;
        try_files \$uri \$uri/ /bbox/index.html;
    }
    ${NGINX_BLOCK_END}
EOF
}

function print_nginx_instructions {
    echo "[NGINX] Add the following inside the 'server { }' block of your Mainsail"
    echo "[NGINX] Nginx site config (commonly $NGINX_CONF), then run:"
    echo "[NGINX]   sudo nginx -t && sudo systemctl reload nginx"
    echo ""
    nginx_block
    echo ""
}

function offer_nginx_patch {
    if [ ! -f "$NGINX_CONF" ]; then
        echo "[NGINX] $NGINX_CONF not found."
        print_nginx_instructions
        return
    fi

    if grep -qF "$NGINX_BLOCK_BEGIN" "$NGINX_CONF" 2>/dev/null; then
        echo "[NGINX] $NGINX_CONF already has the BBox Toolchanger block, skipping."
        return
    fi

    if grep -q "location /bbox/" "$NGINX_CONF" 2>/dev/null; then
        echo "[NGINX] $NGINX_CONF already has a /bbox/ location block (pre-existing/unmarked), skipping."
        return
    fi

    echo "[NGINX] Found $NGINX_CONF without a /bbox/ location block."
    echo "[NGINX] This block would be added just before the file's final closing brace:"
    nginx_block

    local last_line
    last_line="$(tail -n 1 "$NGINX_CONF" | tr -d '[:space:]')"
    if [ "$last_line" != "}" ]; then
        echo "[NGINX] Could not confidently locate the end of the server block (last line isn't a lone '}')."
        echo "[NGINX] Skipping automatic edit."
        print_nginx_instructions
        return
    fi

    if ask_yes_no "[NGINX] Add it now (backs up the file first)? [y/N] " "n"; then
        local backup tmpfile
        backup="${NGINX_CONF}.bak-$(date +%Y%m%d-%H%M%S)"
        cp "$NGINX_CONF" "$backup"
        echo "[NGINX] Backed up to $backup"

        tmpfile="$(mktemp)"
        head -n -1 "$NGINX_CONF" > "$tmpfile"
        nginx_block >> "$tmpfile"
        echo "}" >> "$tmpfile"

        if ! sudo cp "$tmpfile" "$NGINX_CONF"; then
            echo "${C_RED}[ERROR]${C_RESET} Could not write $NGINX_CONF (sudo failed - a password may be required, which won't work non-interactively)."
            rm -f "$tmpfile"
            print_nginx_instructions
            return
        fi
        rm -f "$tmpfile"

        if sudo nginx -t; then
            if sudo systemctl reload nginx; then
                echo "[NGINX] Reloaded."
            else
                echo "${C_RED}[ERROR]${C_RESET} Could not reload nginx (sudo failed). Restoring backup."
                sudo cp "$backup" "$NGINX_CONF" || cp "$backup" "$NGINX_CONF"
            fi
        else
            echo "${C_RED}[ERROR]${C_RESET} nginx -t failed, restoring backup."
            sudo cp "$backup" "$NGINX_CONF" || cp "$backup" "$NGINX_CONF"
        fi
    else
        echo "[NGINX] Skipped."
        print_nginx_instructions
    fi
}

function offer_remove_nginx_block {
    if [ ! -f "$NGINX_CONF" ] || ! grep -qF "$NGINX_BLOCK_BEGIN" "$NGINX_CONF" 2>/dev/null; then
        echo "[NGINX] No BBox Toolchanger block (added by this installer) found in $NGINX_CONF, nothing to remove."
        return
    fi

    echo "[NGINX] Found the BBox Toolchanger block in $NGINX_CONF."
    if ! ask_yes_no "[NGINX] Remove it now (backs up the file first)? [y/N] " "n"; then
        echo "[NGINX] Skipped."
        return
    fi

    local backup tmpfile
    backup="${NGINX_CONF}.bak-$(date +%Y%m%d-%H%M%S)"
    cp "$NGINX_CONF" "$backup"
    echo "[NGINX] Backed up to $backup"

    tmpfile="$(mktemp)"
    strip_marked_block "$NGINX_CONF" "$NGINX_BLOCK_BEGIN" "$NGINX_BLOCK_END" > "$tmpfile"

    if ! sudo cp "$tmpfile" "$NGINX_CONF"; then
        echo "${C_RED}[ERROR]${C_RESET} Could not write $NGINX_CONF (sudo failed)."
        rm -f "$tmpfile"
        return
    fi
    rm -f "$tmpfile"

    if sudo nginx -t; then
        if sudo systemctl reload nginx; then
            echo "[NGINX] Removed and reloaded."
        else
            echo "${C_RED}[ERROR]${C_RESET} Could not reload nginx (sudo failed). Restoring backup."
            sudo cp "$backup" "$NGINX_CONF" || cp "$backup" "$NGINX_CONF"
        fi
    else
        echo "${C_RED}[ERROR]${C_RESET} nginx -t failed, restoring backup."
        sudo cp "$backup" "$NGINX_CONF" || cp "$backup" "$NGINX_CONF"
    fi
}

function moonraker_block {
    cat <<EOF
[update_manager BBox-KTC]
type: git_repo
channel: dev
path: ${INSTALL_PATH}
origin: ${REPO_URL}
managed_services: klipper
primary_branch: main
install_script: install.sh
EOF
}

function print_moonraker_instructions {
    echo "[MOONRAKER] Add the following to moonraker.conf, then restart Moonraker"
    echo "[MOONRAKER] (sudo systemctl restart moonraker):"
    echo ""
    moonraker_block
    echo ""
}

function offer_moonraker_patch {
    if [ ! -f "$MOONRAKER_CONF" ]; then
        echo "[MOONRAKER] $MOONRAKER_CONF not found."
        print_moonraker_instructions
        return
    fi

    if grep -q "^\[update_manager BBox-KTC\]" "$MOONRAKER_CONF" 2>/dev/null; then
        echo "[MOONRAKER] $MOONRAKER_CONF already configured for BBox-KTC, skipping."
        return
    fi

    echo "[MOONRAKER] Found $MOONRAKER_CONF without a BBox-KTC update_manager entry."
    echo "[MOONRAKER] This section would be appended to the end of the file:"
    echo ""
    moonraker_block
    echo ""

    if ask_yes_no "[MOONRAKER] Add it now (backs up the file first)? [y/N] " "n"; then
        local backup
        backup="${MOONRAKER_CONF}.bak-$(date +%Y%m%d-%H%M%S)"
        cp "$MOONRAKER_CONF" "$backup"
        echo "[MOONRAKER] Backed up to $backup"

        {
            echo ""
            moonraker_block
        } >> "$MOONRAKER_CONF"

        echo "[MOONRAKER] Restarting Moonraker to apply..."
        curl -sf -X POST 'http://localhost:7125/machine/services/restart?service=moonraker' > /dev/null || true
        sleep 3
        if curl -sf http://localhost:7125/server/info > /dev/null 2>&1; then
            echo "[MOONRAKER] Added and restarted successfully."
        else
            echo "${C_RED}[ERROR]${C_RESET} Moonraker did not come back up, restoring backup."
            cp "$backup" "$MOONRAKER_CONF"
            curl -sf -X POST 'http://localhost:7125/machine/services/restart?service=moonraker' > /dev/null || true
            echo "${C_RED}[ERROR]${C_RESET} If Moonraker still isn't responding, SSH in and run: sudo systemctl restart moonraker"
        fi
    else
        echo "[MOONRAKER] Skipped."
        print_moonraker_instructions
    fi
}

function offer_remove_moonraker_entry {
    if [ ! -f "$MOONRAKER_CONF" ] || ! grep -q "^\[update_manager BBox-KTC\]" "$MOONRAKER_CONF" 2>/dev/null; then
        echo "[MOONRAKER] No BBox-KTC update_manager entry found in $MOONRAKER_CONF, nothing to remove."
        return
    fi

    echo "[MOONRAKER] Found the BBox-KTC update_manager entry in $MOONRAKER_CONF."
    if ! ask_yes_no "[MOONRAKER] Remove it now (backs up the file first)? [y/N] " "n"; then
        echo "[MOONRAKER] Skipped."
        return
    fi

    local backup tmpfile
    backup="${MOONRAKER_CONF}.bak-$(date +%Y%m%d-%H%M%S)"
    cp "$MOONRAKER_CONF" "$backup"
    echo "[MOONRAKER] Backed up to $backup"

    tmpfile="$(mktemp)"
    awk '
        /^\[update_manager BBox-KTC\]/ { skip=1; next }
        skip==1 && /^\[/ { skip=0 }
        skip!=1 { print }
    ' "$MOONRAKER_CONF" > "$tmpfile"
    cp "$tmpfile" "$MOONRAKER_CONF"
    rm -f "$tmpfile"

    echo "[MOONRAKER] Removed. Restarting Moonraker to apply..."
    curl -sf -X POST 'http://localhost:7125/machine/services/restart?service=moonraker' > /dev/null || true
    sleep 3
    if curl -sf http://localhost:7125/server/info > /dev/null 2>&1; then
        echo "[MOONRAKER] Removed and restarted successfully."
    else
        echo "${C_RED}[ERROR]${C_RESET} Moonraker did not come back up, restoring backup."
        cp "$backup" "$MOONRAKER_CONF"
        curl -sf -X POST 'http://localhost:7125/machine/services/restart?service=moonraker' > /dev/null || true
        echo "${C_RED}[ERROR]${C_RESET} If Moonraker still isn't responding, SSH in and run: sudo systemctl restart moonraker"
    fi
}

function printer_cfg_block {
    cat <<'EOF'
#--[ Begin: BBox Toolchanger config files ]-----------------------------------------------
[include bbox_toolchanger/bt_base.cfg]
[include bbox_toolchanger/bt_utils.cfg]

# The bt_config/bt_customize.cfg file should be edited to match your printer's configuration
[include bt_config/bt_customize.cfg]
[bbox_toolchanger]
#--[ End: BBox Toolchanger config files ]-------------------------------------------------
EOF
}

function print_printer_cfg_instructions {
    echo "[PRINTER.CFG] Add the following to your printer.cfg:"
    echo ""
    printer_cfg_block
    echo ""
}

function offer_printer_cfg_patch {
    local printer_cfg="$CONFIG_PATH/printer.cfg"

    if [ ! -f "$printer_cfg" ]; then
        echo "[PRINTER.CFG] $printer_cfg not found."
        print_printer_cfg_instructions
        return
    fi

    if grep -qF "$PRINTER_CFG_BEGIN" "$printer_cfg" 2>/dev/null; then
        echo "[PRINTER.CFG] $printer_cfg already has the BBox Toolchanger block, skipping."
        return
    fi

    local have_base=0 have_utils=0 have_customize=0 have_section=0
    grep -qE '^\s*\[include\s+bbox_toolchanger/bt_base\.cfg\]' "$printer_cfg" 2>/dev/null && have_base=1
    grep -qE '^\s*\[include\s+bbox_toolchanger/bt_utils\.cfg\]' "$printer_cfg" 2>/dev/null && have_utils=1
    grep -qE '^\s*\[include\s+bt_config/bt_customize\.cfg\]' "$printer_cfg" 2>/dev/null && have_customize=1
    grep -qE '^\[bbox_toolchanger\]' "$printer_cfg" 2>/dev/null && have_section=1
    local have_count=$((have_base + have_utils + have_customize + have_section))

    if [ "$have_count" -eq 4 ]; then
        echo "[PRINTER.CFG] $printer_cfg already has all required lines (pre-existing, unmarked), skipping."
        return
    fi

    if [ "$have_count" -gt 0 ]; then
        echo "[PRINTER.CFG] $printer_cfg has some but not all required lines - leaving it alone to avoid duplicating anything."
        echo "[PRINTER.CFG] Missing:"
        [ "$have_base" -eq 0 ] && echo "    [include bbox_toolchanger/bt_base.cfg]"
        [ "$have_utils" -eq 0 ] && echo "    [include bbox_toolchanger/bt_utils.cfg]"
        [ "$have_customize" -eq 0 ] && echo "    [include bt_config/bt_customize.cfg]"
        [ "$have_section" -eq 0 ] && echo "    [bbox_toolchanger]"
        echo ""
        return
    fi

    local has_save_config=false
    grep -qE "$SAVE_CONFIG_MARKER_PATTERN" "$printer_cfg" 2>/dev/null && has_save_config=true

    echo "[PRINTER.CFG] $printer_cfg is missing the BBox Toolchanger config block."
    if [ "$has_save_config" = true ]; then
        echo "[PRINTER.CFG] This block would be inserted just above the existing SAVE_CONFIG block"
        echo "[PRINTER.CFG] (Klipper requires that block stay at the very end of the file):"
    else
        echo "[PRINTER.CFG] This block would be appended to the end of the file:"
    fi
    echo ""
    printer_cfg_block
    echo ""

    if ask_yes_no "[PRINTER.CFG] Add it now (backs up the file first)? [Y/n] " "y"; then
        local backup
        backup="${printer_cfg}.bak-$(date +%Y%m%d-%H%M%S)"
        cp "$printer_cfg" "$backup"
        echo "[PRINTER.CFG] Backed up to $backup"
        printer_cfg_block | insert_before_marker_or_append "$printer_cfg" "$SAVE_CONFIG_MARKER_PATTERN"
        if [ "$has_save_config" = true ]; then
            echo "[PRINTER.CFG] Added (placed above the existing SAVE_CONFIG block)."
        else
            echo "[PRINTER.CFG] Added."
        fi
    else
        echo "[PRINTER.CFG] Skipped."
        print_printer_cfg_instructions
    fi
}

function offer_remove_printer_cfg_block {
    local printer_cfg="$CONFIG_PATH/printer.cfg"

    if [ ! -f "$printer_cfg" ] || ! grep -qF "$PRINTER_CFG_BEGIN" "$printer_cfg" 2>/dev/null; then
        echo "[PRINTER.CFG] No BBox Toolchanger block (added by this installer) found in $printer_cfg, nothing to remove."
        return
    fi

    echo "[PRINTER.CFG] Found the BBox Toolchanger block in $printer_cfg."
    if ! ask_yes_no "[PRINTER.CFG] Remove it now (backs up the file first)? [y/N] " "n"; then
        echo "[PRINTER.CFG] Skipped."
        return
    fi

    local backup tmpfile
    backup="${printer_cfg}.bak-$(date +%Y%m%d-%H%M%S)"
    cp "$printer_cfg" "$backup"
    echo "[PRINTER.CFG] Backed up to $backup"

    tmpfile="$(mktemp)"
    strip_marked_block "$printer_cfg" "$PRINTER_CFG_BEGIN" "$PRINTER_CFG_END" > "$tmpfile"
    cp "$tmpfile" "$printer_cfg"
    rm -f "$tmpfile"
    echo "[PRINTER.CFG] Removed."
}

function restart_klipper {
    echo "[POST-INSTALL] Restarting Klipper via Moonraker..."
    if ! curl -sf -X POST 'http://localhost:7125/machine/services/restart?service=klipper' > /dev/null; then
        echo "${C_RED}[ERROR]${C_RESET} Could not reach Moonraker to restart Klipper."
        echo "${C_RED}[ERROR]${C_RESET} Restart it yourself, e.g.: sudo systemctl restart klipper"
    fi
}

function do_install {
    stage_header "Linking software"
    link_extras
    link_config_cfgs

    stage_header "Config templates"
    install_examples

    stage_header "Web UI"
    install_ui

    stage_header "Nginx"
    offer_nginx_patch

    stage_header "Moonraker"
    offer_moonraker_patch

    stage_header "printer.cfg"
    offer_printer_cfg_patch

    stage_header "Restart"
    restart_klipper

    echo ""
    echo "${C_GREEN}[DONE]${C_RESET} Installed to $INSTALL_PATH"
    echo "${C_GREEN}[DONE]${C_RESET} See $INSTALL_PATH/README.md for update instructions."
    remind_bt_customize_if_needed
}

function do_update {
    stage_header "Checking for updates"
    echo "[UPDATE] Checking for updates..."
    local before after
    before="$(git -C "$INSTALL_PATH" rev-parse HEAD)"
    if ! git -C "$INSTALL_PATH" pull --ff-only; then
        echo "${C_RED}[ERROR]${C_RESET} git pull failed. Resolve manually in $INSTALL_PATH."
        exit 1
    fi
    after="$(git -C "$INSTALL_PATH" rev-parse HEAD)"
    if [ "$before" = "$after" ]; then
        echo "[UPDATE] Already up to date."
    else
        echo "[UPDATE] Updated $before -> $after"
    fi

    stage_header "Linking software"
    link_extras
    link_config_cfgs

    stage_header "Config templates"
    install_examples

    stage_header "Web UI"
    install_ui

    stage_header "Nginx"
    offer_nginx_patch

    stage_header "Moonraker"
    offer_moonraker_patch

    stage_header "printer.cfg"
    offer_printer_cfg_patch

    stage_header "Restart"
    restart_klipper

    echo ""
    echo "${C_GREEN}[DONE]${C_RESET} Update complete."
    remind_bt_customize_if_needed
}

function do_uninstall {
    echo ""
    echo "[UNINSTALL] This will remove BBox Toolchanger from this printer."
    if ! ask_yes_no "[UNINSTALL] Continue? [y/N] " "n"; then
        echo "[UNINSTALL] Aborted."
        return
    fi

    stage_header "printer.cfg"
    offer_remove_printer_cfg_block

    stage_header "Nginx"
    offer_remove_nginx_block

    stage_header "Moonraker"
    offer_remove_moonraker_entry

    stage_header "Removing symlinks & UI"
    echo "[UNINSTALL] Removing extras symlinks..."
    local f target found=0
    for f in "$INSTALL_PATH"/bbox_toolchanger/*.py; do
        [ -e "$f" ] || continue
        target="$EXTRAS_PATH/$(basename "$f")"
        if [ -L "$target" ] && [ "$(readlink -f "$target")" = "$(readlink -f "$f")" ]; then
            rm -f "$target"
            found=1
        fi
    done
    rm -f "$EXTRAS_PATH"/__pycache__/bbox*.pyc
    if [ "$found" -eq 1 ]; then
        echo "[UNINSTALL] Removed extras symlinks."
    else
        echo "[UNINSTALL] No matching extras symlinks found."
    fi

    echo "[UNINSTALL] Removing config symlinks..."
    if [ -d "$BT_SOFTWARE_DIR" ]; then
        for f in "$INSTALL_PATH"/bbox_toolchanger/*.cfg; do
            [ -e "$f" ] || continue
            target="$BT_SOFTWARE_DIR/$(basename "$f")"
            if [ -L "$target" ] && [ "$(readlink -f "$target")" = "$(readlink -f "$f")" ]; then
                rm -f "$target"
            fi
        done
        rmdir "$BT_SOFTWARE_DIR" 2>/dev/null || true
    fi

    if [ -d "$UI_DEST" ]; then
        rm -rf "$UI_DEST"
        echo "[UNINSTALL] Removed $UI_DEST"
    fi

    if [ -d "$BT_USER_CONFIG_DIR" ]; then
        if ask_yes_no "[UNINSTALL] Remove $BT_USER_CONFIG_DIR too (this may contain your customizations)? [y/N] " "n"; then
            rm -rf "$BT_USER_CONFIG_DIR"
            echo "[UNINSTALL] Removed $BT_USER_CONFIG_DIR"
        else
            echo "[UNINSTALL] Keeping $BT_USER_CONFIG_DIR"
        fi
    fi

    stage_header "Restart"
    restart_klipper

    echo ""
    echo "${C_GREEN}[DONE]${C_RESET} Uninstall complete."
    if [ -d "$BT_USER_CONFIG_DIR" ]; then
        echo "${C_GREEN}[DONE]${C_RESET} Kept: $BT_USER_CONFIG_DIR"
    fi
    echo "${C_GREEN}[DONE]${C_RESET} The cloned repo at $INSTALL_PATH was left in place."
    echo "${C_GREEN}[DONE]${C_RESET} Remove it yourself if you don't plan to reinstall: rm -rf $INSTALL_PATH"
}

function prompt_update_or_uninstall {
    echo ""
    echo "[MENU] An existing BBox Toolchanger install was detected."
    echo "[MENU]   1) Update"
    echo "[MENU]   2) Uninstall"
    echo -n "[MENU] Choose [1/2, default 1]: "
    local choice=""
    read -r choice < /dev/tty 2>/dev/null || true
    case "$choice" in
        2) do_uninstall ;;
        *) do_update ;;
    esac
}

printf "\n${C_CYAN}${C_BOLD}=================================================${C_RESET}\n"
echo "${C_CYAN}${C_BOLD}- BBox Klipper Toolchanger install script -${C_RESET}"
printf "${C_CYAN}${C_BOLD}=================================================${C_RESET}\n\n"

stage_header "Preflight checks"
preflight_checks

stage_header "Download & version"
check_download
show_version_info
detect_install_state

if [ "$INSTALLED" = true ]; then
    prompt_update_or_uninstall
else
    do_install
fi
