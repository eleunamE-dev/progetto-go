package main

import "sync/atomic"

// MeasuredWorker decorates a Worker and counts how many operations it has
// completed. It is safe for concurrent use: calls to the wrapped Worker still
// run in parallel, only the counter update is synchronised.
type MeasuredWorker struct {
	Worker
	value atomic.Int64
}

// Work runs the wrapped Worker and, once it has finished, records the
// completed operation.
//
// The increment must be atomic: a plain `value++` is a read-modify-write, so
// two goroutines can read the same value and one of the updates gets lost.
// Guarding the whole method with a mutex would also fix the count, but it would
// serialise the wrapped Worker and defeat the point of running it concurrently.
func (m *MeasuredWorker) Work() {
	m.Worker.Work()
	m.value.Add(1)
}

// Value returns the number of operations completed so far.
func (m *MeasuredWorker) Value() int {
	return int(m.value.Load())
}
