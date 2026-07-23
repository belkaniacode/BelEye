# Compatibility

Short version: **if your recorder speaks DVRIP on port 34567, BelEye will very likely show your
cameras.** If you only have standalone IP cameras, BelEye works with any of them that speak RTSP.

This page is deliberately specific about what is *verified*, what is *likely*, and what does
*not* work. Over-promising here would only waste your evening.

---

## Check your device in 60 seconds

You do not need to know the brand. Three checks, in order of confidence:

**1. Is DVRIP open?** On the same network:

```bash
nc -vz 192.168.1.108 34567      # replace with your recorder's IP
```

`succeeded` / `open` → your recorder speaks the protocol BelEye uses for recorders.

**2. Is it configured through a phone app that adds devices by IP + port 34567, or by a device
ID / QR code?** Those apps talk to devices built on Xiongmai (XM) technology *regardless of the
brand on the box*, and 34567 is the port they use for a direct connection. If your manual describes
that flow, you are in the right family.

**3. Just try it.** Add the recorder in BelEye → **Проверить соединение**. It logs in and lists
the channels, or it tells you exactly what failed.

> **The chip is not what matters — the firmware is.** Listings advertise "Mstar chip",
> "HiSilicon", "Novatek". That tells you nothing about compatibility. The same chip running
> Hikvision-style firmware will not work; a different chip running Xiongmai/Sofia firmware will.
> Judge by the protocol and the port, never by the chip.

---

## Recorders (NVR / DVR / HVR)

BelEye speaks **DVRIP** (also called the *Sofia* protocol) on TCP port **34567** — the protocol
these recorders use for direct connections. Hangzhou Xiongmai is an OEM that supplies well over
a hundred downstream brands and sells almost nothing under its own name, which is why this one
protocol covers a very large slice
of the affordable market: many of the no-name 4/8/16-channel PoE recorders on AliExpress, Amazon
and eBay are the same firmware behind different logos.

| Level | What | Why we can say that |
|---|---|---|
| ✅ **Verified on hardware** | **Xiongmai NBD80S16S-KL**, firmware `V4.03.R11`, 8-channel PoE HVR — live view, archive playback, calendar, timeline, mp4 export, REC indicator, playback speed ¼×–8× | This is the device BelEye is developed against. Every feature is tested on it. |
| 🟢 **Very likely — live view** | Other DVRIP recorders | The parts that matter are platform-wide: the 20-byte packet header, the MD5-based `sofia_hash` login, and the `OPMonitor` live stream. Channel discovery tries **six** different firmware dialects before giving up. |
| 🟡 **May need work — archive** | Other DVRIP recorders, playback of recordings | Archive is where firmwares diverge most, and BelEye's is tuned to the device above: playback data arrives on opcode `1422` where the reference implementation documents `1426`; the claim/start order is `1413 → 1410`, the opposite of the obvious one; `OPFileQuery` silently caps at 64 records per reply. Different firmware may need adjustments. |
| ❌ **Not supported** | Hikvision, Dahua, Uniview, Reolink, Axis, Hanwha **recorders** | Each uses its own closed protocol (Hikvision ISAPI/SDK on 8000, Dahua on 37777, …). Nothing in common with DVRIP. Their **cameras** are fine — see below. |

**Channel count.** BelEye supports whatever the recorder reports and shows the ports that
actually have a camera. On an 8-channel unit with 4 cameras connected you will see 4 tiles —
the empty ports are hidden on purpose, not missed.

**If your recorder does not work,** open an issue with the log line starting `[NVR] discovery` and
the output of the check above. Discovery is the part most likely to need one more dialect, and it
is usually a small fix.

---

## IP cameras (without a recorder)

**Any camera that speaks RTSP works** — Hikvision, Dahua, Reolink, Uniview, TP-Link, Amcrest,
no-name. BelEye builds a plain RTSP URL and hands it to FFmpeg, which handles Basic/Digest
authentication and both H.264 and H.265.

The one thing you must supply is the **stream path**, because BelEye has no ONVIF auto-discovery
yet. Common paths:

| Brand | Typical RTSP path |
|---|---|
| **Xiongmai / DVRIP** | `/user=admin&password=PASS&channel=1&stream=0.sdp` — `stream=0` main, `stream=1` sub ✅ *verified* |
| Hikvision (and Annke) | `/Streaming/Channels/101` main, `/102` sub |
| Dahua (and Amcrest) | `/cam/realmonitor?channel=1&subtype=0` |
| Reolink | `/h264Preview_01_main`, `/h264Preview_01_sub` |
| Uniview | `/media/video1` |
| TP-Link Tapo | `/stream1` main, `/stream2` sub |

Only the first row is verified by us; the rest are the widely documented defaults for those
vendors and may differ by model or firmware. Your camera's manual, or its web interface, is
authoritative. If a path is wrong the connection test says so rather than failing silently.

**TCP or UDP?** Leave it on **TCP** unless you have a reason not to. TCP survives lossy Wi-Fi and
NAT; UDP is offered because a few cameras have broken TCP interleaving and because it has slightly
lower latency on a clean wired LAN. Note that some recorders' RTSP servers answer over TCP only —
the Xiongmai unit above is one of them, so if you point BelEye at a *recorder's* RTSP port, use
TCP.

---

## Not implemented

Stated plainly so you can decide before installing:

- **ONVIF** — no camera auto-discovery and no automatic RTSP path lookup. Paths are typed by hand.
- **PTZ** — no pan/tilt/zoom control.
- **Audio** — video only, in both live and archive.
- **Two-way talk**, alarm I/O, and on-device configuration (recording schedules, motion zones) —
  use the recorder's own web interface for those.
- **Motion/face/vehicle event browsing** — the recorder's own detections are not surfaced as an
  event list. BelEye shows the REC indicator and marks days that contain event-flagged recordings
  in the archive calendar, but does not filter by event type.

ONVIF is the most requested of these and the one that would widen support the most; it is not
promised for any particular release.

---

## The verified device, in full

Read from the device itself over DVRIP, not from the sales listing:

| | |
|---|---|
| Hardware | `NBD80S16S-KL` |
| Firmware | `V4.03.R11.C6380233.12201.140000.0000000` (built 2023-04-14) |
| Type | HVR, 8 channels (`DigChannel: 8`) |
| Encoding | H.265 / H.264, dual stream (main + sub) |
| Detection | motion, video loss, camera blind, face, human, vehicle |
| Network | RTSP, DDNS, NTP, FTP, e-mail, UPnP, P2P |

Sold as an *"8-channel PoE NVR for IP cameras, 8MP/5MP/4MP/2MP, H.265, face / human / vehicle
detection"* — the exact wording used by many listings for the same hardware.
