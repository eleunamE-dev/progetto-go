import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field


@dataclass
class Checks:
    passed: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def check(self, name: str, condition: object, detail: object = "") -> bool:
        if condition:
            self.passed += 1
            print(f"PASS  {name}", flush=True)
        else:
            self.failed.append((name, str(detail)))
            print(f"FAIL  {name}   <- {str(detail)[:300]}", flush=True)
        return bool(condition)

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append(name)
        print(f"SKIP  {name}: {reason}", flush=True)

    def run(self, sections: Sequence[tuple[str, Callable[[], None]]]) -> None:
        for title, section in sections:
            print(f"\n=== {title}", flush=True)
            try:
                section()
            except Exception as exc:
                self.check(f"{title}: ran to the end", False, f"{type(exc).__name__}: {exc}")

    def report(self) -> int:
        print(f"\n{self.passed} passed, {len(self.failed)} failed, {len(self.skipped)} skipped")
        for name, detail in self.failed:
            print(f"  FAIL {name}: {detail[:300]}")
        return 1 if self.failed else 0


def wait_until[T](predicate: Callable[[], T], timeout: float, interval: float = 0.5) -> T | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(interval)
    return None
