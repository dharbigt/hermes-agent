"""Keybase platform adapter.

Connects to the local `keybase` CLI/service, which must already be
installed, running, and logged in on this machine (`keybase login`).

Inbound messages arrive via `keybase chat api-listen -j`, a long-running
subprocess that streams one JSON object per line for every new message
the logged-in user can see (DMs, team channels, etc.).

Outbound messages and actions use `keybase chat api -m '<json>'`, a
one-shot subprocess call that takes a JSON-RPC-ish payload on stdin/argv
and returns a JSON result on stdout. There is no persistent outbound
connection to manage — every send is its own subprocess invocation.

This mirrors gateway/platforms/signal.py's shape (HTTP+SSE daemon there
vs. local subprocess+JSONL here) so it drops into the same
BasePlatformAdapter contract.

Requires:
- The `keybase` binary installed and on PATH.
- `keybase login` already completed for the account Hermes should use
  (this adapter does not handle interactive login/provisioning).
- Optionally KEYBASE_ALLOWED_TEAMS / KEYBASE_GROUP_ALLOWED_USERS env
  vars, mirroring the Signal adapter's group-allowlist pattern.
"""

import asyncio
import json
import logging
import os
import random
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_document_from_bytes,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KEYBASE_MAX_ATTACHMENT_SIZE = 100 * 1024 * 1024  # 100 MB, mirrors Signal's cap
MAX_MESSAGE_LENGTH = 100_000  # Keybase messages are effectively unbounded; be generous
LISTENER_RETRY_DELAY_INITIAL = 2.0
LISTENER_RETRY_DELAY_MAX = 60.0
HEALTH_CHECK_INTERVAL = 30.0
HEALTH_CHECK_STALE_THRESHOLD = 120.0
RPC_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_comma_list(value: str) -> List[str]:
    """Split a comma-separated string into a list, stripping whitespace."""
    return [v.strip() for v in value.split(",") if v.strip()]


def _redact_username(name: str) -> str:
    """Avoid dumping full Keybase usernames into logs at INFO level."""
    if not name:
        return name
    if len(name) <= 3:
        return "*" * len(name)
    return name[:2] + "*" * (len(name) - 2)


def _guess_extension(mime_type: str, filename: str = "") -> str:
    """Best-effort extension from a MIME type, falling back to filename."""
    mime_to_ext = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
        "audio/wav": ".wav", "audio/mp4": ".m4a", "video/mp4": ".mp4",
        "application/pdf": ".pdf", "application/zip": ".zip",
    }
    if mime_type in mime_to_ext:
        return mime_to_ext[mime_type]
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[-1].lower()
    return ".bin"


def _is_image_mime(mime_type: str) -> bool:
    return (mime_type or "").startswith("image/")


def _is_audio_mime(mime_type: str) -> bool:
    return (mime_type or "").startswith("audio/")


def check_keybase_requirements() -> bool:
    """Check if the `keybase` binary is available on PATH."""
    return shutil.which("keybase") is not None


# ---------------------------------------------------------------------------
# Keybase Adapter
# ---------------------------------------------------------------------------

