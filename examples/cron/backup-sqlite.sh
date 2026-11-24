#!/usr/bin/env bash
# Run as the app's user on a joined machine. This job owns scheduled/* names.
set -o errexit
set -o nounset
set -o pipefail

if (( $# == 0 )); then
    echo 'usage: backup-sqlite.sh /absolute/path/database.sqlite [...]' >&2
    exit 2
fi

for path in "${@}"; do
    litebackup backup --timeout 30m -- "${path}" scheduled/
done
