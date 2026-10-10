# Telegram Song Bot (TypeTube)

Search and play songs by name, or identify music from supported public video links. Link messages receive a video preview capped at 720p; recognition starts only after the user taps **Recognize Song**.

Inline song and video-link results download directly as audio without a recognition prompt; the resulting audio has only the Playlist button.

## Commands
- `/start`, `/help`: welcome message
- In a private chat, send a song name to choose from paginated search results (ten tracks per page, two per row). Send a supported public video link to receive a preview (up to 720p); tap **Recognize Song** to identify and play the match.
- `/search <name or video link>`: use this command in groups, e.g. `/search kesariya`. Names show paginated results (ten tracks per page, two per row);
- `/play <link or video id>`: skip search and preview, download the audio straight away with your saved effect. supported public video links return a preview with a **Recognize Song** button.
- `/playlist`: list and open your private playlists.
- `/playlist create <name>`: create a playlist. When prompted from a song's Playlist button, the current song is saved into it automatically.
- `/playlist <id>`: browse and play saved songs. `/playlist delete <id>` deletes a playlist.

Playlists are available to every user in a private chat; they are not owner-only. Each sent song has a cloud-icon **Playlist** button. Save songs to an existing playlist or create a new one from that button. Inline-selected songs keep only this playlist button, without the other player controls. Playlist entries store Telegram audio file IDs, so the bot reuses Telegram's hosted audio rather than retaining another copy of the song. Playlist names and file IDs are held in the bot's SQLite database on the host and scoped to the Telegram account that saved them.

## Setup
1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
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
| `TYPETUBE_ENDPOINT` | `https://typetube.xysushi.in` | TypeTube API endpoint |
| `BOT_DATA_DIR` | OS user config directory | Private storage for bot state and TypeTube credentials |
| `TYPETUBE_KEY_FILE` | `<BOT_DATA_DIR>/typetube-api-key` | Optional TypeTube API key file override |
| `BOT_DATABASE_FILE` | `<BOT_DATA_DIR>/bot-state.sqlite3` | Optional user, playlist, and broadcast database override |

The TypeTube key is optional: songs use the public daily allowance first. Set a private fallback later with `/apikey` in the bot's private chat as the owner. Users never need to provide an API key; when the free allowance is exhausted, the owner is notified privately and users receive only a generic temporary-unavailable message.

## How it works (RAM based, no local storage)
1. **Track resolve**: YouTube Music (`ytmusicapi`, no API key needed) supplies catalog search results. TypeTube's public tier is tried first (up to 1,500 resolves per day, subject to the provider's current limits). The owner's private API key is used as a fallback only after the public tier is exhausted.
2. TypeTube downloads audio in parallel range chunks where supported. On Windows, the bot streams the resolved URL directly because the current TypeTube range downloader uses `os.pwrite`, which is unavailable there. FFmpeg converts the temporary source to the configured MP3 bitrate.
3. Temporary media is read into RAM and removed before upload.
4. Bot sends the bytes through Telegram's `InputFile`, stores the Telegram `file_id` in `cache.json`, and retains the untouched source audio for the bounded in memory effect session. Repeats resend by ID in ~300-500ms with zero download/upload. Per-user effect settings are stored in `user.json`; existing `songs_cache.json` and `user_settings.json` files are renamed to the new names on startup when those do not yet exist.
- Song audio is temporary; the TypeTube key and owner/broadcast state are stored outside the project by default. The normal Telegram file-ID cache remains a small JSON file.

### Audio effects and transfer timings
- The Effect menu and `/settings` use the FFmpeg presets in `emyfx.py`: 8D, 360°, 3D, Concert, Slowed, Vinyl, and LoFi. 360° adds a slower stereo orbit; 3D adds soft depth and short reflections while keeping bass centred. Headphones are recommended for spatial effects.
- Changing a song's name, artist, cover, or effect replaces the media on the existing Telegram message. Effects always render from the untouched source audio; choosing another preset never layers effects, and the active preset's button becomes **Original** to restore the unprocessed track.
- Song search is a single YouTube Music API call (~0.5 to 1s) returning rich music metadata with no JS challenge and no per result lookups. Results are cached in memory for five minutes; cached searches are immediate.
- The bot logs source resolution, source download, FFmpeg extraction/transcoding, effect rendering, Telegram upload request and stage duration, effective payload rate, and total request time when the corresponding phase occurs.
- Telegram's Bot API returns only after it has accepted and processed an upload; it does not expose a separate server processing duration. The logged request duration therefore includes both network transfer and Telegram processing and must not be interpreted as client side upload time alone.
- Song audio resolution/download uses `typetube-py`. `yt-dlp` remains only for the existing cross-platform video-preview and URL-recognition helpers; TypeTube provides audio stream resolution/download, not those multi-platform video extraction features.
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
- `/apikey` prompts for the TypeTube API key privately, verifies it with TypeTube before saving it outside the project directory, and attempts to delete the submitted key message. `/apikey remove` deletes it. Never send the key in a group.
- `/broadcast` opens a button-based composer. Send text or media once, then use inline buttons to preview the exact card, edit its content, create/remove styled link buttons, toggle silent/protected delivery, choose all active users or users active within 30 days, and review a recipient-count confirmation before sending.
- Format broadcast text and captions with Telegram's supported HTML tags. Insert a premium custom emoji as `{emoji_id}` (for example, `{5368324170671202286}`). The button manager guides you through the label, HTTPS URL, and `primary`, `success`, or `danger` style with inline choices. It supports up to 10 buttons across five rows (two per row); a `{emoji_id}` in a button label becomes its Telegram custom-emoji icon.
- `/broadcast_status` shows recent progress and has inline controls to resume interrupted broadcasts or request cancellation of active ones. `/broadcast_cancel <id>` and `/broadcast_resume <id>` remain available as command alternatives. Per-recipient outcomes and progress are stored in SQLite, and blocked recipients are marked unreachable. Users can `/stop` broadcasts and `/start` to opt in again.
- Owner commands are silent for other users, including when invoked outside a private chat.

## Realistic latency (measured on this machine, 236 KiB/s link)
| Step | Fresh song | Repeat (cached) |
|------|-----------|-----------------|
| Search + pick official | ~1 to 2s | 0 (skipped) |
| YouTube download (320k ≈ 2.4 MB/min) | ~10 to 40s | 0 (skipped) |
| ffmpeg → 320k MP3 | ~1 to 3s | 0 (skipped) |
| Telegram upload | ~10 to 40s | ~0.3 to 0.5s (file_id resend) |
| **Total** | **~25 to 80s** | **~300 to 500ms** |
Sub 500ms fresh downloads are physically impossible (multi MB transfers); cache hits are the only sub second path.

## Notes
- Files are deleted after sending (RAM based, nothing persists on disk).
- Keep `yt-dlp` fresh for the remaining video-preview and URL-recognition paths: `pip install -U yt-dlp`.

## Requirements
- Python 3.10+ recommended; TypeTube itself supports Python 3.9+.
- Node.js may be needed by yt-dlp for some video-preview or recognition URLs; it is not used for song audio resolution/download.
- ffmpeg required for MP3 conversion.

## Troubleshooting
| Error | Cause | Fix |
|-------|-------|-----|
| Song downloads are temporarily unavailable | The public daily allowance is exhausted and the private fallback key is missing, invalid, or out of quota | The owner can privately add or update the fallback with `/apikey`; users do not need to send credentials. |
| Preview/recognition link cannot be fetched | Unsupported/private URL or yt-dlp platform change | Retry with a public supported link and keep yt-dlp current. |
