#!/usr/bin/env bash
# 一鍵歸零假機（issue 013-B／2026-08-11 孤兒假機事件洪水事故）。
#
# 先 SIGTERM——假機收到會送 disarm 心跳讓地面站關架次（避免殭屍 session），
# 再對殘餘 -9。用 python3-起始的 cmdline 精準匹配，避開查詢 shell 自匹配
# （這正是先前漏殺、孤兒累積的原因）。
#
# **這支收的是程序，不是記錄。** 假機一連上，backend 就會替它自動註冊一筆
# `drones` 記錄——殺掉程序不會讓那筆消失，它會**永遠留在機隊裡並出現在
# 即時頁上**，而且與一台「只是斷線」的真機完全同形（2026-09-02 被使用者
# 抓到：uav-s42 刪不掉，因為每跑一次 test-admission-gate 就長回來一次）。
#
# 記錄要另外刪：`curl -X DELETE :38000/api/drones/<id>`（該端點會一併清掉
# 執行期狀態並通知畫面）。`test-admission-gate.py` 已改成自己收拾。
#
# 用法：scripts/teardown-fakes.sh [容器名，預設 uav-backend]
set -u
CTR="${1:-uav-backend}"
echo "假機一鍵歸零 @ $CTR"

# 1) SIGTERM（graceful disarm）
docker exec "$CTR" sh -c '
for p in /proc/[0-9]*/cmdline; do
  pid=${p#/proc/}; pid=${pid%/cmdline}
  c=$(tr "\0" " " < "$p" 2>/dev/null)
  case "$c" in python3*fake-drone.py*) kill -TERM "$pid" 2>/dev/null && echo "  SIGTERM $pid";; esac
done' 2>/dev/null

sleep 2   # 讓 disarm 心跳送出＋backend 收到關架次

# 2) -9 殘餘
docker exec "$CTR" sh -c '
for p in /proc/[0-9]*/cmdline; do
  pid=${p#/proc/}; pid=${pid%/cmdline}
  c=$(tr "\0" " " < "$p" 2>/dev/null)
  case "$c" in python3*fake-drone.py*) kill -9 "$pid" 2>/dev/null && echo "  KILL -9 (殘餘) $pid";; esac
done' 2>/dev/null

# 3) 最終盤點
left=$(docker exec "$CTR" sh -c 'n=0; for p in /proc/[0-9]*/cmdline; do c=$(tr "\0" " " < "$p" 2>/dev/null); case "$c" in python3*fake-drone.py*) n=$((n+1));; esac; done; echo $n' 2>/dev/null)
echo "剩餘假機程序：$left"
[ "$left" = "0" ] && echo "✅ 歸零完成" || echo "⚠️ 仍有殘餘，請手動檢查"
