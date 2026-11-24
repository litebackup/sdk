package litebackup

import (
	"errors"
	"fmt"
	"os"
	"syscall"
)

// privateRuntimeDirectory makes sure the per-user runtime directory used
// without $XDG_RUNTIME_DIR is this user's own, as the litebackup command
// does: the temporary directory is shared, so another local user could
// create it first and answer as the daemon. It creates the directory with
// mode 0700 when it is missing, and refuses one that is a symlink, is not a
// directory, belongs to another user, or is open to anyone else.
func privateRuntimeDirectory(directory string) error {
	err := os.Mkdir(directory, 0o700)
	if err != nil && !errors.Is(err, os.ErrExist) {
		return err
	}
	status, err := os.Lstat(directory)
	if err != nil {
		return err
	}
	ownership, ok := status.Sys().(*syscall.Stat_t)
	if !status.IsDir() || !ok || int(ownership.Uid) != os.Getuid() ||
		status.Mode().Perm()&0o077 != 0 {
		return fmt.Errorf(
			"%s is not a directory of this user's alone; "+
				"remove it or set XDG_RUNTIME_DIR to this user's own directory",
			directory,
		)
	}
	return nil
}
