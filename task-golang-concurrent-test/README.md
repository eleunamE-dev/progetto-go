# Golang concurrent test

We're writing a `MeasuredWorker` that tries to parallelize and count the work done by a generic worker defined by the
interface `Worker`.

A test has been written, however we realized two problems:

- when processing concurrently the count not always matches the expected number of operations done
- the test requires over 20 seconds to run

## Constraints

You're allowed to change only the `measured_worker.go` and the `main_test.go` files.

## Instructions

If you have Go installed you can run the tests by simply doing:

```bash
go test .
```

Otherwise, you can use the included Dockerfile to build and run the tests in a container:

```bash
docker run --rm -it $(docker build -q .)
```

## Tips

Remember that the focus is `MeasuredWorker`, so feel free to mock or workaround the other components that are in the
files you're not allowed to edit!