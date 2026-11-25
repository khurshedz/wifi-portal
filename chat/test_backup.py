"""Резервная копия базы в Telegram: сон сервера не должен плодить новые темы.
Запуск: TG_BOT_TOKEN=1:x TG_CHAT_ID=-1 python -m pytest test_backup.py -q (из папки chat). В сеть не ходит."""
import asyncio
import json
import os
import tempfile

os.environ.setdefault("TG_BOT_TOKEN", "1:x")
os.environ.setdefault("TG_CHAT_ID", "-1")
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["TG_BACKUP"] = "0"

import pytest  # noqa: E402

import app  # noqa: E402
import backup  # noqa: E402


class Resp:
    def __init__(self, data=None, content=b"", status=200):
        self._d, self.content, self.status_code = data, content, status

    def json(self):
        return self._d


class FakeTg:
    """Группа Telegram в памяти: описание, сообщения с файлами, темы."""

    def __init__(self):
        self.desc, self.msgs, self.files, self.next_id = "", {}, {}, 100
        self.calls, self.topics, self.sent, self.dead_threads = [], [], [], set()
        self.edits, self.loud = [], []

    def _new(self):
        self.next_id += 1
        return self.next_id

    def _store(self, files, key):
        fid = f"file{self._new()}"
        self.files[fid] = files[key][1]
        return fid

    async def post(self, url, json=None, data=None, files=None, timeout=None):
        m, p = url.rsplit("/", 1)[1], (json or data or {})
        self.calls.append(m)
        ok = lambda r: Resp({"ok": True, "result": r})  # noqa: E731
        err = lambda t: Resp({"ok": False, "description": "Bad Request: " + t})  # noqa: E731
        if m == "getChat":
            return ok({"id": -1, "description": self.desc} if self.desc else {"id": -1})
        if m == "setChatDescription":
            self.desc = p["description"]
            return ok(True)
        if m == "sendDocument":
            mid = self._new()
            self.msgs[mid] = self._store(files, "document")
            return ok({"message_id": mid})
        if m == "editMessageMedia":
            mid = int(p["message_id"])
            if mid not in self.msgs:
                return err("message to edit not found")
            self.msgs[mid] = self._store(files, "f")
            return ok({"message_id": mid})
        if m == "forwardMessage":
            mid = int(p["message_id"])
            if mid not in self.msgs:
                return err("message to forward not found")
            new = self._new()
            self.msgs[new] = self.msgs[mid]
            return ok({"message_id": new, "document": {"file_id": self.msgs[mid]}})
        if m == "deleteMessage":
            self.msgs.pop(int(p["message_id"]), None)
            return ok(True)
        if m == "getFile":
            return ok({"file_path": "documents/" + p["file_id"]})
        if m in ("editForumTopic", "closeForumTopic"):
            self.edits.append((m, p.get("message_thread_id"), p.get("name")))
            return ok(True)
        if m == "createForumTopic":
            tid = self._new()
            self.topics.append((tid, p["name"]))
            return ok({"message_thread_id": tid})
        if m == "sendMessage":
            if p.get("message_thread_id") in self.dead_threads:
                return err("message thread not found")
            self.sent.append((p.get("message_thread_id"), p["text"]))
            self.loud.append(not p.get("disable_notification"))
            return ok({"message_id": self._new()})
        raise AssertionError("unexpected method " + m)

    async def get(self, url, headers=None, timeout=None):
        if "/file/bot" in url:
            return Resp(content=self.files[url.rsplit("/", 1)[1]])
        return Resp(status=503)  # старой версии портала нет


def run(coro):
    return asyncio.run(coro)


def wipe():
    for t in app.DB_EXPORT:
        app.db.execute(f"DELETE FROM {t}")
    app.info._cache.clear()


@pytest.fixture
def tg():
    fake = FakeTg()
    app.http = fake
    app.bak = backup.TgBackup(fake, "1:x", -1)
    app.restore_tries = 0
    wipe()
    yield fake
    app.bak = None


def test_pack_roundtrip_and_stable_digest():
    a, da = backup.pack({"b": 1, "a": [1, "тема"]})
    b, db_ = backup.pack({"a": [1, "тема"], "b": 1})
    assert da == db_ and a == b
    assert backup.unpack(a) == {"a": [1, "тема"], "b": 1}


def test_save_once_then_only_on_change(tg):
    b = app.bak
    assert run(b.save({"x": 1})) is True
    mid = b.msg
    assert f"backup_msg={mid}" in tg.desc
    assert run(b.save({"x": 1})) is False            # ничего не изменилось — в Telegram не ходим
    assert run(b.save({"x": 2})) is True
    assert b.msg == mid and tg.calls.count("sendDocument") == 1 and tg.calls.count("editMessageMedia") == 1
    assert len(tg.msgs) == 1                         # одно сообщение, а не лента файлов


def test_description_text_of_the_owner_is_kept(tg):
    tg.desc = "Моя группа"
    run(app.bak.save({"x": 1}))
    assert tg.desc.startswith("Моя группа") and "backup_msg=" in tg.desc
    run(app.bak._set_pointer(555))
    assert tg.desc.count("backup_msg=") == 1 and "backup_msg=555" in tg.desc and tg.desc.startswith("Моя группа")


