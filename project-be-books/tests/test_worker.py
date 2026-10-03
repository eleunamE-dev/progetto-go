import asyncio

import pytest

from bookreviews import worker
from bookreviews.config import Settings
from bookreviews.queue import Topology
from bookreviews.worker import WorkerOptions


def test_options_come_from_the_settings() -> None:
    settings = Settings(
        worker_concurrency=8,
        enrichment_max_attempts=3,
        sweep_interval=30,
        sweep_after=120,
        worker_shutdown_timeout=5,
    )

    assert WorkerOptions.from_settings(settings) == WorkerOptions(
        topology=Topology(),
        concurrency=8,
        max_attempts=3,
        sweep_interval=30,
        sweep_after=120,
        shutdown_timeout=5,
    )


async def test_main_runs_the_worker_until_a_stop_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Settings, asyncio.Event]] = []

    async def fake_serve(settings: Settings, stop: asyncio.Event) -> None:
        calls.append((settings, stop))

    monkeypatch.setattr(worker, "serve", fake_serve)
    settings = Settings()

    await worker.main(settings)

    [(served, stop)] = calls
    assert served is settings
    assert not stop.is_set()
