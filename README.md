# Telegram Song Bot

Search and play songs by name, or identify music from public YouTube links. YouTube links receive a direct-stream video preview capped at 720p; recognition starts only after the user taps **Recognize Song**. Other media platforms are not supported.

Selecting a search result starts a fresh YouTube stream resolution and audio download. YouTube links sent as messages keep the separate video-preview and **Recognize Song** flow.

Telegram inline mode (`@botname song`) shows up to ten audio results. Selecting one makes Telegram fetch the fresh direct audio stream and post it in the chat; this skips the bot's download, 320 kbps MP3 conversion, and upload, so inline audio uses the source bitrate provided by YouTube. Use `@botname video: song name` for up to ten matching MP4 videos (720p maximum), paste a YouTube video URL or ID for a video result, or paste a YouTube Music URL for audio. Inline stream URLs are short-lived and depend on Telegram being able to fetch them. Inline queries are time-bounded to keep responses fast; results that do not resolve in time are omitted.

## Commands
- `/start`, `/help`: welcome message
- In a private chat, send a song name to choose from paginated search results (ten tracks per page, two per row). Send a supported public video link to receive a preview (up to 720p); tap **Recognize Song** to identify and play the match. Send an audio or video upload to get a **Recognize Song** button as well.
- `/search <name or video link>`: use this command in groups, e.g. `/search kesariya`. Names show paginated results (ten tracks per page, two per row);
- `/play <link or video id>`: send a supported YouTube video preview (up to 720p) with a **Recognize Song** button. YouTube Music links go straight to MP3 audio.
- `/playlist`: list and open your private playlists.
- `/playlist create <name>`: create a playlist. When prompted from a song's Playlist button, the current song is saved into it automatically.
- `/playlist <id>`: browse and play saved songs. `/playlist delete <id>` deletes a playlist.

Playlists are available to every user in a private chat; they are not owner-only. Each sent song has a cloud-icon **Playlist** button. Save songs to an existing playlist or create a new one from that button. Playlist entries store the YouTube video ID and metadata; selecting a playlist item fetches a fresh stream URL and uploads a new audio file.

## Setup
1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token, and enable inline mode with `/setinline` if you want `@botname song` results.
2. Install Python 3.11 or newer, ffmpeg, and a JavaScript runtime such as Deno for yt-dlp's current YouTube challenge handling.
3. Install deps:
   ```powershell
   pip install --upgrade -r requirements.txt
   ```
