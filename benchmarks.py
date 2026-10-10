"""Measure Telegram upload/download latency and throughput with test files.

Configure BOT_TOKEN and BENCHMARK_CHAT_ID in the environment or .env, then run:
    python benchmark_transfers.py

The benchmark sends and deletes four test documents in the configured chat.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # never emit __pycache__ / *.pyc

import asyncio
import hashlib
import io
import logging
import os
import time

from dotenv import load_dotenv
from telegram import Bot, InputFile
from telegram.request import HTTPXRequest

load_dotenv()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("transfer_benchmark")

FILE_SIZES_MB = (1, 5, 10, 15)


async def benchmark(bot: Bot, chat_id: str) -> None:
    for size_mb in FILE_SIZES_MB:
        payload = os.urandom(size_mb * 1_000_000)
        expected_hash = hashlib.sha256(payload).hexdigest()
        name = f"telegram-transfer-{size_mb}MB.bin"

        upload_started = time.perf_counter()
        sent = await bot.send_document(
            chat_id=chat_id,
            document=InputFile(payload, filename=name),
            disable_notification=True,
        )
        upload_seconds = time.perf_counter() - upload_started
        if not sent.document:
            raise RuntimeError(f"Telegram did not return a document for {name}")

        try:
            lookup_started = time.perf_counter()
            remote_file = await bot.get_file(sent.document.file_id)
            lookup_seconds = time.perf_counter() - lookup_started

            downloaded = io.BytesIO()
            download_started = time.perf_counter()
            await remote_file.download_to_memory(out=downloaded)
            download_seconds = time.perf_counter() - download_started
            received = downloaded.getvalue()
            if len(received) != len(payload):
                raise RuntimeError(
                    f"{name}: size mismatch (sent {len(payload)}, received {len(received)})"
                )
            if hashlib.sha256(received).hexdigest() != expected_hash:
                raise RuntimeError(f"{name}: SHA-256 mismatch")
        finally:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
            except Exception:
                logger.exception("Could not delete benchmark message %s", sent.message_id)

        upload_mbps = len(payload) * 8 / upload_seconds / 1_000_000
        download_mbps = len(received) * 8 / download_seconds / 1_000_000
        total_seconds = upload_seconds + lookup_seconds + download_seconds
        logger.info(
            "%d MB integrity=PASS upload_request=%.3fs (%.2f Mbps) "
            "file_lookup=%.3fs download=%.3fs (%.2f Mbps) total=%.3fs; "
            "upload request includes Telegram processing",
            size_mb, upload_seconds, upload_mbps,
            lookup_seconds, download_seconds, download_mbps, total_seconds,
        )


async def main() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("BENCHMARK_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("Set BOT_TOKEN and BENCHMARK_CHAT_ID before running this benchmark.")

    request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=60.0,
        read_timeout=180.0,
        write_timeout=180.0,
        pool_timeout=60.0,
    )
    bot = Bot(token=token, request=request)
    await bot.initialize()
    try:
        await benchmark(bot, chat_id)
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
