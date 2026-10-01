#!/usr/bin/env python3
"""從地面站盯機上 Pi 的可達性，1 Hz（issues/072）。

**這支要回答的是一個問題：Pi 無預警重開時，它是「斷電」還是「卡住」？**

兩者在 Pi 自己的日誌裡長得一模一樣——都是在一行正常訊息之後直接沒了。
但從外面看不一樣：

| 探針 | 斷電 | 卡住（userspace／systemd 停擺） |
|---|---|---|
| ICMP（**核心回的**，不需要 userspace） | 立刻消失 | **還會繼續回**，直到看門狗重置 |
| TCP :22（sshd，userspace） | 立刻消失 | 可能還在聽，但接受不了新連線 |

RPi OS **預設**開著 systemd 的硬體看門狗（`40-rpi-enable-watchdog.conf`，
`RuntimeWatchdogSec=1m`）：systemd 有 60 秒沒餵狗，硬體就重置，而且不留痕跡。
所以「日誌死了之後 ICMP 還回了大約一分鐘」＝卡住被看門狗重置；
「兩個探針同時消失」＝供電斷了。

搭配機上的 `tools/power-log.sh`（uav-agent）一起看：那邊有斷掉前最後一刻的
EXT5V 電壓。兩邊的時戳對起來就說得出是哪一種。

用法：
    python3 scripts/watch-pi-outage.py            # 前景
    nohup python3 scripts/watch-pi-outage.py &    # 放著跑

輸出：`/var/tmp/pi-outage.log`（每行都 flush＋fsync——**地面站自己被關掉時
也不能丟掉最後那幾行**），另外每段中斷結束時印一行判讀。
"""
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

#: 寫死就會失效——2026-10-01 的教訓：這支從 9/24 起一直盯著一個不存在的
#: 位址，整週的紀錄都是「斷線中」，而它正是為了查 Pi 無預警重開而裝的
#: （issues/072／075）。預設改成**跟著地面站學到的對端走**。
HOST = os.environ.get("PI_HOST") or ""
LOG = os.environ.get("OUTAGE_LOG", "/var/tmp/pi-outage.log")
# **只探 22。** 原本連 8554（相機服務）也探，而那會在機上的 uav-camera 日誌裡
# 每秒留一行「conn opened」，把真正要看的東西洗掉——**診斷工具不該污染被
# 診斷的對象**。sshd 同樣是 userspace 行程，足以回答「userspace 還活著嗎」。
PORTS = [22]
PERIOD = 1.0


def learn_host() -> str | None:
    """機上現在的位址——取自指令服務學到的對端（實際封包的來源）。

    **這支要盯的是「那台 Pi」，不是「某個 IP」。** 位址會變（換網段、5G 重新
    配址），寫死的話它會安靜地盯著一個不存在的位址，而「一直不通」與「真的
    掛了」在紀錄裡長得一模一樣。
    """
    try:
        out = subprocess.run(
            ["docker", "exec", "uav-command", "cat", "/state/peers.json"],
            capture_output=True, timeout=5).stdout
        peers = (json.loads(out).get("peers") or {}) if out else {}
    except Exception:
        return None
    for v in peers.values():
        if v.get("ip"):
            return v["ip"]
    return None


def ping(host: str, timeout: float = 0.8) -> bool:
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout * 1000)) + "ms", host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout + 0.5).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def tcp(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


#: 多久重新確認一次機上位址（秒）。**位址變了要跟上**，但也不必每秒問
HOST_REFRESH_S = 60.0


def main() -> None:
    global HOST
    f = open(LOG, "a", buffering=1)

    def emit(line: str) -> None:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())          # **斷電也要留得住**

    if not HOST:
        HOST = learn_host() or ""
        if not HOST:
            emit(f"# {datetime.now():%F %T} **問不到機上位址**"
                 "（指令服務沒有學到對端？）——設 PI_HOST 指定")
            sys.exit(2)
    emit(f"# {datetime.now():%F %T} 開始盯 {HOST}（ICMP＋TCP {PORTS}）")
    host_at = time.monotonic()
    # 一段中斷的記錄：什麼時候各個探針最後一次成功
    last_ok = {"icmp": None, **{f"tcp{p}": None for p in PORTS}}
    down_since = None

    while True:
        t0 = time.monotonic()
        now = datetime.now()
        # 位址跟著走。**換位址要寫進紀錄**——不然事後看到的「中斷」會分不出
        # 是機器掛了還是它搬家了
        if not os.environ.get("PI_HOST") and t0 - host_at > HOST_REFRESH_S:
            host_at = t0
            found = learn_host()
            if found and found != HOST:
                emit(f"# {now:%F %T} **機上位址換了**：{HOST} → {found}")
                HOST = found
                last_ok = {k: None for k in last_ok}
                down_since = None
        res = {"icmp": ping(HOST)}
        for p in PORTS:
            res[f"tcp{p}"] = tcp(HOST, p)
        for k, v in res.items():
            if v:
                last_ok[k] = now
        line = " ".join(f"{k}={'1' if v else '0'}" for k, v in res.items())
        emit(f"{now:%F %T} {line}")

        alive = any(res.values())
        if not alive and down_since is None:
            down_since = now
            emit(f"# {now:%F %T} **全部探針都不通**——中斷開始")
        elif alive and down_since is not None:
            gap = (now - down_since).total_seconds()
            # **判讀寫在這裡，不要留給事後回想**
            icmp_last = last_ok["icmp"]
            ssh_last = last_ok["tcp22"]
            extra = ""
            if icmp_last and ssh_last and icmp_last > ssh_last:
                d = (icmp_last - ssh_last).total_seconds()
                extra = (f"；ICMP 比 sshd 多撐了 {d:.0f} 秒"
                         "——**核心還活著而 userspace 已經死了，像是卡住**"
                         if d >= 10 else f"；ICMP 比 sshd 多撐 {d:.0f} 秒（差不多同時，像是斷電）")
            emit(f"# {now:%F %T} 恢復，中斷 {gap:.0f} 秒{extra}")
            emit(f"#   最後一次成功：" +
                 "、".join(f"{k} {v:%T}" if v else f"{k} 從沒成功"
                           for k, v in last_ok.items()))
            down_since = None

        time.sleep(max(0.0, PERIOD - (time.monotonic() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
