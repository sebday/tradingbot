#!/usr/bin/env bash
# Shared helpers for evo.trading bar scripts.

EVO_BAR_CACHE_DIR="${EVO_BAR_CACHE_DIR:-${EVOSHELL_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/evoshell}/trading}"

evo_bar_cache_path() {
  printf '%s/%s.json' "$EVO_BAR_CACHE_DIR" "$1"
}

evo_private_dir() {
  local dir="$1"
  mkdir -p -m 700 "$dir" || return 1
  [[ ! -L "$dir" ]] || return 1
  [[ -d "$dir" ]] || return 1
  [[ "$(stat -c %u "$dir")" == "$(id -u)" ]] || return 1
}

evo_read_bounded() {
  local file="$1" max="${2:-65536}" data
  [[ -e "$file" ]] || return 1
  data=$(/usr/bin/dd if="$file" iflag=nofollow,nonblock,count_bytes,fullblock bs=1 count=$((max + 1)) status=none) || return 1
  [ ${#data} -le "$max" ] || return 1
  printf '%s' "$data"
}

evo_bar_cache_read() {
  local key="$1" ttl="${2:-60}"
  local path now mtime age content
  path="$(evo_bar_cache_path "$key")"
  [[ -e "$path" ]] || return 1
  now=$(date +%s)
  mtime=$(stat -c %Y "$path" 2>/dev/null || echo 0)
  age=$((now - mtime))
  (( age < ttl )) || return 1
  content="$(evo_read_bounded "$path")" || return 1
  [[ -n "${content//[[:space:]]/}" ]] || return 1
  printf '%s' "$content"
}

evo_bar_cache_write() {
  local key="$1" path tmp
  evo_private_dir "$EVO_BAR_CACHE_DIR" || return 1
  path="$(evo_bar_cache_path "$key")"
  umask 077
  tmp="$(/usr/bin/mktemp -p "$EVO_BAR_CACHE_DIR" .cache.XXXXXXXXXX)" || return 1
  cat >"$tmp" || { rm -f -- "$tmp"; return 1; }
  mv -f -T -- "$tmp" "$path"
}