def test_sleep_and_wake_keeps_topics_guests_and_balances(tg):
    run(app.room_topic())
    num, topic = run(app.ensure_topic("s" * 43))
    app.q("INSERT INTO users(login,pw,created,by,balance) VALUES('ali','x',0,'t',3600)")
    assert len(tg.topics) == 2
    run(app.save_backup())

    wipe()                                           # сервер уснул: /tmp стёрт
    app.bak = backup.TgBackup(tg, "1:x", -1)         # новый процесс ничего не помнит
    assert app.db_empty()
    run(app.load_state())

    assert not app.restore_tries
    run(app.room_topic())
    assert run(app.ensure_topic("s" * 43)) == (num, topic)
    assert len(tg.topics) == 2                       # ни одной новой темы
    assert app.q("SELECT balance FROM users WHERE login='ali'") == [(3600,)]
    assert len(tg.msgs) == 1                         # пересланная для чтения копия удалена
    num2, _ = run(app.ensure_topic("z" * 43))
    assert num2 == num + 1                           # нумерация гостей продолжается, а не «Анон1» снова


def test_no_backup_yet_is_a_normal_cold_start(tg):
    run(app.load_state())
    assert app.restore_tries == 0 and app.db_empty()


def test_deleted_backup_message_starts_clean_and_makes_new_one(tg):
    run(app.bak.save({"db": {}}))
    tg.msgs.clear()                                  # владелец удалил сообщение с копией
    app.bak = backup.TgBackup(tg, "1:x", -1)
    run(app.load_state())
    assert app.restore_tries == 0
    assert run(app.bak.save({"db": {}, "n": 1})) is True
    assert len(tg.msgs) == 1 and f"backup_msg={app.bak.msg}" in tg.desc


def test_failed_restore_does_not_overwrite_good_backup(tg, monkeypatch):
    run(app.room_topic())
    run(app.save_backup())
    good = dict(tg.msgs)
    wipe()
    app.bak = backup.TgBackup(tg, "1:x", -1)

    async def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(app.bak, "load", boom)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(app.asyncio, "sleep", no_sleep)
    run(app.load_state())
    assert app.restore_tries == 1                    # сбой запомнен: сохранять поверх копии нельзя
    assert tg.msgs == good


def test_deleted_service_topic_is_recreated(tg):
    first = run(app.room_topic())
    tg.dead_threads.add(first)                       # тему удалили в Telegram
    run(app.tg_service("room_topic", app.room_topic, text="🆕 гость"))
    second = run(app.room_topic())
    assert second != first and (second, "🆕 гость") in tg.sent


def test_backup_file_has_no_images(tg):
    app.info.images["0" * 16] = (b"\xff" * 1000, "image/jpeg")
    try:
        st = app.backup_state()
        assert "images" not in st and "kv" in st["db"]
        json.dumps(st)
    finally:
        app.info.images.clear()


def test_restore_failures_never_overwrite_backup(tg, monkeypatch):
    run(app.room_topic())
    run(app.save_backup())
    good = dict(tg.msgs)
    wipe()
    app.bak = backup.TgBackup(tg, "1:x", -1)

    async def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(app.bak, "load", boom)
    app.restore_tries = 1
    for _ in range(10):                              # раньше после 5 попыток копия затиралась
        assert run(app.restore_backup(replace=True)) is False
        app.restore_tries += 1
    assert tg.msgs == good and app.restore_tries == 11


def test_late_restore_replaces_instead_of_mixing(tg):
    num, _ = run(app.ensure_topic("o" * 43))         # старый гость Анон1 с перепиской
    app.q("INSERT INTO messages(num,out,text,ts) VALUES(?,0,'тайна старого гостя',0)", (num,))
    run(app.save_backup())
    wipe()
    app.bak = backup.TgBackup(tg, "1:x", -1)
    new_num, _ = run(app.ensure_topic("n" * 43))     # пока копия не читалась, пришёл новый гость: тоже Анон1
    assert new_num == num
    assert run(app.restore_backup(replace=True)) is True
    assert app.q("SELECT sid FROM guests WHERE num=?", (num,)) == [("o" * 43,)]
    assert app.q("SELECT 1 FROM guests WHERE sid=?", ("n" * 43,)) == []  # новый гость не получил чужое


def test_pointer_failure_removes_new_copy_and_keeps_old_pointer(tg, monkeypatch):
    run(app.bak.save({"x": 1}))
    old = app.bak.msg
    tg.msgs.pop(old)                                 # старое сообщение удалили — придётся слать новое
    real = tg.post

    async def no_desc(url, **kw):
        if url.endswith("/setChatDescription"):
            from test_backup import Resp
            return Resp({"ok": False, "description": "Bad Request: not enough rights"})
        return await real(url, **kw)
    monkeypatch.setattr(tg, "post", no_desc)
    with pytest.raises(RuntimeError):
        run(app.bak.save({"x": 2}))
    assert tg.msgs == {} and f"backup_msg={old}" in tg.desc


def test_damaged_backup_file_counts_as_no_backup(tg):
    run(app.bak.save({"x": 1}))
    fid = tg.msgs[app.bak.msg]
    tg.files[fid] = b"not gzip at all"
    with pytest.raises(backup.NoBackup):
        run(backup.TgBackup(tg, "1:x", -1).load())


def test_long_owner_description_keeps_pointer(tg):
    tg.desc = "Я" * 300
    run(app.bak._set_pointer(42))
    assert len(tg.desc) <= 255 and tg.desc.endswith("backup_msg=42") and tg.desc.startswith("Я")