4. Configure:
   ```powershell
   Copy-Item .env.example .env
   # set BOT_TOKEN and your numeric Telegram BOT_OWNER_ID
   ```
   For an optional same-VPS Bot API hop, run the official [Telegram Local Bot API server](https://core.telegram.org/bots/api#using-a-local-bot-api-server), then set `TELEGRAM_LOCAL_API_URL=http://127.0.0.1:8081` in `.env`. Keep the service bound to loopback or a trusted private interface; do not expose its port publicly. This may reduce client-to-API overhead, but cannot guarantee the VPS-to-Telegram network route's throughput.
5. Run:
   ```powershell
   python bot.py
   ```

## Config (.env)
| Key | Default | Description |
|-----|---------|-------------|
| `BOT_TOKEN` | none | Telegram bot token (required) |
| `BOT_OWNER_ID` | `0` | Numeric Telegram ID authorized for private owner controls |
| `BOT_DESCRIPTION` | Built-in Emy description | Bot profile description |
| `BOT_ABOUT` | Built-in Emy short description | Bot profile short description |
| `BOT_PROFILE_PHOTO` | none | Optional local JPEG file for the bot profile photo |
| `MAX_DURATION_SEC` | `600` | Reject songs longer than this (also saves RAM) |
| `MAX_FILE_MB` | `49` | Telegram Bot API ~50MB limit (also RAM cap) |
| `TELEGRAM_LOCAL_API_URL` | unset | Optional Local Bot API server origin, e.g. `http://127.0.0.1:8081` |
| `BOT_DATA_DIR` | OS user config directory | Private storage for bot state |
| `BOT_DATABASE_FILE` | `<BOT_DATA_DIR>/bot-state.sqlite3` | Optional user, playlist, and broadcast database override |

## How it works (temporary media, no persistent audio cache)
1. **Track search**: Search results are parsed from YouTube's public `/results` page, in relevance order. Results are cached in memory for five minutes; a consent page, changed markup, or rate limit is reported as a search error.
2. Song downloads use yt-dlp's YouTube extractor to select and download the best available audio, then its FFmpeg audio postprocessor extracts MP3 at `MP3_QUALITY` (320 kbps by default). Install the `yt-dlp[default]` extra and a JavaScript runtime such as Deno to help yt-dlp handle YouTube's changing player challenges; YouTube can still deny media access to some server IPs. The bot's own YouTube results-page parser remains responsible for search; player resolution and direct HTTP range downloads are not used for normal song playback.
3. Recognition extracts a 10-second audio sample from the start of the uploaded file or YouTube preview (uploads/previews are limited to 20 MiB) and submits it to the unauthenticated ShazamIO recognition service. Recognition is best-effort and can fail for intros, unclear audio, provider outages, or tracks Shazam does not identify.
4. Audio is held in temporary working files during download/conversion, read into RAM for upload, and the temporary directory is removed at the end of processing.
5. Bot sends the bytes through Telegram's `InputFile` and retains the untouched source audio only in a bounded in-memory effect session. Per-user effect settings are stored in `user.json`; owner/broadcast state is stored outside the project by default.
- No local song or Telegram `file_id` playback cache is read or written. The bot keeps search-result metadata in memory for five minutes, but yt-dlp fetches fresh extraction metadata and media for every playback. yt-dlp's own cache is disabled; download and conversion work files are temporary and removed when processing finishes.
- Telegram inline mode uses the player's direct audio-only MP4 streams for text searches, YouTube links, and 11-character video IDs. Telegram fetches the signed YouTube URL itself rather than sending the audio through the bot; the available quality is whatever YouTube exposes, and a signed URL may expire or be inaccessible to Telegram. Enable inline mode with BotFather's `/setinline`. Inline answers are not cached.

### Audio effects and transfer timings
- The Effect menu and `/settings` use the FFmpeg presets in `emyfx.py`: 8D, 360°, 3D, Concert, Slowed, Vinyl, LoFi, Phonk, and Dreamy. Phonk shapes the source's bass and transients with controlled saturation; it does not generate or randomize beats. Dreamy adds filtered, spacious reflections over the original signal. Headphones are recommended for spatial effects.
- Changing a song's name, artist, cover, or effect replaces the media on the existing Telegram message. Effects always render from the untouched source audio; choosing another preset never layers effects, and the active preset's button becomes **Original** to restore the unprocessed track.
- Song search requests up to 25 YouTube results in a background thread; search results are cached in memory for five minutes, including partial result sets, so repeat searches do not refetch just because fewer than the requested maximum were available.
- The bot logs catalog search, yt-dlp download/MP3 postprocessing, effect rendering, Telegram upload request and stage duration, effective payload rate, and total request time when the corresponding phase occurs. Initial playback still depends on catalog/provider response time, file size, and Telegram's upload/processing speed; no bot can guarantee a fixed 2–3 second upload for a full-length track.
- The bot processes up to eight Telegram updates concurrently so a long download or effect render does not hold up unrelated messages and button callbacks. Effect presets that do not use source duration skip the extra FFprobe subprocess; Vinyl and LoFi retain the duration probe required to size their texture sources.
- `YTDLP_CONCURRENT_FRAGMENTS` controls yt-dlp's bounded fragment-download concurrency (default 16, maximum 16). It can help segmented media when the server/network supports parallel fragments, but does not accelerate progressive single-stream downloads or Telegram uploads. Use the transfer benchmark below to identify the actual bottleneck; VPS bandwidth alone cannot guarantee Telegram throughput.
- Telegram's Bot API returns only after it has accepted and processed an upload; it does not expose a separate server processing duration. The logged request duration therefore includes both network transfer and Telegram processing and must not be interpreted as client side upload time alone.
- YouTube's public results page supplies search results. yt-dlp handles song downloads and MP3 extraction; the player endpoint is still used by inline audio, previews, and recognition, which require direct stream URLs.
- The bot reuses its configured `HTTPXRequest` connection pool for Telegram API calls.
- To measure Telegram transfer performance independently of YouTube search and FFmpeg, set `BENCHMARK_CHAT_ID` to a chat where the bot can send messages, then run `python transfer.py`. It sends and deletes synthetic 1, 5, 10, and 15 MB documents (decimal), verifies their SHA-256 hashes, and logs upload/download times and effective throughput. It uses `TELEGRAM_LOCAL_API_URL` when configured. Timings include Bot API processing because Telegram does not expose the raw network-transfer interval.

### Telegram rich-message support
- `telegram_features.py` wraps the current Bot API rich message, rich draft, text draft, and ephemeral message methods using PTB's pooled request client. Rich-message payloads use Telegram's `InputRichMessage` schema, including its supported text entities and content blocks.
- Text and rich drafts are private-chat previews; the helper sends the completed message separately to persist it. The bot uses private-to-user ephemeral responses for `/start` and `/settings` invoked in groups, with settings callbacks edited only for their intended recipient.
- Existing song and settings keyboards use the verified custom emoji IDs supplied for Emy and only Telegram's documented `danger`, `success`, and `primary` button styles.
- On startup, the bot resolves emoji IDs from the `aicreate`, `tgiosicons`, and `tgmacicons` sticker sets via `getStickerSet` and stores the returned emoji-to-ID mappings in `bot_data["resolved_custom_emoji_packs"]`. It does not assume IDs for these packs.
- The bot display name is managed in BotFather rather than changed on every startup, avoiding repeated `setMyName` calls and Telegram flood limits. Description and short description are synced at startup. Set `BOT_PROFILE_PHOTO` to a local JPEG path to opt into uploading a profile photo with `setMyProfilePhoto`.
- Telegram rich/draft features are available through the reusable helpers; no AI or generated text handler is added because Emy has no such response path.
- The Lyrics button requests plain or synced lyrics directly from LRCLIB, validates search fallbacks against the saved track title and artist, and sends long results as bounded Telegram messages. YouTube Music is the search catalog; YouTube remains the playable-source provider.
- Lyrics lookup uses the track title alone so noisy artist, album, or duration metadata does not hide available lyrics.
- Name, artist, and cover edits update the existing audio message with the requested metadata.
- YouTube media is downloaded without browser cookies or user credentials. If a particular result requires sign in, Emy reports that the upload is unavailable and the user can select another search result.
- Song recognition uses ShazamIO against Shazam's reverse engineered, unauthenticated endpoint and does not require an API key. It is a free best effort integration, not an official or guaranteed service; Shazam can rate limit or change the endpoint. URL/upload recognition submits a 10 second sample, matching the installed ShazamIO recognizer's default fingerprint window.

### Owner console and broadcasts
- Set `BOT_OWNER_ID` to the owner's numeric Telegram user ID. Owner tools work only for that account in a private chat.
- `/owner` opens the console; `/stats` shows known private users, recent activity and growth windows, plus broadcast delivery totals. User counts begin when the bot first sees private interactions and are not historical Telegram membership counts.
- `/broadcast` opens a button-based composer. Send text or media once, then use inline buttons to preview the exact card, edit its content, create/remove styled link buttons, toggle silent/protected delivery, choose all active users or users active within 30 days, and review a recipient-count confirmation before sending.
- Format broadcast text and captions with Telegram's supported HTML tags. Insert a premium custom emoji as `{emoji_id}` (for example, `{5368324170671202286}`). The button manager guides you through the label, HTTPS URL, and `primary`, `success`, or `danger` style with inline choices. It supports up to 10 buttons across five rows (two per row); a `{emoji_id}` in a button label becomes its Telegram custom-emoji icon.
- `/broadcast_status` shows recent progress and has inline controls to resume interrupted broadcasts or request cancellation of active ones. `/broadcast_cancel <id>` and `/broadcast_resume <id>` remain available as command alternatives. Per-recipient outcomes and progress are stored in SQLite, and blocked recipients are marked unreachable. Users can `/stop` broadcasts and `/start` to opt in again.
- Owner commands are silent for other users, including when invoked outside a private chat.

## Realistic latency
Fresh playback always resolves, downloads, converts, and uploads the audio; repeats take the same path. A 320 kbps MP3 is about 2.4 MB per minute before metadata, so even on a 2.5 Gbps VPS the end-to-end time depends on YouTube's response/CDN, transcoding, Telegram's upload path and processing, and network latency. A 2–3 second end-to-end guarantee for full songs cannot be made from the VPS's link speed alone. Search results are cached in process memory for five minutes to skip repeat search-page requests; stream URLs are never cached.

## Notes
- Audio working files are temporary and deleted after processing; no downloaded music is persisted as a local cache.
- YouTube player responses can change or withhold direct stream URLs; private, restricted, or cipher-only streams may not be available.

## Requirements
- Python 3.10+ recommended.
- yt-dlp required for song downloads (`pip install -r requirements.txt`).
- ffmpeg required for MP3 conversion.

## Troubleshooting
| Error | Cause | Fix |
|-------|-------|-----|
| YouTube search is temporarily unavailable | YouTube requires consent, rate-limits the server, or changes its results page | Try again later; the bot parses the public results page and does not use a third-party search service. |
| Preview/recognition link cannot be fetched | Unsupported/private YouTube URL or no direct stream URL | Retry with a public YouTube link that exposes direct player streams. |
