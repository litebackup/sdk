// Package sqlitedsn finds the file a database/sql SQLite data source name
// opens, so an app can register the same database it opens:
//
//	dsn := "file:data/app.sqlite?_pragma=journal_mode(WAL)"
//	db, err := sql.Open("sqlite", dsn)
//	path, err := sqlitedsn.Modernc(dsn)
//	_, err = client.StartBackup(ctx, path, nil)
//
// It follows each driver's own parsing and SQLite's URI rules, and refuses
// in-memory and temporary databases, which have no file to back up. It imports
// no driver.
package sqlitedsn

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

// ErrNoFile reports a DSN that opens an in-memory or temporary database.
var ErrNoFile = errors.New("the DSN opens an in-memory or temporary database, which has no file to back up; open it from a path")

// Modernc returns the absolute path of the file modernc.org/sqlite (driver
// "sqlite") opens for dsn. Relative paths resolve against the working directory.
func Modernc(dsn string) (string, error) {
	return resolve(dsn)
}

// Mattn returns the absolute path of the file github.com/mattn/go-sqlite3
// (driver "sqlite3") opens for dsn. Relative paths resolve against the
// working directory.
func Mattn(dsn string) (string, error) {
	return resolve(dsn)
}

// resolve applies the parsing both drivers share: a query after a '?' past
// the first character is the driver's own and is cut off, unless the DSN is
// a file: URI, which SQLite parses whole.
func resolve(dsn string) (string, error) {
	if strings.HasPrefix(dsn, "file:") {
		return resolveURI(dsn)
	}
	position := strings.IndexRune(dsn, '?')
	if position >= 1 {
		parameters, err := url.ParseQuery(dsn[position+1:])
		if err != nil {
			return "", fmt.Errorf("parsing the DSN's parameters: %w", err)
		}
		if parameters.Get("vfs") == "memdb" {
			return "", ErrNoFile
		}
		dsn = dsn[:position]
	}
	if dsn == "" || dsn == ":memory:" {
		return "", ErrNoFile
	}
	return absolute(dsn)
}

// resolveURI parses a file: URI as sqlite3ParseUri does: an empty or
// localhost authority, a percent-encoded path up to '?' or '#', and
// key=value parameters up to '#'.
func resolveURI(uri string) (string, error) {
	rest := strings.TrimPrefix(uri, "file:")
	if strings.HasPrefix(rest, "//") {
		authorityEnd := strings.IndexAny(rest[2:], "/?#")
		authority := rest[2:]
		if authorityEnd >= 0 {
			authority = rest[2 : 2+authorityEnd]
		}
		if authority != "" && authority != "localhost" {
			return "", fmt.Errorf("SQLite opens only local files, and %q names host %q", uri, authority)
		}
		rest = rest[2+len(authority):]
	}
	fragment := strings.IndexRune(rest, '#')
	if fragment >= 0 {
		rest = rest[:fragment]
	}
	encodedPath, query, _ := strings.Cut(rest, "?")
	path, err := decode(encodedPath)
	if err != nil {
		return "", fmt.Errorf("decoding the path of %q: %w", uri, err)
	}
	for _, parameter := range strings.Split(query, "&") {
		encodedKey, encodedValue, _ := strings.Cut(parameter, "=")
		key, keyErr := decode(encodedKey)
		value, valueErr := decode(encodedValue)
		if keyErr != nil || valueErr != nil {
			return "", fmt.Errorf("decoding the parameters of %q: %w", uri, errors.Join(keyErr, valueErr))
		}
		if key == "mode" && value == "memory" || key == "vfs" && value == "memdb" {
			return "", ErrNoFile
		}
	}
	if path == "" || path == ":memory:" {
		return "", ErrNoFile
	}
	return absolute(path)
}

// decode undoes SQLite URI percent-encoding; unlike a form, '+' stays '+'.
func decode(text string) (string, error) {
	return url.PathUnescape(text)
}

// absolute resolves path against the working directory and cleans it.
func absolute(path string) (string, error) {
	if filepath.IsAbs(path) {
		return filepath.Clean(path), nil
	}
	directory, err := os.Getwd()
	if err != nil {
		return "", err
	}
	return filepath.Join(directory, path), nil
}
