package main

import (
	"sync"
	"testing"
	"time"
)

// workerFunc adapts a plain function to the Worker interface. The tests use it
// instead of SlowWorker: what is under test is MeasuredWorker, so there is no
// reason to wait 5 seconds for every operation.
type workerFunc func()

func (f workerFunc) Work() { f() }

func TestCounter(t *testing.T) {

	t.Run("processing 3 times brings the counter to 3", func(t *testing.T) {
		calls := 0
		mw := &MeasuredWorker{Worker: workerFunc(func() { calls++ })}

		mw.Work()
		mw.Work()
		mw.Work()

		assertEqual(t, mw.Value(), 3)
		assertEqual(t, calls, 3) // every operation reached the wrapped worker
	})

	t.Run("concurrent processing and counting", func(t *testing.T) {
		const goroutines, opsPerGoroutine = 100, 1000
		mw := &MeasuredWorker{Worker: workerFunc(func() {})}

		// All goroutines wait on the same signal and then hammer the counter
		// together: the more they overlap, the more likely a non-atomic
		// increment loses updates, even without the race detector.
		start := make(chan struct{})
		var wg sync.WaitGroup
		wg.Add(goroutines)
		for range goroutines {
			go func() {
				defer wg.Done()
				<-start
				for range opsPerGoroutine {
					mw.Work()
				}
			}()
		}
		close(start)
		wg.Wait()

		assertEqual(t, mw.Value(), goroutines*opsPerGoroutine)
	})

	t.Run("operations on the wrapped worker run in parallel", func(t *testing.T) {
		const parallelism = 10

		// Every call blocks until all `parallelism` calls are in progress at
		// the same time. If MeasuredWorker serialised them (e.g. by holding a
		// lock while working), the first call would block the others forever.
		var arrived sync.WaitGroup
		arrived.Add(parallelism)
		mw := &MeasuredWorker{Worker: workerFunc(func() {
			arrived.Done()
			arrived.Wait()
		})}

		var wg sync.WaitGroup
		wg.Add(parallelism)
		for range parallelism {
			go func() {
				defer wg.Done()
				mw.Work()
			}()
		}

		waitOrFail(t, &wg, 5*time.Second, "calls to the wrapped worker were serialised")
		assertEqual(t, mw.Value(), parallelism)
	})

}

func assertEqual(t testing.TB, got int, want int) {
	t.Helper()
	if got != want {
		t.Errorf("got %d, want %d", got, want)
	}
}

// waitOrFail waits for wg, failing the test if it takes longer than timeout,
// so that a deadlock shows up as a test failure instead of a hung test run.
func waitOrFail(t testing.TB, wg *sync.WaitGroup, timeout time.Duration, msg string) {
	t.Helper()
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(timeout):
		t.Fatalf("timed out after %s: %s", timeout, msg)
	}
}
