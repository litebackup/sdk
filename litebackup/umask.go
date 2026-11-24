package litebackup

import (
	"bufio"
	"io/fs"
	"os"
	"strconv"
	"strings"
	"sync"
	"syscall"
)

// umaskMutex keeps two calls of this package from setting the umask at once.
var umaskMutex sync.Mutex

// currentUmask returns this process's file mode creation mask, which
// acquire and restore send so a file the daemon creates gets the bits a file
// this process created would. On Linux it reads the Umask line of
// /proc/self/status and leaves the mask alone; where that is missing, as on
// macOS, it sets the mask and puts it back at once, so a file another
// goroutine creates in that moment could get the mask 0.
func currentUmask() fs.FileMode {
	mask, ok := statusUmask("/proc/self/status")
	if ok {
		return mask
	}
	umaskMutex.Lock()
	defer umaskMutex.Unlock()
	previous := syscall.Umask(0)
	syscall.Umask(previous)
	return fs.FileMode(previous)
}

// statusUmask reads the octal Umask line of a /proc status file.
func statusUmask(path string) (fs.FileMode, bool) {
	status, err := os.Open(path)
	if err != nil {
		return 0, false
	}
	defer status.Close()
	lines := bufio.NewScanner(status)
	for lines.Scan() {
		umaskText, found := strings.CutPrefix(lines.Text(), "Umask:")
		if !found {
			continue
		}
		mask, err := strconv.ParseUint(strings.TrimSpace(umaskText), 8, 32)
		if err != nil || mask > 0o777 {
			return 0, false
		}
		return fs.FileMode(mask), true
	}
	return 0, false
}

// requestUmask is the umask a request sends: the one given, checked, or
// this process's.
func requestUmask(umask *fs.FileMode) (*int32, error) {
	if umask == nil {
		wireUmask := int32(currentUmask())
		return &wireUmask, nil
	}
	mask := *umask
	if mask > 0o777 {
		return nil, invalidArgument("umask must be between 0 and 0777")
	}
	wireUmask := int32(mask)
	return &wireUmask, nil
}
