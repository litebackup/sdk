#!/usr/bin/env bash
# Run as the app's user on a joined machine. This job owns scheduled/* names.
set -euo pipefail

if (( $# == 0 )); then
  echo 'usage: backup-sqlite.sh /absolute/path/database.sqlite [...]' >&2
  exit 2
fi

current_path=''
cleanup() {
  local result=$?
  trap - EXIT
  if [[ -n "$current_path" ]]; then
    if ! litebackup backup stop "$current_path"; then
      echo "Could not stop backup for $current_path; inspect litebackup backup list" >&2
      result=1
    fi
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for path in "$@"; do
  if [[ "$path" != /* || ! -f "$path" ]]; then
    echo "Expected an existing absolute database path: $path" >&2
    exit 2
  fi
  litebackup backup "$path" --prefix scheduled/
  current_path="$path"
  litebackup backup "$path" --prefix scheduled/ --wait cloud --timeout 30m
  litebackup backup stop "$path"
  current_path=''
done
