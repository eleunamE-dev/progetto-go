package main

import (
	"sync"
)

func main() {
	operations := 100
	mw := &MeasuredWorker{Worker: &SlowWorker{}}

	println("Starting", operations, "operations.")

	wg := sync.WaitGroup{}
	wg.Add(operations)

	for i := 0; i < operations; i++ {
		go func() {
			mw.Work()
			wg.Done()
		}()
	}

	wg.Wait()

	println("Operations counted: ", mw.Value())
}
