package handlers

import (
	"os"
	"path/filepath"
	"syscall"

	"github.com/brainite/plugin/internal/config"
)

// acquireFlushLock serialises flushers.
//
// Stop, PreCompact and SessionEnd can each spawn one within milliseconds of each
// other, and two flushers reading the same spool both push before either writes
// its .sent sidecar. The server's UPSERT means no duplicate row is created, but
// the second request is pure waste and it makes "did this flush work?"
// unanswerable from the outside.
//
// Non-blocking: a second flusher exits immediately rather than queueing. There is
// nothing for it to do that the one holding the lock is not already doing, and a
// queued flusher on a hook path is a hang waiting to happen.
func acquireFlushLock() (release func(), ok bool) {
	if err := os.MkdirAll(config.Dir(), 0o700); err != nil {
		// Can't lock, so don't flush: a flush we cannot serialise is the case
		// this function exists to prevent.
		return func() {}, false
	}
	path := filepath.Join(config.Dir(), "flush.lock")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return func() {}, false
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return func() {}, false
	}
	// The lock is held by the fd, so it is released by the kernel even if this
	// process is killed — no stale-lock recovery to get wrong.
	return func() {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		_ = f.Close()
	}, true
}
