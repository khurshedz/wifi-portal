"""Резервная копия базы портала в самом Telegram.

Зачем: на бесплатном Render база лежит в /tmp и стирается, когда сервер засыпает или перезапускается.
Вместе с ней пропадали номера тем («📊 Посетители», «👥 Общий чат», темы гостей), и бот заводил их заново.

Как устроено:
  * в группе есть одно сообщение с файлом portal-backup.json.gz; при изменениях файл в нём заменяется;
  * номер этого сообщения записан в описании группы (метка backup_msg=N) — это единственное,
    что бот может прочитать после холодного старта, не зная ничего;
  * при старте с пустой базой бот пересылает это сообщение самому себе, чтобы узнать файл, и скачивает его.
"""
import gzip
import hashlib
import json
import re
import time

import httpx

TAG_RE = re.compile(r"backup_msg=(\d+)")
FILE_NAME = "portal-backup.json.gz"
MAX_BLOB = 18_000_000  # бот может скачать файл не больше 20 МБ


class NoBackup(Exception):
    """Копии нет (или сообщение с ней удалили) — это не сбой, начинать с пустой базы нормально."""


def pack(state: dict) -> tuple[bytes, str]:
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return gzip.compress(raw, mtime=0), hashlib.sha256(raw).hexdigest()


def unpack(blob: bytes) -> dict:
    d = json.loads(gzip.decompress(blob))
    if not isinstance(d, dict):
        raise ValueError("backup: not an object")
    return d


def caption() -> str:
    t = time.strftime("%d.%m %H:%M", time.gmtime(time.time() + 5 * 3600))  # время Душанбе
    return f"🗄 Резервная копия базы портала, обновлена {t}.\nОбновляется сама. Не удаляйте это сообщение."


class TgBackup:
    def __init__(self, http: httpx.AsyncClient, token: str, chat_id: int):
        self.http, self.chat = http, chat_id
        self.api = f"https://api.telegram.org/bot{token}"
        self.file_api = f"https://api.telegram.org/file/bot{token}"
        self.msg: int | None = None   # сообщение с файлом
        self.last: str | None = None  # отпечаток последнего сохранённого состояния

    async def call(self, method: str, files=None, **params):
        if files:
            r = await self.http.post(f"{self.api}/{method}", data={k: str(v) for k, v in params.items()},
                                     files=files, timeout=60)
        else:
            r = await self.http.post(f"{self.api}/{method}", json=params, timeout=40)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    async def _description(self) -> str:
        return (await self.call("getChat", chat_id=self.chat)).get("description") or ""

    async def pointer(self) -> int | None:
        m = TAG_RE.search(await self._description())
        return int(m.group(1)) if m else None

    async def _set_pointer(self, mid: int) -> None:
        tag = f"Служебная метка бота: backup_msg={mid}"
        rest = TAG_RE.sub("", await self._description()).replace("Служебная метка бота:", "").strip()
        rest = rest[:255 - len(tag) - 1].rstrip()  # укорачиваем текст владельца, а не метку
        try:
            await self.call("setChatDescription", chat_id=self.chat, description=(rest + "\n" if rest else "") + tag)
        except RuntimeError as e:
            if "not modified" not in str(e).lower():
                raise

    async def load(self) -> dict:
        """Состояние из копии. NoBackup — копии нет; любая другая ошибка — сбой, стоит повторить."""
        mid = await self.pointer()
        if not mid:
            raise NoBackup("no pointer")
        try:
            fwd = await self.call("forwardMessage", chat_id=self.chat, from_chat_id=self.chat,
                                  message_id=mid, disable_notification=True)
        except RuntimeError as e:
            if "not found" in str(e).lower():
                raise NoBackup("backup message deleted") from None
            raise
        try:
            await self.call("deleteMessage", chat_id=self.chat, message_id=fwd["message_id"])
        except Exception as e:
            print("backup: could not delete the forwarded copy:", e)
        doc = fwd.get("document")
        if not doc:
            raise NoBackup("pointer leads to a message without a file")
        f = await self.call("getFile", file_id=doc["file_id"])
        r = await self.http.get(f"{self.file_api}/{f['file_path']}", timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"backup download: HTTP {r.status_code}")
        try:
            state = unpack(r.content)
        except (OSError, EOFError, ValueError) as e:  # файл испорчен — читать нечего, это не сбой сети
            raise NoBackup(f"backup file is damaged: {type(e).__name__}") from None
        self.msg = mid
        return state

    async def save(self, state: dict) -> bool:
        """Сохраняет состояние, если оно изменилось. True — файл отправлен."""
        blob, digest = pack(state)
        if digest == self.last:
            return False
        if len(blob) > MAX_BLOB:
            raise RuntimeError(f"backup too big: {len(blob)} bytes")
        files = {"f": (FILE_NAME, blob, "application/gzip")}
        self.msg = await self.pointer()  # всегда пишем в то сообщение, на которое указывает описание группы
        if self.msg:
            try:
                media = json.dumps({"type": "document", "media": "attach://f", "caption": caption()})
                await self.call("editMessageMedia", files=files, chat_id=self.chat, message_id=self.msg, media=media)
                self.last = digest
                return True
            except RuntimeError as e:
                print("backup: edit failed, sending a new message:", e)
        m = await self.call("sendDocument", files={"document": files["f"]}, chat_id=self.chat,
                            caption=caption(), disable_notification="true")
        try:
            await self._set_pointer(m["message_id"])
        except Exception:
            # описание не обновилось — новая копия бесполезна (после сна её не найдут): убрать и повторить позже
            try:
                await self.call("deleteMessage", chat_id=self.chat, message_id=m["message_id"])
            except Exception as e:
                print("backup: could not delete the unreferenced copy:", e)
            raise
        self.msg = m["message_id"]
        self.last = digest
        return True
