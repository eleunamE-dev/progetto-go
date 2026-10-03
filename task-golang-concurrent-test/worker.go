package main

import "time"

type Worker interface {
	Work()
}

type SlowWorker struct {
}

func (s *SlowWorker) Work() {
	time.Sleep(5 * time.Second)
}
