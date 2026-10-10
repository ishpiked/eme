"""Measure Telegram Bot API document transfer time independently of song work."""

import asyncio
import hashlib
import os
import time
from io import BytesIO

from dotenv import load_dotenv
from telegram import Bot, InputFile
from telegram.request import HTTPXRequest

from transport import local_api_urls

SIZES_MB = (1, 5, 10, 15)


async def benchmark(bot: Bot, chat_id: int) -> None:
    for size_mb in SIZES_MB:
        payload = os.urandom(size_mb * 1_000_000)
        expected_hash = hashlib.sha256(payload).hexdigest()
        document = InputFile(
            BytesIO(payload),
            filename=f"telegram-transfer-{size_mb}mb.bin",
        )

        upload_started = time.perf_counter()
        message = await bot.send_document(chat_id, document=document)
        upload_seconds = time.perf_counter() - upload_started
        upload_mbps = len(payload) * 8 / upload_seconds / 1_000_000
        print(
            f"UPLOAD size={size_mb}MB request={upload_seconds:.2f}s "
            f"effective={upload_mbps:.2f}Mbps "
            "(includes Telegram processing)"
        )

        if not message.document:
            raise RuntimeError("Telegram did not return the uploaded document.")
        telegram_file = await bot.get_file(message.document.file_id)
        download_started = time.perf_counter()
        downloaded = bytes(await telegram_file.download_as_bytearray())
        download_seconds = time.perf_counter() - download_started
        if hashlib.sha256(downloaded).hexdigest() != expected_hash:
            raise RuntimeError(f"SHA-256 verification failed for {size_mb}MB.")
        download_mbps = len(downloaded) * 8 / download_seconds / 1_000_000
        print(
            f"DOWNLOAD size={size_mb}MB request={download_seconds:.2f}s "
            f"effective={download_mbps:.2f}Mbps SHA-256=OK"
        )

        await bot.delete_message(chat_id, message.message_id)


async def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    chat_id_value = os.getenv("BENCHMARK_CHAT_ID", "").strip()
    if not token:
        raise SystemExit("Set BOT_TOKEN in the environment or .env file.")
    try:
        chat_id = int(chat_id_value)
    except ValueError as error:
        raise SystemExit("Set BENCHMARK_CHAT_ID to a numeric chat ID.") from error

    request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=30.0,
        read_timeout=180.0,
        write_timeout=180.0,
        media_write_timeout=180.0,
        pool_timeout=30.0,
    )
    local_api_url = os.getenv("TELEGRAM_LOCAL_API_URL", "").strip()
    local_urls = local_api_urls(local_api_url)
    if local_urls:
        bot = Bot(
            token=token,
            request=request,
            base_url=local_urls[0],
            base_file_url=local_urls[1],
            local_mode=True,
        )
        print(f"Benchmarking Local Bot API at {local_api_url.rstrip('/')}")
    else:
        bot = Bot(token=token, request=request)
        print("Benchmarking Telegram's hosted Bot API")
    await bot.initialize()
    try:
        await benchmark(bot, chat_id)
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
