# 005 · SITL 在 host network 下把 MAVLink 送到區網閘道

- 狀態：closed
- 嚴重度：high
- 位置：`docker-compose.yml`（sitl service）
- 建立：2026-08-03
- 關閉：2026-08-03

## 現象

`docker compose up -d db sitl` 後 PX4 正常開機、Gazebo 連上，但 backend 的
`/healthz` 永遠是 `"mavlink_connected": false`，mavsdk_server 一直停在
`Waiting to discover system on udpin://0.0.0.0:14540...`。

tcpdump 顯示 PX4 其實有在送，只是送錯地方：

```
11:08:53 enp1s0 Out IP 192.168.137.111.14580 > 192.168.137.1.14540: UDP, length 40
```

目標是 `192.168.137.1`——區網上的另一台機器，封包直接飛出網卡。

## 原因

映像的 `edit_rcS.bash` 用**預設閘道**當 MAVLink 目標：

```bash
function get_host_ip {
    echo "$(ip route | awk '/default/ { print $3 }')"
}
```

這個假設是為 bridge 網路寫的——容器在 bridge 網路裡時，預設閘道正好就是
docker host（172.17.0.1）。但本專案的 sitl 用 `network_mode: host`
（為了讓 MAVLink UDP 不過 NAT，見 `doc/architecture.md`），
容器共用主機路由表，預設閘道就變成區網 router。

## 影響

整條資料流完全不通，且**不會報錯**——backend 看起來一切正常，只是永遠收不到遙測。
若沒抓封包很難定位。

## 解決方式

`docker-compose.yml` 的 sitl service 明確指定目標 IP：

```yaml
    command: ["127.0.0.1", "127.0.0.1"]   # [14540 目標, 14550 目標]
```

驗證：PX4 開機 log 出現 `14540 will be associated to 127.0.0.1`，
`/healthz` 轉為 `"mavlink_connected": true`。

> 註：映像的 `entrypoint.sh` 與 `edit_rcS.bash` 對兩個參數的順序說法不一致
> （前者當成 API→QGC，後者當成 QGC→API）。兩個都填 `127.0.0.1` 可迴避這個歧義。
