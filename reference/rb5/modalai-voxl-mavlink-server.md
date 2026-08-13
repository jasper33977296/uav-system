# ModalAI voxl-mavlink-server（使用者手動提供，2026-08-13）

> 來源：ModalAI 官方文件（v1.4.18 時點）。為 issue 032 候選 2
> （MANUAL_CONTROL 轉發問題）調查而抓。原文照錄，分析見
> `doc/032-rb5-manual-control-analysis.md`。

## Overview

voxl-mavlink-server manages MAVLink routing between network interfaces, MPA applications, and the flight controller such as PX4.

## Configuration

voxl-mavlink-server reads its configuration from `/etc/modalai/voxl-mavlink-server.conf`. The available fields and their defaults (as of v1.4.18):

| Field | Default | Description |
|---|---|---|
| primary_static_gcs_ip | "192.168.8.10" | Static GCS IP that voxl-mavlink-server automatically tries to connect to. 192.168.8.10 is the first IP that VOXL DHCP serves when connecting in WiFi SoftAP mode. Set to empty or NULL to let the GCS initialize the connection instead. |
| primary_static_gcs_ip_port | 14550 | UDP port number for the primary static GCS IP. |
| secondary_static_gcs_ip | "192.168.8.11" | Second static GCS IP to automatically try to connect to. |
| secondary_static_gcs_ip_port | 14550 | UDP port number for the secondary static GCS IP. |
| onboard_port_to_autopilot | 14556 | UDP port to send high-rate onboard data to SLPI when running voxl-px4. |
| onboard_port_from_autopilot | 14557 | UDP port to receive high-rate onboard data from SLPI. |
| gcs_port_to_autopilot | 14558 | UDP port to send normal-rate GCS data to SLPI. |
| gcs_port_from_autopilot | 14559 | UDP port to receive normal-rate GCS data from SLPI. |
| en_external_uart_ap | false | QRB5165 only. Set to true to enable UART communication to an external flight controller, otherwise a UDP interface is started to talk to voxl-px4 on localhost. |
| autopilot_uart_bus | 1 | UART bus for the external autopilot. Bus 1 goes through the legacy B2B connector (M0125/M0141 accessory boards); bus 12 goes through the ESC port (J18). |
| autopilot_uart_baudrate | 921600 | UART baudrate for the external autopilot. |
| autopilot_mission_delay_start | -1 | -1 is off; a value >0 delays mission start by that many seconds. |
| autopilot_mission_delay_sound | false | Play the ESC chime tone/behavior during the mission delay. |
| autopilot_mission_force_restart | 0 | Force mission restart. |
| autopilot_mission_notif_dur | 0.1 | Visual notification using motor spin, duration in seconds. |
| en_external_ap_timesync | 1 | Enable responding to timesync messages. |
| en_external_ap_heartbeat | 1 | Enable automatic sending of heartbeat. |
| en_elrs_rc_mux | false | (v1.4.18 and newer) RC-source mux for the rc_active pipe with ELRS failsafe awareness. |
| udp_mtu | 512 | Maximum transfer unit for aggregated UDP packets back to the GCS. Set to 0 to disable aggregation. |
| gcs_timeout_s | 4.5 | Time without a heartbeat before a GCS is considered disconnected. |

Never set primary_static_gcs_ip or secondary_static_gcs_ip to localhost, these are meant for external ground control stations. For local MAVSDK and MAVROS communication, set the `en_localhost_mavlink_udp` flag in voxl-vision-hub's `/etc/modalai/voxl-vision-hub.conf` file instead.

### udp_mtu

For radios, such as Doodle Labs, that utilize larger packet sizes with increased latency, voxl-mavlink-server can bundle mavlink messages headed to the GCS into larger UDP packets. Since v1.4.14 this aggregation is on by default with a udp_mtu of 512 bytes. Set udp_mtu to 0 to disable aggregation and send one UDP packet per message. Config-file description:

```
 * udp_mtu - maximum transfer unit for UDP packets back to GCS. voxl-mavlink-server
 *           will bundle up backets for the GCS into a single UDP packet with
 *           a maxium size of this. This saves network traffic drastically.
 *           Set to 0 to disable this feature and send one UDP packet per msg.
```

Try a few different values such as 100, 200, 300 etc to find an optimal value for your setup.

## Pipes

voxl-mavlink-server publishes the following 14 pipes into MPA.

Data from the high-rate onboard stream:

- `mavlink_onboard`: full high-rate 'onboard mode' mavlink stream from the autopilot
- `mavlink_ap_heartbeat`: autopilot heartbeat messages
- `mavlink_sys_status`: sys_status messages
- `mavlink_gps_raw_int`: gps_raw_int messages
- `mavlink_attitude`: attitude messages
- `mavlink_local_position_ned`: local_position_ned messages
- `imu_mavlink`: IMU data from the autopilot
- `px4_baro`: barometer data from the autopilot
- `rc_channels`: RC channel data
- `rc_active`: **active RC source, muxing rc_channels and manual_control**

GCS-related pipes:

- `mavlink_to_gcs`: normal-rate stream from the autopilot to the GCS
- `mavlink_from_gcs`: data coming from the GCS
- `gcs_ip_list`: list of IP addresses of connected GCS
- `mission`: mission data

Up to 16 simultaneous GCS connections can be established over UDP port 14550. See the **Mavlink Routing feature guide** for the routing paths and how to inspect these pipes with `voxl-inspect-mavlink`.

## 補充（使用者口述，2026-08-13）

RB5 上 `COM_RC_IN_MODE` 的實際值**要等無人機修好才查得到**——事前無法確認，
032 的「執行期前提檢查」建議因此升為必要（不能假設出廠值）。