class KeybaseAdapter(BasePlatformAdapter):
    """Keybase adapter using the local `keybase` CLI's chat API.

    Unlike Signal (HTTP daemon) this talks to a *local* logged-in
    `keybase` service via subprocess, so there's no host/port/account
    config beyond optional allowlists — the identity is whatever
    `keybase login` is currently active on the machine.
    """

    platform = Platform.KEYBASE

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.KEYBASE)
        extra = config.extra or {}

        self.keybase_bin = extra.get("keybase_bin") or shutil.which("keybase") or "keybase"

        # Team/channel allowlist, same shape as Signal's group allowlist:
        # unset -> team channels disabled; "*" -> all allowed; else explicit list.
        team_allowed_str = os.getenv("KEYBASE_ALLOWED_TEAMS", "")
        self.team_allow_from = set(_parse_comma_list(team_allowed_str))

        # Background process/tasks
        self._listen_proc: Optional[asyncio.subprocess.Process] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._typing_tasks: Dict[str, asyncio.Task] = {}

        self._running = False
        self._last_activity = 0.0

        # Echo-back suppression for our own sent messages, same pattern
        # as Signal's _recent_sent_timestamps.
        self._recent_sent_ids: set = set()
        self._max_recent_sent_ids = 50

        self._self_username: Optional[str] = None

        logger.info(
            "Keybase adapter initialized: bin=%s teams=%s",
            self.keybase_bin,
            "enabled" if self.team_allow_from else "disabled",
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Verify `keybase` is logged in, then start the streaming listener."""
        if not shutil.which(self.keybase_bin) and not Path(self.keybase_bin).exists():
            logger.error("Keybase: binary not found (%s). Install Keybase and ensure it's on PATH.",
                         self.keybase_bin)
            return False

        lock_acquired = False
        try:
            if not self._acquire_platform_lock('keybase-account', 'default', 'Keybase account'):
                return False
            lock_acquired = True
        except Exception as e:
            logger.warning("Keybase: could not acquire account lock (non-fatal): %s", e)

        # Verify login status via `keybase status -j`
        try:
            status = await self._run_cli(["status", "-j"])
            if not status:
                logger.error("Keybase: could not read `keybase status` — is the service running?")
                if lock_acquired:
                    self._release_platform_lock()
                return False
            parsed = json.loads(status)
            logged_in = parsed.get("LoggedIn", False)
            username = (parsed.get("User") or {}).get("Name")
            if not logged_in or not username:
                logger.error("Keybase: not logged in. Run `keybase login` for the account Hermes should use.")
                if lock_acquired:
                    self._release_platform_lock()
                return False
            self._self_username = username
        except Exception as e:
            logger.error("Keybase: failed to check login status: %s", e)
            if lock_acquired:
                self._release_platform_lock()
            return False

        self._running = True
        self._last_activity = time.time()
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._health_monitor_task = asyncio.create_task(self._health_monitor())

        logger.info("Keybase: connected as %s", _redact_username(self._self_username))
        return True

    async def disconnect(self) -> None:
        """Stop the listener subprocess and clean up."""
        self._running = False

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

        if self._health_monitor_task:
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass

        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()

        await self._terminate_listen_proc()
        self._release_platform_lock()
        logger.info("Keybase: disconnected")

    async def _terminate_listen_proc(self) -> None:
        proc = self._listen_proc
        self._listen_proc = None
        if not proc:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception("Keybase: error terminating api-listen process")

    # ------------------------------------------------------------------
    # Streaming (inbound messages via `keybase chat api-listen -j`)
    # ------------------------------------------------------------------

    async def _listen_loop(self) -> None:
        """Spawn `keybase chat api-listen -j` and process one JSON obj per line."""
        backoff = LISTENER_RETRY_DELAY_INITIAL

        while self._running:
            try:
                logger.debug("Keybase: starting api-listen subprocess")
                self._listen_proc = await asyncio.create_subprocess_exec(
                    self.keybase_bin, "chat", "api-listen", "-j",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                backoff = LISTENER_RETRY_DELAY_INITIAL
                self._last_activity = time.time()
                logger.info("Keybase: api-listen connected")

                assert self._listen_proc.stdout is not None
                async for raw_line in self._listen_proc.stdout:
                    if not self._running:
                        break
                    self._last_activity = time.time()
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        await self._handle_notification(obj)
                    except json.JSONDecodeError:
                        logger.debug("Keybase: non-JSON line from api-listen: %s", line[:100])
                    except Exception:
                        logger.exception("Keybase: error handling notification")

                # stdout closed — process likely exited
                rc = await self._listen_proc.wait()
                if self._running:
                    logger.warning("Keybase: api-listen exited (code %s), reconnecting", rc)

            except asyncio.CancelledError:
                break
            except FileNotFoundError:
                logger.error("Keybase: `%s` binary not found; cannot start api-listen", self.keybase_bin)
                break
            except Exception as e:
                if self._running:
                    logger.warning("Keybase: api-listen error: %s (reconnecting in %.0fs)", e, backoff)
            finally:
                await self._terminate_listen_proc()

            if self._running:
                jitter = backoff * 0.2 * random.random()
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, LISTENER_RETRY_DELAY_MAX)

    # ------------------------------------------------------------------
    # Health Monitor
    # ------------------------------------------------------------------

    async def _health_monitor(self) -> None:
        """Force a listener restart if no notifications for too long AND
        the daemon itself looks unresponsive (avoids restarting a listener
        that's simply idle because no one has messaged in a while)."""
        while self._running:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            if not self._running:
                break
            elapsed = time.time() - self._last_activity
            if elapsed > HEALTH_CHECK_STALE_THRESHOLD:
                status = await self._run_cli(["status", "-j"], log_failures=False)
                if status is None:
                    logger.warning("Keybase: service unresponsive, forcing listener restart")
                    await self._terminate_listen_proc()
                else:
                    # Service is fine, just quiet — don't spam restarts.
                    self._last_activity = time.time()

    # ------------------------------------------------------------------
    # Notification Handling
    # ------------------------------------------------------------------

    async def _handle_notification(self, obj: dict) -> None:
        """Process one JSON object emitted by `keybase chat api-listen`."""
        msg = obj.get("msg")
        if not isinstance(msg, dict):
            return

        content = msg.get("content", {})
        msg_type = content.get("type")
        # Only handle plain text and attachment messages; ignore
        # edits/deletes/reactions/system messages for now.
        if msg_type not in ("text", "attachment"):
            return

        channel = msg.get("channel", {})
        is_team = bool(channel.get("name")) and channel.get("members_type") == "team"
        team_name = channel.get("name") if is_team else None
        topic_name = channel.get("topic_name") if is_team else None

        sender = msg.get("sender", {})
        sender_username = sender.get("username", "")

        # Self-echo suppression
        msg_id = msg.get("id")
        if msg_id in self._recent_sent_ids:
            self._recent_sent_ids.discard(msg_id)
            return
        if self._self_username and sender_username == self._self_username:
            return

        # Team/channel allowlist (mirrors Signal's group policy)
        if is_team:
            if not self.team_allow_from:
                logger.debug("Keybase: ignoring team message (no KEYBASE_ALLOWED_TEAMS)")
                return
            channel_key = f"{team_name}#{topic_name}" if topic_name else team_name
            if "*" not in self.team_allow_from and channel_key not in self.team_allow_from and team_name not in self.team_allow_from:
                logger.debug("Keybase: team channel %s not in allowlist", channel_key)
                return

        # Chat identifiers: DMs use the conversation id; teams use
        # "team:<name>#<topic>" so send() can route appropriately.
        conv_id = msg.get("conversation_id", "")
        if is_team:
            chat_id = f"team:{team_name}#{topic_name or 'general'}"
            chat_type = "group"
            chat_name = f"{team_name}#{topic_name}" if topic_name else team_name
        else:
            chat_id = conv_id
            chat_type = "dm"
            chat_name = sender_username

        text = ""
        media_urls: List[str] = []
        media_types: List[str] = []
        result_msg_type = MessageType.TEXT

        if msg_type == "text":
            text = content.get("text", {}).get("body", "")
        elif msg_type == "attachment":
            att = content.get("attachment", {})
            text = att.get("caption", "") or ""
            object_info = att.get("object", {})
            mime_type = object_info.get("mimeType", "")
            filename = object_info.get("filename", "")
            try:
                cached_path = await self._download_attachment(conv_id, msg_id, mime_type, filename)
                if cached_path:
                    media_urls.append(cached_path)
                    media_types.append(mime_type or "application/octet-stream")
                    if _is_image_mime(mime_type):
                        result_msg_type = MessageType.PHOTO
                    elif _is_audio_mime(mime_type):
                        result_msg_type = MessageType.VOICE
            except Exception:
                logger.exception("Keybase: failed to download attachment for msg %s", msg_id)

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=sender_username,
            user_name=sender_username,
        )

        ctime = msg.get("sent_at")  # seconds since epoch
        try:
            timestamp = datetime.fromtimestamp(ctime, tz=timezone.utc) if ctime else datetime.now(tz=timezone.utc)
        except (ValueError, OSError, TypeError):
            timestamp = datetime.now(tz=timezone.utc)

        event = MessageEvent(
            source=source,
            text=text or "",
            message_type=result_msg_type,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=timestamp,
        )

        logger.debug("Keybase: message from %s in %s: %s",
                     _redact_username(sender_username), chat_id[:30], (text or "")[:50])
        await self.handle_message(event)

    async def _download_attachment(
        self, conv_id: str, msg_id: Any, mime_type: str, filename: str
    ) -> Optional[str]:
        """Download an attachment to a temp file via `keybase chat download`,
        then re-cache it through the standard media cache helpers."""
        if not conv_id or msg_id is None:
            return None

        tmp_dir = Path(os.getenv("HERMES_TMP_DIR", "/tmp")) / "keybase_attachments"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ext = _guess_extension(mime_type, filename)
        tmp_path = tmp_dir / f"kb_{conv_id[:8]}_{msg_id}{ext}"

        result = await self._run_cli([
            "chat", "download", str(conv_id), str(msg_id),
            "--out", str(tmp_path),
        ], top_level=True)
        if result is None or not tmp_path.exists():
            return None

        try:
            raw_data = tmp_path.read_bytes()
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

        if len(raw_data) > KEYBASE_MAX_ATTACHMENT_SIZE:
            logger.warning("Keybase: attachment too large (%d bytes), skipping", len(raw_data))
            return None

        if _is_image_mime(mime_type):
            return cache_image_from_bytes(raw_data, ext)
        if _is_audio_mime(mime_type):
            return cache_audio_from_bytes(raw_data, ext)
        return cache_document_from_bytes(raw_data, ext)

    # ------------------------------------------------------------------
    # CLI / "RPC" Communication
    # ------------------------------------------------------------------

    async def _run_cli(
        self, args: List[str], *, top_level: bool = False, log_failures: bool = True
    ) -> Optional[str]:
        """Run `keybase <args>` and return stdout, or None on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.keybase_bin, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=RPC_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                if log_failures:
                    logger.warning("Keybase: CLI call timed out: %s", args)
                return None
            if proc.returncode != 0:
                if log_failures:
                    logger.warning("Keybase: CLI call failed (%s): %s",
                                    args, stderr.decode("utf-8", errors="replace")[:300])
                return None
            return stdout.decode("utf-8", errors="replace")
        except Exception as e:
            if log_failures:
                logger.warning("Keybase: CLI call errored: %s: %s", args, e)
            return None

    async def _chat_api(self, method: str, params: dict, *, log_failures: bool = True) -> Any:
        """Send a request through `keybase chat api -m '<json>'`."""
        payload = {"method": method, "params": {"options": params}}
        payload_str = json.dumps(payload)
        raw = await self._run_cli(["chat", "api", "-m", payload_str], log_failures=log_failures)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            if log_failures:
                logger.warning("Keybase: chat api returned non-JSON: %s", raw[:200])
            return None
        if isinstance(data, dict) and data.get("error"):
            if log_failures:
                logger.warning("Keybase: chat api error (%s): %s", method, data["error"])
            return None
        return data.get("result") if isinstance(data, dict) else data

    # ------------------------------------------------------------------
    # Channel resolution
    # ------------------------------------------------------------------

    def _channel_params(self, chat_id: str) -> dict:
        """Build the `channel` object `keybase chat api` expects."""
        if chat_id.startswith("team:"):
            rest = chat_id[len("team:"):]
            if "#" in rest:
                team_name, topic_name = rest.split("#", 1)
            else:
                team_name, topic_name = rest, "general"
            return {
                "name": team_name,
                "topic_name": topic_name,
                "members_type": "team",
            }
        # Direct message: chat_id is either a conversation_id or a
        # bare username to open/continue a 1:1 conversation with.
        if len(chat_id) >= 32 and all(c in "0123456789abcdef" for c in chat_id.lower()):
            return {"conversation_id": chat_id}
        return {"name": chat_id, "members_type": "impteam"}

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message."""
        await self._stop_typing_indicator(chat_id)

        params = {
            "channel": self._channel_params(chat_id),
            "message": {"body": content},
        }
        result = await self._chat_api("send", params)
        if result is not None:
            msg_id = result.get("message", {}).get("id") if isinstance(result, dict) else None
            if msg_id is not None:
                self._track_sent_id(msg_id)
            return SendResult(success=True, message_id=str(msg_id) if msg_id is not None else None)
        return SendResult(success=False, error="chat api send failed")

    def _track_sent_id(self, msg_id: Any) -> None:
        self._recent_sent_ids.add(msg_id)
        if len(self._recent_sent_ids) > self._max_recent_sent_ids:
            self._recent_sent_ids.pop()

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Keybase doesn't expose a typing-indicator RPC in the local API,
        so this is a deliberate no-op — kept as a method (rather than
        omitted) so base.py's `_keep_typing` refresh loop has something
        safe to call without special-casing this adapter."""
        return None

    async def _send_attachment(
        self,
        chat_id: str,
        file_path: str,
        media_label: str,
        caption: Optional[str] = None,
    ) -> SendResult:
        """Send any local file as a Keybase attachment via `chat api attach`."""
        await self._stop_typing_indicator(chat_id)

        try:
            file_size = Path(file_path).stat().st_size
        except FileNotFoundError:
            return SendResult(success=False, error=f"{media_label} file not found: {file_path}")
        if file_size > KEYBASE_MAX_ATTACHMENT_SIZE:
            return SendResult(success=False, error=f"{media_label} too large ({file_size} bytes)")

        params = {
            "channel": self._channel_params(chat_id),
            "filename": str(file_path),
            "title": caption or "",
        }
        result = await self._chat_api("attach", params)
        if result is not None:
            msg_id = result.get("message", {}).get("id") if isinstance(result, dict) else None
            if msg_id is not None:
                self._track_sent_id(msg_id)
            return SendResult(success=True)
        return SendResult(success=False, error=f"chat api attach ({media_label.lower()}) failed")

    async def send_image(
        self, chat_id: str, image_url: str, caption: Optional[str] = None, **kwargs,
    ) -> SendResult:
        if image_url.startswith("file://"):
            from urllib.parse import unquote
            return await self._send_attachment(chat_id, unquote(image_url[7:]), "Image", caption)
        return SendResult(success=False, error="Keybase adapter requires a local file path for images")

    async def send_document(
        self, chat_id: str, file_path: str, caption: Optional[str] = None,
        filename: Optional[str] = None, **kwargs,
    ) -> SendResult:
        return await self._send_attachment(chat_id, file_path, "File", caption)

    async def send_image_file(
        self, chat_id: str, image_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, **kwargs,
    ) -> SendResult:
        return await self._send_attachment(chat_id, image_path, "Image", caption)

    async def send_voice(
        self, chat_id: str, audio_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, **kwargs,
    ) -> SendResult:
        return await self._send_attachment(chat_id, audio_path, "Audio", caption)

    async def send_video(
        self, chat_id: str, video_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, **kwargs,
    ) -> SendResult:
        return await self._send_attachment(chat_id, video_path, "Video", caption)

    # ------------------------------------------------------------------
    # Typing Indicators (no-op, see send_typing above)
    # ------------------------------------------------------------------

    async def _stop_typing_indicator(self, chat_id: str) -> None:
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def stop_typing(self, chat_id: str) -> None:
        """Public interface called by base adapter's `_keep_typing` finally block."""
        await self._stop_typing_indicator(chat_id)

    # ------------------------------------------------------------------
    # Chat Info
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        if chat_id.startswith("team:"):
            return {"name": chat_id[len("team:"):], "type": "group", "chat_id": chat_id}
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}
