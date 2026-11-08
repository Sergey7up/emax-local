# W0003 v01 protocol notes

This implementation was independently written from observed packet structure and public protocol descriptions. Related-model decoder code was not copied.

## References

- [EM3371 protocol](https://github.com/matjon/em3371-controller/blob/main/Documentation/device_protocol.md) and [decoder](https://github.com/matjon/em3371-controller/blob/main/src/emax_em3371.c): measurement triples, temperature encoding, battery bit mask.
- [EM3378D research](https://github.com/serpent213/em3378d_weather/blob/master/protocol.md) and [decoder](https://github.com/serpent213/em3378d_weather/blob/master/lib/em3378d_protocol.c): packet envelope, radio status pairs, console battery experiment, pressure/forecast fields.
- [FanJu decoder](https://github.com/denizkoekden/homeassistant-fanju-fjw4/blob/master/custom_components/fanju_fjw4/protocol.py): independent modern layout, direct seconds, cloud-derived pressure on some hardware.
- [HF-LPT230 manual](https://www.image.micros.com.pl/_dane_techniczne_auto/rf%20hf-lpt230-1.pdf): socket B configuration and `AT+Z` restart.

## Envelope

Accepted measurements are exactly 70 bytes: `AA 3C 57 01`, six MAC bytes, type `53 30 01 00`, little-endian payload length `32 00`, 50 payload bytes, full 16-bit little-endian additive checksum of all preceding bytes, trailer `CC 3E`. TCP fragmentation and coalescing are handled independently per connection, with bounded buffers. Unknown layouts do not create readings.

## Payload

| Offset | Meaning | Exposure |
|---|---|---|
| 0 | Payload version, currently 1 | Validation / diagnostics |
| 1 | Timezone-related code, interpretation unverified | Raw diagnostic |
| 2–7 | Year since 2000, month, day, hour, minute, second | Naive local station clock in diagnostics |
| 8 | Weather refresh code | Raw diagnostic |
| 9–17 | Console current / maximum / minimum | Current sensors; extrema attributes |
| 18–26 | Channel 1 current / maximum / minimum | Same |
| 27–35 | Channel 2 current / maximum / minimum | Same |
| 36–44 | Channel 3 current / maximum / minimum | Same |
| 45 | Power flags | Battery warnings / raw diagnostic |
| 46 | Wireless status | Reception diagnostics / freshness gating |
| 47–48 | Little-endian local pressure | Optional sensor; `FFFF` absent |
| 49 | Local forecast code | Raw diagnostic; `FF` absent |

Each measurement triple contains a little-endian uint16 temperature and uint8 humidity. Celsius is `(raw - 1220) / 18`, rounded to 0.1 °C; `FFFF` is missing. Humidity 0–100 is valid; other values are missing. Fields are validated independently. The modern payload carries direct seconds, unlike the doubled seconds reported for an older model.

## Battery interpretation and uncertainty

The EM3371 decoder interprets byte 45 as `battery_low_bitmask`, with bit N for channel N. A console battery experiment on EM3378D independently supports bit 0. Observed W0003 power flags `07` agree with cloud per-channel fields `1, 2, 4, 0` under `mask & (1 << channel)`.

This supports binary low-battery warnings, not four charge levels. The EM3378D researcher did not establish the higher bits on that model. W0003 remote battery mapping has not yet been verified by replacing each remote's batteries in a controlled experiment. The original byte is retained for future validation. Do not infer percentages from bit positions.

## Radio status

Pairs 0–1, 2–3, 4–5 represent CH1–CH3: `00` receiving, `10` held, `11` missing, `01` unknown. Held/missing/unknown channels are unavailable. W0003 missing placeholders agree with the missing flag; detailed hold transitions derive from related EM3378D research. Other-model timeout durations are not imposed on W0003.

## Pressure and cloud data

The verified W0003 uploads contain `FFFF` pressure and `FF` forecast. Channel B copies outgoing UART traffic; it does not supply cloud responses. Cloud forecast, wind, rain and pressure cannot be reconstructed from absent upload fields. Service packet types are counted in diagnostics without invented entities.

## Configuration safety

Discovery verifies host identity and compatible module. The receiver starts first. A control operation rechecks A/B, saves an operation snapshot before writing, changes only `SOCKB`, reads it back, sends `AT+Z` once, and verifies the returning module's identity and configuration. Lost write acknowledgements are resolved by reading in a fresh session. Operation markers prevent automatic retry loops after an interrupted restart. No factory-reset or firmware-write commands are used.

The Wi-Fi restart is distinct from restarting the display MCU. Verified module configuration and received measurement data are tracked separately. Network recovery files stay local; downloadable diagnostics redact connection identifiers.
