# Exercises

| Folder | Exercise | Language |
|---|---|---|
| [task-golang-concurrent-test](task-golang-concurrent-test) | Fix a concurrency bug and a slow test | Go |
| [project-be-books](project-be-books) | Book review service | Python |

## task-golang-concurrent-test

The assignment is in [task-golang-concurrent-test/README.md](task-golang-concurrent-test/README.md);
only `measured_worker.go` and `main_test.go` could change.

**Wrong count.** `value++` on a plain `int` is a read-modify-write: two goroutines can read the same
value, and one of the two updates is lost (the original test sometimes got 999 instead of 1000).
The counter is now an `atomic.Int64`.

Only the increment is synchronized. Putting the whole `Work` behind a mutex would also fix the
count, but it would make the work sequential: 1000 calls of the real `SlowWorker` would take
5000 s instead of 5.

**Slow test.** The 20 s came from `SlowWorker`, which sleeps 5 s per call and cannot be changed.
The tests now use a function-based test double instead, which brings them down to about 0.2 s.
They also check more than before:

- The concurrent test runs 100 goroutines × 1000 calls, all released at the same time. The old
  code fails it every time with two or more CPUs, whereas the original test caught the bug only
  sometimes.
- A new test checks that calls to the wrapped worker really run in parallel. The mutex-around-`Work`
  fix fails it with a timeout instead of hanging.

```bash
cd task-golang-concurrent-test
go test .
# With the race detector, which needs cgo (the Dockerfile disables it):
docker run --rm -e CGO_ENABLED=1 $(docker build -q .) go test -race .
```

## project-be-books

See [project-be-books/README.md](project-be-books/README.md), starting from its
[Start here](project-be-books/README.md#start-here) section: how to run and try the service in a
few minutes, where each part of the assignment is, and why the rest was added.
