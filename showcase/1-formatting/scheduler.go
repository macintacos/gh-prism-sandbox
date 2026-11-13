// Package scheduler queues the nightly finance jobs: settlement runs,
// statement exports, and the retries that follow a failed run.
package scheduler

import (
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

// ErrClosed is returned once the scheduler has shut down.
var ErrClosed = errors.New("scheduler closed")

// ErrDuplicate is returned when a job with the same key is queued.
var ErrDuplicate = errors.New("job already queued")

const (
	defaultWindow = 15 * time.Minute
	maxRetries    = 6
	baseBackoff   = 30 * time.Second
	maxBackoff    = 20 * time.Minute
)

// Options tunes one scheduled job.
type Options struct {
	Window  time.Duration
	Retries int
	Tags    []string
}

// Job is one unit of work waiting on the queue.
type Job struct {
	Key     string
	Kind    string
	At      time.Time
	Window  time.Duration
	Attempt int
}

// Handle lets a caller cancel or inspect a queued job.
type Handle struct {
	Key       string
	QueuedAt  time.Time
	cancelled bool
}

// Scheduler holds the queue and the jobs waiting on it.
type Scheduler struct {
	mu      sync.Mutex
	closed  bool
	queue   *Queue
	pending map[string]*Handle
	metrics Metrics
	now     func() time.Time
}

// New returns a scheduler that reads the time from now.
func New(q *Queue, m Metrics, now func() time.Time) *Scheduler {
	return &Scheduler{
		queue:   q,
		pending: make(map[string]*Handle),
		metrics: m,
		now:     now,
	}
}

// Schedule queues a job of kind to run at the given time.
func (s *Scheduler) Schedule(
	kind, key string,
	at time.Time,
	opts Options) (*Handle, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil, ErrClosed
	}
	if _, ok := s.pending[key]; ok {
		return nil, ErrDuplicate
	}

	window := opts.Window
	if window <= 0 {
		window = defaultWindow
	}
	job := Job{Key: key, Kind: kind, At: at, Window: window}
	err := s.queue.Push(
		job,
		opts.Tags...)
	if err != nil {
		return nil, fmt.Errorf("queue %s job: %w", kind, err)
	}

	handle := &Handle{Key: key, QueuedAt: s.now()}
	s.pending[key] = handle
	s.metrics.Inc("scheduler.queued", kind)
	return handle, nil
}

// Cancel removes a queued job. It reports whether the job was still
// waiting; a job that already started cannot be cancelled.
func (s *Scheduler) Cancel(h *Handle) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if h.cancelled {
		return false
	}
	key := h.Key
	if _, ok := s.pending[key]; !ok ||
		s.queue.Started(key) ||
		s.closed {
		return false
	}

	h.cancelled = true
	delete(s.pending, key)
	s.queue.Remove(key)
	s.metrics.Inc("scheduler.cancelled", "")
	return true
}

// Backoff returns how long to wait before the given retry attempt.
// The wait doubles each attempt and stops growing at maxBackoff.
func Backoff(attempt int) time.Duration {
	if attempt <= 0 {
		return 0
	}
	wait := baseBackoff
	for i := 1; i < attempt; i++ {
		wait *= 2
		if wait >= maxBackoff {
			return maxBackoff
		}
	}
	return wait
}

// Retry requeues a failed job after its backoff, unless it has used
// up its retries.
func (s *Scheduler) Retry(job Job, opts Options) error {
	limit := opts.Retries
	if limit <= 0 || limit > maxRetries {
		limit = maxRetries
	}
	if job.Attempt >= limit {
		s.metrics.Inc("scheduler.gave_up", job.Kind)
		return fmt.Errorf("job %s: out of retries", job.Key)
	}

	next := job
	next.Attempt++
	next.At = s.now().Add(Backoff(next.Attempt))
	if err := s.queue.Push(next, opts.Tags...); err != nil {
		return err
	}
	s.metrics.Inc("scheduler.retried", job.Kind)
	return nil
}

// Due returns the jobs whose start time has passed, in key order.
func (s *Scheduler) Due() []Job {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := s.now()
	var due []Job
	for _, job := range s.queue.Peek() {
		if !job.At.After(now) {
			due = append(due, job)
		}
	}
	sort.Slice(due, func(i, j int) bool {
		return due[i].Key < due[j].Key
	})
	return due
}

// Missed returns the jobs whose window closed before they ran.
func (s *Scheduler) Missed() []Job {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := s.now()
	var missed []Job
	for _, job := range s.queue.Peek() {
		end := job.At.Add(job.Window)
		if end.Before(now) {
			missed = append(missed, job)
		}
	}
	return missed
}

// Report summarises the queue for the operations dashboard.
type Report struct {
	Queued    int
	Due       int
	Missed    int
	Retrying  int
	OldestAge time.Duration
}

// Snapshot builds a Report from the current queue.
func (s *Scheduler) Snapshot() Report {
	due := s.Due()
	missed := s.Missed()

	s.mu.Lock()
	defer s.mu.Unlock()
	retrying := 0
	var oldest time.Time
	for _, job := range s.queue.Peek() {
		if job.Attempt > 0 {
			retrying++
		}
		if oldest.IsZero() || job.At.Before(oldest) {
			oldest = job.At
		}
	}

	queued := len(s.pending)
	report := Report{
		Queued:   queued,
		Due:      len(due),
		Missed:   len(missed),
		Retrying: retrying}
	if !oldest.IsZero() {
		report.OldestAge = s.now().Sub(oldest)
	}
	return report
}

// Drain waits until every queued job has started, or until the
// deadline passes. It returns the keys still waiting at the deadline.
func (s *Scheduler) Drain(deadline time.Time) []string {
	for s.now().Before(deadline) {
		s.mu.Lock()
		left := len(s.pending)
		s.mu.Unlock()
		if left == 0 {
			return nil
		}
		time.Sleep(time.Second)
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	keys := make([]string, 0, len(s.pending))
	for key := range s.pending {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// Close stops the scheduler. Jobs already queued stay on the queue
// for the next process to pick up.
func (s *Scheduler) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return ErrClosed
	}
	s.closed = true
	for key, h := range s.pending {
		if !s.queue.Started(key) {
			s.metrics.Inc("scheduler.left_queued", "")
		}
		h.cancelled = false
	}
	s.pending = nil
	return nil
}

// Nightly queues the settlement and export jobs for the given night.
func (s *Scheduler) Nightly(night time.Time, tags []string) error {
	start := time.Date(night.Year(), night.Month(), night.Day(),
		1, 30, 0, 0, time.UTC)

	opts := Options{Window: time.Hour, Retries: 3, Tags: tags}
	settleKey := "settle:" + night.Format("2006-01-02")
	if _, err := s.Schedule("settle", settleKey, start, opts); err != nil {
		return err
	}

	at := start.Add(2 * time.Hour)
	key := "export:" + night.Format("2006-01-02")
	if _, err := s.Schedule("export", key, at, opts); err != nil {
		return err
	}
	return nil
}
