package main

import "sync/atomic"

type MeasuredWorker struct {
	Worker
	value atomic.Int64
}

func (m *MeasuredWorker) Work() {
	m.Worker.Work()
	m.value.Add(1)
}

func (m *MeasuredWorker) Value() int {
	return int(m.value.Load())
}
