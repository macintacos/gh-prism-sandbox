// Package cache keeps a two-level cache of finance records: a local
// shard per process in front of a shared store.
package cache

import (
	"context"
	"errors"
	"sync"
	"time"
)

// ErrMissing is returned when neither level holds the key.
var ErrMissing = errors.New("cache: key not found")

// DefaultTTL suits records that change at most a few times a day.
const DefaultTTL = 5 * time.Minute

// Entry is one cached record. Data holds the encoded record exactly as
// the shared store returned it; Expires is when the local copy stops
// being trusted, which may be earlier than the store's own expiry.
type Entry struct {
	Key     string
	Data    []byte
	Expires time.Time
	Version uint64
}

// Store is the shared level: slower, but seen by every process.
type Store interface {
	Load(ctx context.Context, key string) (Entry, error)
	Save(ctx context.Context, e Entry) error
	Delete(ctx context.Context, key string) error
}

// Cache reads through a local shard to the shared store.
type Cache struct {
	mu     sync.RWMutex
	local  map[string]Entry
	shared Store
	ttl    time.Duration
	now    func() time.Time
	stats  Stats
}

// Stats counts what the cache did since it started.
type Stats struct {
	Hits    uint64 // served from the local shard
	Misses  uint64 // went to the shared store
	Expired uint64 // found locally but too old to use
	Evicted uint64
}

// New returns a cache whose local entries live for ttl.
func New(shared Store, ttl time.Duration, now func() time.Time) *Cache {
	return &Cache{
		local:  make(map[string]Entry),
		shared: shared,
		ttl:    ttl,
		now:    now,
	}
}

// Get returns the cached record for key. It reads the local
// shard first and falls back to the shared store when the entry is
// missing or expired, and it refreshes the local copy on the way out.
func (c *Cache) Get(ctx context.Context, key string) (Entry, error) {
	c.mu.RLock()
	entry, ok := c.local[key]
	c.mu.RUnlock()
	if ok && c.now().Before(entry.Expires) {
		c.count(func(s *Stats) { s.Hits++ })
		return entry, nil
	}
	if ok {
		c.count(func(s *Stats) { s.Expired++ })
	}

	entry, err := c.shared.Load(ctx, key)
	if err != nil {
		c.count(func(s *Stats) { s.Misses++ })
		return Entry{}, err
	}
	c.keep(entry)
	return entry, nil
}

/*
 * Put writes an entry to both levels. The shared store is written
 * first, so a crash between the two writes leaves the local shard
 * stale rather than ahead of the store.
 */
func (c *Cache) Put(ctx context.Context, e Entry) error {
	if err := c.shared.Save(ctx, e); err != nil {
		return err
	}
	c.keep(e)
	return nil
}

// Invalidate drops key from both levels.
//
// It is safe to call for a key that was never cached. Callers that
// change a record must invalidate it after the write commits, not
// before, or a concurrent reader can refill the cache with the old
// value.
//
// Invalidate does not wait for other processes to drop their local
// copies; they keep serving them until their own ttl runs out.
func (c *Cache) Invalidate(ctx context.Context, key string) error {
	c.mu.Lock()
	delete(c.local, key)
	c.mu.Unlock()
	return c.shared.Delete(ctx, key)
}

// keep stores e in the local shard with a fresh local expiry.
func (c *Cache) keep(e Entry) {
	e.Expires = c.now().Add(c.ttl)
	c.mu.Lock()
	c.local[e.Key] = e
	c.mu.Unlock()
}

// count applies f to the stats under the lock.
func (c *Cache) count(f func(*Stats)) {
	c.mu.Lock()
	f(&c.stats)
	c.mu.Unlock()
}

// Snapshot returns a copy of the stats.
func (c *Cache) Snapshot() Stats {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.stats
}

// Sweep removes every local entry that has expired and reports how
// many it removed. Run it on a timer; Get already skips expired
// entries, so Sweep only bounds the shard's memory.
func (c *Cache) Sweep() int {
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	removed := 0
	for key, e := range c.local {
		if !now.Before(e.Expires) {
			delete(c.local, key)
			removed++
		}
	}
	c.stats.Evicted += uint64(removed)
	return removed
}

// Warm loads keys from the shared store into the local shard ahead of
// a busy period, such as the hour before the nightly settlement run.
// It stops at the first error and returns it.
func (c *Cache) Warm(ctx context.Context, keys []string) error {
	for _, key := range keys {
		entry, err := c.shared.Load(ctx, key)
		if err != nil {
			return err
		}
		c.keep(entry)
	}
	return nil
}

// GetMany returns the entries for keys that either level holds.
func (c *Cache) GetMany(
	ctx context.Context,
	keys []string,
) map[string]Entry {
	found := make(map[string]Entry, len(keys))
	for _, key := range keys {
		// A missing key is not an error here: the caller asked for
		// whatever is cached, and gets a shorter map.
		entry, err := c.Get(ctx, key)
		if err != nil {
			continue
		}
		found[key] = entry
	}
	return found
}

// Refresh reloads key from the shared store if the local copy is older
// than the version the caller has seen. It returns the entry it ends
// up with.
func (c *Cache) Refresh(
	ctx context.Context,
	key string,
	seen uint64,
) (Entry, error) {
	c.mu.RLock()
	entry, ok := c.local[key]
	c.mu.RUnlock()
	// The local copy is at least as new as what the caller saw, so
	// there is nothing to reload. Skipping the store here is what
	// keeps a burst of refreshes from turning into a burst of loads.
	if ok && entry.Version >= seen {
		return entry, nil
	}

	entry, err := c.shared.Load(ctx, key)
	if err != nil {
		return Entry{}, err
	}
	c.keep(entry)
	return entry, nil
}

/*
 * Len returns how many entries the local shard holds, expired or not.
 * It is meant for metrics and tests; do not use it to decide whether
 * a key is cached.
 */
func (c *Cache) Len() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.local)
}

// Keys returns the keys in the local shard, in no particular order.
func (c *Cache) Keys() []string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	keys := make([]string, 0, len(c.local))
	for key := range c.local {
		keys = append(keys, key)
	}
	return keys
}

// Clear empties the local shard. The shared store is left alone, so
// the next Get for each key goes to the store and refills the shard.
func (c *Cache) Clear() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stats.Evicted += uint64(len(c.local))
	c.local = make(map[string]Entry)
}

// TTL returns how long local entries live.
func (c *Cache) TTL() time.Duration {
	return c.ttl
}

// SetTTL changes how long new local entries live. Entries already in
// the shard keep the expiry they were stored with; call Clear as well
// to apply the new ttl to every key at once.
func (c *Cache) SetTTL(ttl time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ttl = ttl
}
