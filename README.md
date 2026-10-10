# Telegram Song Bot

Search and play songs by name, or identify music from public YouTube links. YouTube links receive a direct-stream video preview capped at 720p; recognition starts only after the user taps **Recognize Song**. Other media platforms are not supported.

Selecting a search result starts a fresh YouTube stream resolution and audio download. YouTube links sent as messages keep the separate video-preview and **Recognize Song** flow.

Telegram inline mode (`@botname song`) searches YouTube and resolves up to five fresh, direct MP4 audio URLs concurrently. Telegram fetches the selected source directly, so inline playback skips the bot's download, 320 kbps MP3 conversion, and upload; source bitrate is whatever YouTube provides, and playback depends on Telegram being able to fetch the signed URL before it expires. Normal `/play` and selected search results continue to use the full MP3 pipeline.

## Commands
- `/start`, `/help`: welcome message
- In a private chat, send a song name to choose from paginated search results (ten tracks per page, two per row). Send a supported public video link to receive a preview (up to 720p); tap **Recognize Song** to identify and play the match.
- `/search <name or video link>`: use this command in groups, e.g. `/search kesariya`. Names show paginated results (ten tracks per page, two per row);
- `/play <link or video id>`: skip search and preview, download the audio straight away with your saved effect. supported public video links return a preview with a **Recognize Song** button.
- `/playlist`: list and open your private playlists.
- `/playlist create <name>`: create a playlist. When prompted from a song's Playlist button, the current song is saved into it automatically.
- `/playlist <id>`: browse and play saved songs. `/playlist delete <id>` deletes a playlist.

Playlists are available to every user in a private chat; they are not owner-only. Each sent song has a cloud-icon **Playlist** button. Save songs to an existing playlist or create a new one from that button. Playlist entries store the YouTube video ID and metadata; selecting a playlist item fetches a fresh stream URL and uploads a new audio file.

## Setup
1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token, and enable inline mode with `/setinline` if you want `@botname song` results.
2. Install ffmpeg (already required for mp3 conversion, verified present on this machine).
3. Install deps:
   ```powershell
   pip install -r requirements.txt
   ```
4. Configure:
   ```powershell
   Copy-Item .env.example .env
   # set BOT_TOKEN and your numeric Telegram BOT_OWNER_ID
   ```
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
| `BOT_DATA_DIR` | OS user config directory | Private storage for bot state |
| `BOT_DATABASE_FILE` | `<BOT_DATA_DIR>/bot-state.sqlite3` | Optional user, playlist, and broadcast database override |

## How it works (temporary media, no persistent audio cache)
1. **Track search**: Search results are parsed from YouTube's public `/results` page, in relevance order. Results are cached in memory for five minutes; a consent page, changed markup, or rate limit is reported as a search error.
2. The YouTube player endpoint resolves direct stream URLs, retrying supported web, YouTube Music, and Android clients when a client cannot play the video. Audio downloads use parallel 4 MiB HTTP range requests (up to 16 workers) when supported, with a bounded single-request fallback; this is platform-independent and does not use yt-dlp. Audio is encoded to MP3 at `MP3_QUALITY` (320 kbps by default) by FFmpeg.
3. Audio is held in temporary working files during download/conversion, read into RAM for upload, and the temporary directory is removed at the end of processing.
4. Bot sends the bytes through Telegram's `InputFile` and retains the untouched source audio only in a bounded in-memory effect session. Per-user effect settings are stored in `user.json`; owner/broadcast state is stored outside the project by default.
- No local song or Telegram `file_id` playback cache is read or written. The bot keeps search-result metadata in memory for five minutes, but resolves a fresh player response and stream URL for every playback. Download and conversion work files are temporary and removed when processing finishes.
- Telegram inline mode resolves up to five direct audio-only MP4 streams for text searches, YouTube links, and 11-character video IDs. Telegram fetches the signed YouTube URL itself rather than sending the audio through the bot; the available quality is whatever YouTube exposes, and a signed URL may expire or be inaccessible to Telegram. Enable inline mode with BotFather's `/setinline`. Inline answers are not cached, and YouTube Music links use the Music player client.

### Audio effects and transfer timings
- The Effect menu and `/settings` use the FFmpeg presets in `emyfx.py`: 8D, 360°, 3D, Concert, Slowed, Vinyl, and LoFi. 360° adds a slower stereo orbit; 3D adds soft depth and short reflections while keeping bass centred. Headphones are recommended for spatial effects.
- Changing a song's name, artist, cover, or effect replaces the media on the existing Telegram message. Effects always render from the untouched source audio; choosing another preset never layers effects, and the active preset's button becomes **Original** to restore the unprocessed track.
- Song search requests up to 25 YouTube results in a background thread; search results are cached in memory for five minutes, including partial result sets, so repeat searches do not refetch just because fewer than the requested maximum were available.
- The bot logs source resolution, source download, FFmpeg transcoding, effect rendering, Telegram upload request and stage duration, effective payload rate, and total request time when the corresponding phase occurs. Initial playback still depends on catalog/provider response time, file size, and Telegram's upload/processing speed; no bot can guarantee a fixed 2–3 second upload for a full-length track.
- Telegram's Bot API returns only after it has accepted and processed an upload; it does not expose a separate server processing duration. The logged request duration therefore includes both network transfer and Telegram processing and must not be interpreted as client side upload time alone.
- YouTube's public results page supplies search results, and its player endpoint supplies stream metadata and direct URLs for audio resolution, download, and video previews. Streams without direct URLs (including cipher-only responses) are not supported by the resolver.
- The bot reuses its configured `HTTPXRequest` connection pool for Telegram API calls.
- To measure Telegram transfer performance independently of YouTube search and FFmpeg, set `BENCHMARK_CHAT_ID` to a chat where the bot can send messages, then run `python benchmark_transfers.py`. It sends and deletes synthetic 1, 5, 10, and 15 MB documents (decimal), verifies their SHA-256 hashes, and logs upload/download times and effective throughput. It does not report server-processing time separately because Telegram does not expose that measurement.

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
- ffmpeg required for MP3 conversion.

## Troubleshooting
| Error | Cause | Fix |
|-------|-------|-----|
| YouTube search is temporarily unavailable | YouTube requires consent, rate-limits the server, or changes its results page | Try again later; the bot parses the public results page and does not use a third-party search service. |
| Preview/recognition link cannot be fetched | Unsupported/private YouTube URL or no direct stream URL | Retry with a public YouTube link that exposes direct player streams. |
