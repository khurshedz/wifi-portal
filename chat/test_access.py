"""Сценарии обхода лимитов. Запуск: python -m pytest test_access.py -q (из папки chat)."""
import sqlite3

import pytest

import access
from access import AccessError

M1, M2, M3 = "AA-AA-AA-AA-AA-01", "AA-AA-AA-AA-AA-02", "AA-AA-AA-AA-AA-03"
FP1, FP2 = "a" * 64, "b" * 64
T0 = 1_800_000_000.0  # фиксированное «сейчас»


@pytest.fixture
def q():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript(access.SCHEMA)
    return lambda sql, args=(): db.execute(sql, args).fetchall()


def user(q, login, sid="s-" + "x" * 40, mac=M1, fp=FP1):
    uid = access.create_user(q, login, "secret1", "test")
    access.bind_sid(q, sid, uid)
    return uid


def test_free_minutes_once_per_day_per_user(q):
    u = user(q, "ali")
    mac, secs, _ = access.start(q, u, "sid1", M1, FP1, "free", T0)
    assert secs == access.FREE_SEC
    access.stop(q, u, "expired", T0 + secs)
    with pytest.raises(AccessError):
        access.start(q, u, "sid1", M1, FP1, "free", T0 + secs + 1)


def test_mac_change_does_not_give_new_free_minutes(q):
    u = user(q, "ali")
    _, secs, _ = access.start(q, u, "sid1", M1, FP1, "free", T0)
    access.stop(q, u, "expired", T0 + secs)
    # тот же пользователь, новый (случайный) MAC, другой браузер — минуты не возвращаются
    with pytest.raises(AccessError):
        access.start(q, u, "sid2", M2, FP2, "free", T0 + secs + 5)


def test_second_account_same_device_no_new_free_minutes(q):
    a = user(q, "ali")
    _, secs, _ = access.start(q, a, "sid1", M1, FP1, "free", T0)
    access.stop(q, a, "expired", T0 + secs)
    b = user(q, "vali")
    with pytest.raises(AccessError):  # тот же MAC / отпечаток / cookie
        access.start(q, b, "sid1", M1, FP1, "free", T0 + secs + 5)
    with pytest.raises(AccessError):  # даже если сменить MAC, но остался отпечаток
        access.start(q, b, "sid9", M3, FP1, "free", T0 + secs + 5)


def test_registration_once_per_device(q):
    access.register(q, "sid1", M1, FP1, "ali", "secret1")
    with pytest.raises(AccessError):
        access.register(q, "sid2", M1, FP2, "vali", "secret1")  # тот же MAC
    with pytest.raises(AccessError):
        access.register(q, "sid3", M2, FP1, "vali", "secret1")  # тот же отпечаток
    with pytest.raises(AccessError):
        access.register(q, "sid4", None, None, "vali", "secret1")  # не из Wi-Fi


def test_one_active_device_per_user_and_time_not_duplicated(q):
    u = user(q, "ali")
    q("UPDATE users SET balance=3600 WHERE id=?", (u,))
    access.start(q, u, "sid1", M1, FP1, "paid", T0)
    # друг входит под тем же логином с другого телефона: первое устройство отключается,
    # остаток переносится, а не удваивается
    mac, secs, revoke = access.start(q, u, "sid2", M2, FP2, "paid", T0 + 600)
    assert revoke == [M1] and mac == M2
    assert secs == 3000
    assert q("SELECT COUNT(*) FROM sessions")[0][0] == 1


def test_device_limit_blocks_account_sharing(q):
    u = user(q, "ali")
    q("UPDATE users SET balance=36000 WHERE id=?", (u,))
    access.start(q, u, "s1", M1, FP1, "paid", T0)
    access.start(q, u, "s2", M2, FP2, "paid", T0 + 10)
    with pytest.raises(AccessError):  # третье устройство — только через админа
        access.start(q, u, "s3", M3, None, "paid", T0 + 20)


def test_other_user_on_same_mac_is_replaced(q):
    a, b = user(q, "ali"), user(q, "vali")
    q("UPDATE users SET balance=600 WHERE id IN (?,?)", (a, b))
    access.start(q, a, "s1", M1, FP1, "paid", T0)
    access.start(q, b, "s2", M1, FP1, "paid", T0 + 100)
    assert q("SELECT user_id FROM sessions")[0][0] == b
    assert q("SELECT balance FROM users WHERE id=?", (a,))[0][0] == 500  # ali получил остаток обратно


def test_stop_refunds_unused(q):
    u = user(q, "ali")
    _, secs, _ = access.start(q, u, "s1", M1, FP1, "free", T0)
    access.stop(q, u, "user", T0 + 300)
    assert access.free_left(q, access.free_keys(u, M1, FP1, "s1"), T0 + 301) == secs - 300


def test_codes_single_use_and_bound_to_user(q):
    a, b = user(q, "ali"), user(q, "vali")
    [c] = access.make_codes(q, 60, 1)
    assert access.redeem(q, a, c.lower().replace("-", "")) == 60
    with pytest.raises(AccessError):
        access.redeem(q, b, c)
    assert q("SELECT balance FROM users WHERE id=?", (a,))[0][0] == 3600


