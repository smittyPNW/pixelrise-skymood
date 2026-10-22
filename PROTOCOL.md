# Dekala PixelRise: local protocol notes

Captured 2026-10-21 by driving the official app while a read-only local listener recorded every datapoint
(DP) the clock reported. Tuya product id `kb52kwiygv7wxt0m`, category `znnz`, **Tuya local protocol 3.5**
on TCP 6668. The clock doesn't broadcast on UDP 6666/6667; the phone app finds it with a 3.5 discovery packet on UDP 7000.

## Hardware and app

- **App:** "Dekala" (`com.jukuner.furlife`), a Tuya white-label app. The clock also pairs in Smart Life and shows the same panel.
- **Maker:** Shenzhen Juku Intelligent Technology, FCC grantee 2ARPE. There's no FCC filing for the PixelRise
  itself; sibling Dekala clocks use a Tuya WBR3 (RTL8720CF) or CB3S (BK7231N) module plus a separate chip.
- **Wi-Fi MAC:** from Tuya's own OUI (d8:c8:0c); the network hostname is `lwip0`.

## Datapoints

| DP | Type | Meaning |
|---|---|---|
| 9 | bool | network time sync |
| 10 | enum | `12h` / `24h` |
| 11 | enum | backlight `closed` / `day` / `night` / `auto` |
| 15 | int | volume 5–100 |
| 16, 17 | bool | alarm 1 / alarm 2 on |
| 21 | bool | snooze |
| 23 | bool | sleep aid |
| 102 | int | indoor humidity % (reported every few minutes) |
| 106 | int | indoor temperature, °C × 10 |
| 119 | string | temperature unit (`f`) |
| 104, 108, 105, 109, 114, 115 | mixed | alarm / display options, not mapped yet |
| **111** | raw (13 B) | **alarm 1 settings** |
| **120** | raw (4 B) | **playlist state**: `count, current index, current animation id, 01` |
| **121** | raw (11 B × n) | **playlist definition**, 1–19 items |
| 122 | string | play mode (`Loop`) |
| 128 | raw (7 B) | clock time `yy mm dd hh mm ss weekday`, reported every minute |

Raw DPs travel as base64 strings. They are not returned by a status query; they arrive as reports when
they change, so keep a persistent connection open (`tools/watch_local.py`).

## DP 121: playlist item (11 bytes)

```
[id] [6 option bytes] [00] [seconds] [colour] [tail]
```

Two layouts appear, and writing either back works:
- **Clock faces 11 and 12:** `id 00 00 00 00 00 00 00 secs colour 00`
- **Everything else:** `id 01 01 01 01 01 05 00 secs colour 32`

Colour is a swatch index: 0 white, 1 red, 2 orange (verified). 3–7 are inferred from the app's swatch order
(yellow, green, cyan, blue, purple). 8 appears on an item whose colour was set to "gradient".

Writing DP 121 replaces the playlist; the clock answers with a DP 120 report giving the new count.

## Built-in animation IDs (all 29 in the app's library)

| ID | Animation | ID | Animation | ID | Animation |
|---|---|---|---|---|---|
| 11 | big clock face | 71 | galaxy spiral | 81 | marble swirl |
| 12 | small clock face | 72 | comet line | 82 | plaid grid |
| 13 | date | 73 | wave (aurora-like) | 83 | sine waves |
| 21 | weather icon + temperature | 74 | colour swirl | 84 | sunburst |
| 31 | indoor temperature + humidity | 75 | blob map | 85 | fire bars |
| 51 | alarm clock | 76 | sparkle lines | 86 | fireworks |
| 52 | ghost | 77 | diamond pattern | 87 | triangle |
| 53 | whale | 78 | flames | 88 | rain |
| 54 | city skyline | 79 | bars | 89 | particles |
| | | 80 | checker mosaic | 90 | glowing ring |

The app allows at most 19 items per playlist.

## DP 111: alarm 1 (13 bytes)

```
hh mm repeat 01 volume 00 duration 00 01 sound 01 animation 02
```

Example: `07 1e 7f 01 07 00 1e 00 01 02 01 09 02` = 07:30, every day (0x7f), volume 7, 30 minutes, sound
"Forest", animation "Equalizer". Alarm 2's raw DP was not captured.
