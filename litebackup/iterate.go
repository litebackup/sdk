package litebackup

import (
	"context"
	"iter"
	"time"
)

// AllDatabases iterates every database on this machine, a page at a time.
// It ends after yielding an error.
func (c *Client) AllDatabases(ctx context.Context) iter.Seq2[DatabaseStatus, error] {
	return func(yield func(DatabaseStatus, error) bool) {
		after := ""
		for {
			page, err := c.DatabasesPage(ctx, after, 100)
			if err != nil {
				yield(DatabaseStatus{}, err)
				return
			}
			for _, database := range page.Databases {
				if !yield(database, nil) {
					return
				}
			}
			if page.Next == "" {
				return
			}
			after = page.Next
		}
	}
}

// AllVersions iterates the versions of a database the server can restore,
// oldest first, after args.After and at most args.Limit when positive. It
// ends after yielding an error.
func (c *Client) AllVersions(ctx context.Context, name string, args *VersionsArgs) iter.Seq2[RestorableVersion, error] {
	if args == nil {
		args = &VersionsArgs{}
	}
	after, limit := args.After, args.Limit
	return func(yield func(RestorableVersion, error) bool) {
		cursor := after
		yielded := 0
		for {
			pageArgs := &VersionsArgs{After: cursor}
			if limit > 0 {
				pageArgs.Limit = limit - yielded
			}
			page, err := c.VersionsPage(ctx, name, pageArgs)
			if err != nil {
				yield(RestorableVersion{}, err)
				return
			}
			for _, version := range page.Versions {
				if !yield(version, nil) {
					return
				}
				yielded++
				if limit > 0 && yielded >= limit {
					return
				}
			}
			if page.Next == 0 || page.Next <= cursor || len(page.Versions) == 0 {
				return
			}
			cursor = page.Next
		}
	}
}

// WatchEventsArgs selects where a watch starts and how it long-polls.
type WatchEventsArgs struct {
	// After starts with events after this ID; -1 starts at the end of the
	// log, for events from now on; 0 starts at the oldest.
	After int64
	// Wait is how long each long-poll waits for a new event; 30s when 0.
	Wait time.Duration
	// OnError is called with each failed long-poll before it is retried.
	OnError func(err error)
}

const (
	// firstRetryDelay and maximumRetryDelay bound the backoff between failed long-polls.
	firstRetryDelay   = 250 * time.Millisecond
	maximumRetryDelay = 10 * time.Second
	// eventWait is how long one events long-poll waits by default.
	eventWait = 30 * time.Second
)

// WatchEvents yields events, oldest first, by long-polling the daemon until
// the caller stops or ctx ends. A failed long-poll is retried with backoff
// from the same cursor; cancellation, a closed client, and sdk_unsupported
// are yielded as the last error.
func (c *Client) WatchEvents(ctx context.Context, args *WatchEventsArgs) iter.Seq2[Event, error] {
	if args == nil {
		args = &WatchEventsArgs{}
	}
	settings := *args
	if settings.Wait == 0 {
		settings.Wait = eventWait
	}
	return func(yield func(Event, error) bool) {
		cursor := settings.After
		delay := firstRetryDelay
		for {
			page, err := c.Events(ctx, &EventsArgs{After: cursor, Wait: settings.Wait})
			if err != nil {
				if ctx.Err() != nil || IsCode(err, CodeCancelled) || IsCode(err, CodeClosed) || IsCode(err, CodeSDKUnsupported) || IsCode(err, CodeInvalidArgument) {
					yield(Event{}, err)
					return
				}
				if settings.OnError != nil {
					settings.OnError(err)
				}
				select {
				case <-ctx.Done():
					yield(Event{}, &Error{Code: CodeCancelled, Message: "the watch was cancelled", Err: ctx.Err()})
					return
				case <-time.After(delay):
				}
				delay = min(delay*2, maximumRetryDelay)
				continue
			}
			delay = firstRetryDelay
			for _, event := range page.Events {
				cursor = max(cursor, event.ID)
				if !yield(event, nil) {
					return
				}
			}
			cursor = max(cursor, page.Next)
		}
	}
}
