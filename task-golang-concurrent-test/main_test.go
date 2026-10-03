package main

import (
	"sync"
	"testing"
)

func TestCounter(t *testing.T) {

	t.Run("processing 3 times brings the counter to 3", func(t *testing.T) {
		mw := &MeasuredWorker{Worker: &SlowWorker{}}

		mw.Work()
		mw.Work()
		mw.Work()

		assertEqual(t, mw.Value(), 3)
	})

	t.Run("concurrent processing and counting", func(t *testing.T) {
		wantedCount := 1000
		mw := &MeasuredWorker{Worker: &SlowWorker{}}

		var wg sync.WaitGroup
		wg.Add(wantedCount)

		for i := 0; i < wantedCount; i++ {
			go func() {
				mw.Work()
				wg.Done()
			}()
		}
		wg.Wait()

		gotCount := mw.Value()
		assertEqual(t, gotCount, wantedCount)
	})

}

func assertEqual(t testing.TB, got int, want int) {
	t.Helper()
	if got != want {
		t.Errorf("got %d, want %d", got, want)
	}
}