def test_login_and_blocked(q):
    access.create_user(q, "ali", "secret1", "t")
    with pytest.raises(AccessError):
        access.login_user(q, "s1", "ali", "wrong")
    assert access.login_user(q, "s1", "ALI", "secret1")
    q("UPDATE users SET blocked=1")
    with pytest.raises(AccessError):
        access.login_user(q, "s2", "ali", "secret1")


def test_no_mac_no_internet(q):
    u = user(q, "ali")
    with pytest.raises(AccessError):
        access.start(q, u, "s1", None, FP1, "free", T0)


def test_new_day_resets_free(q):
    u = user(q, "ali")
    _, secs, _ = access.start(q, u, "s1", M1, FP1, "free", T0)
    access.stop(q, u, "expired", T0 + secs)
    _, secs2, _ = access.start(q, u, "s1", M1, FP1, "free", T0 + 86400)
    assert secs2 == access.FREE_SEC


def test_radius_only_for_token_mac_and_chunk_not_refunded(q):
    u = user(q, "ali")
    access.start(q, u, "s1", M1, FP1, "free", T0)
    tu, tp = access.relogon(q, u, M1)
    assert access.radius_auth(q, tu, tp, M2, 3600, T0 + 1) is None      # чужой MAC
    assert access.radius_auth(q, tu, "x" * 16, M1, 3600, T0 + 1) is None  # чужой пароль
    secs, mode = access.radius_auth(q, tu, tp, M1, 120, T0 + 1)          # кусок 2 мин
    assert secs == 120 and mode == "free"
    access.stop(q, u, "user", T0 + 60)  # остановил через минуту: выданные роутеру 2 мин не возвращаются
    left = access.free_left(q, access.free_keys(u, M1, FP1, "s1"), T0 + 61)
    assert left == access.FREE_SEC - 121


def test_relogon_only_on_own_device(q):
    u = user(q, "ali")
    access.start(q, u, "s1", M1, FP1, "free", T0)
    with pytest.raises(AccessError):
        access.relogon(q, u, M2)


def test_identify_by_any_known_signal(q):
    a = access.quick_user(q, "sidA", M1, FP1)
    assert access.quick_user(q, "sidB", M1, FP2) == a      # новый браузер, тот же MAC
    assert access.quick_user(q, "sidC", M2, FP1) == a      # новый (случайный) MAC, тот же отпечаток
    assert access.quick_user(q, "sidA", M3, None) == a     # та же cookie
    assert access.quick_user(q, "sidD", M3, None) == a     # M3 уже дописан к аккаунту
    b = access.quick_user(q, "sidE", "AA-AA-AA-AA-AA-09", "c" * 64)
    assert b != a                                          # совсем новое устройство — новый аккаунт
    with pytest.raises(AccessError):
        access.quick_user(q, "sidF", None, "d" * 64)       # не из Wi-Fi и ничего не совпало


def test_accounting_presence_and_traffic(q):
    u = user(q, "ali")
    access.start(q, u, "s1", M1, FP1, "free", T0)
    tu, _ = access.relogon(q, u, M1)
    access.acct_update(q, "start", "A1", tu, M1, "10.1.30.5", 0, 0, 0, now=T0 + 2)
    access.acct_update(q, "interim", "A1", tu, M1, "10.1.30.5", 60, 1000, 61000, now=T0 + 62)
    d = access.dashboard(q, T0 + 70)
    [o] = d["online"]
    assert o["user"] == "ali" and o["router"] and o["down"] == 61000 and round(o["rate_down"]) == 1017
    # ушёл: Chilli закрыл по простою через 900 с — в длительность простой не входит
    access.acct_update(q, "stop", "A1", "", M1, "", 300 + 900, 2000, 90000, cause="idle", idle=900, now=T0 + 1202)
    d = access.dashboard(q, T0 + 1300)
    [h] = d["history"]
    assert h["secs"] == 300 and h["cause"] == "idle" and h["user"] == "ali"
    assert d["today"]["bytes"] == 92000 and d["users"][0]["today_secs"] == 300


def test_accounting_after_session_gone_keeps_user(q):
    u = user(q, "ali")
    access.start(q, u, "s1", M1, FP1, "free", T0)
    tu, _ = access.relogon(q, u, M1)
    access.acct_update(q, "start", "A2", tu, M1, "", 0, 0, 0, now=T0)
    access.stop(q, u, "kick", T0 + 10)  # кикнули: на роутере ещё до конца куска
    d = access.dashboard(q, T0 + 20)
    assert d["online"][0]["router"] and not d["online"][0]["portal"] and d["online"][0]["user"] == "ali"
    access.acct_update(q, "stop", "A2", tu, M1, "", 100, 1, 2, cause="timeout", now=T0 + 100)
    assert access.dashboard(q, T0 + 101)["online"] == []
