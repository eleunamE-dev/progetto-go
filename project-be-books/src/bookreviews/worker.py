import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass, field
from typing import Self

import aio_pika

from bookreviews.catalog import BookCatalog
from bookreviews.config import Settings
from bookreviews.database import SqlReviewRepository, create_sessions
from bookreviews.enrichment import EnrichmentRepository, MessageHandler, ReviewEnricher, Sweeper
from bookreviews.gutendex import GutendexClient
from bookreviews.queue import CONNECT_TIMEOUT, RabbitQueue, Topology
from bookreviews.wiring import build_catalog, build_engine

logger = logging.getLogger("bookreviews.worker")


@dataclass(frozen=True, slots=True)
class WorkerOptions:
    topology: Topology = field(default_factory=Topology)
    concurrency: int = 4
    max_attempts: int = 5
    deadline: float = 86_400
    sweep_interval: float = 60
    sweep_after: float = 600
    shutdown_timeout: float = 15

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            concurrency=settings.worker_concurrency,
            max_attempts=settings.enrichment_max_attempts,
            deadline=settings.enrichment_deadline,
            sweep_interval=settings.sweep_interval,
            sweep_after=settings.sweep_after,
            shutdown_timeout=settings.worker_shutdown_timeout,
        )


async def consume(
    rabbitmq_url: str,
    reviews: EnrichmentRepository,
    catalog: BookCatalog,
    stop: asyncio.Event,
    options: WorkerOptions,
) -> None:
    topology = options.topology
    queue = RabbitQueue(rabbitmq_url, topology)
    handler = MessageHandler(
        ReviewEnricher(reviews, catalog), topology, options.max_attempts, parking=queue
    )
    sweeper = Sweeper(reviews, queue, stale_after=options.sweep_after, deadline=options.deadline)
    connection = await aio_pika.connect_robust(rabbitmq_url, timeout=CONNECT_TIMEOUT)
    try:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=options.concurrency)
        amqp_queue = await topology.declare(channel)
        consumer_tag = await amqp_queue.consume(handler)
        sweeping = asyncio.create_task(sweeper.run(options.sweep_interval, stop))
        logger.info("worker started", extra={"queue": topology.queue})

        await stop.wait()

        logger.info("worker stopping")
        await amqp_queue.cancel(consumer_tag)
        await handler.drain(grace_period=options.shutdown_timeout)
        await sweeping
    finally:
        await queue.close()
        await connection.close()
    logger.info("worker stopped")


async def serve(settings: Settings, stop: asyncio.Event) -> None:
    engine = build_engine(settings)
    try:
        async with GutendexClient(
            str(settings.gutendex_base_url), settings.gutendex_timeout
        ) as gutendex:
            await consume(
                settings.rabbitmq_url.get_secret_value(),
                SqlReviewRepository(create_sessions(engine)),
                build_catalog(gutendex, settings),
                stop,
                WorkerOptions.from_settings(settings),
            )
    finally:
        await engine.dispose()


async def main(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    await serve(settings, stop)
