package main

import (
	"sync"
	"testing"
	"time"
)

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
		assertEqual(t, calls, 3)
	})

	t.Run("concurrent processing and counting", func(t *testing.T) {
		const goroutines, opsPerGoroutine = 100, 1000
		mw := &MeasuredWorker{Worker: workerFunc(func() {})}

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
