<p align="center"><img src="docs/logo.png" alt="EMAX Local" width="512"></p>

# Your weather. On your network.

**Local EMAX weather stations for Home Assistant.** [Українська](README.md) · **English**

Enter the station's IPv4 address in the graphical setup flow. EMAX Local identifies a compatible Wi-Fi module, starts its TCP receiver, configures channel B and restarts the module. Channel A keeps its previous configuration, allowing the manufacturer's application to continue working. Local measurements require no EMAX account or cloud API.

## Features

| Data | Home Assistant representation |
|---|---|
| Console and CH1–CH3 temperature / humidity | Eight measurement sensors with long-term statistics |
| Station-reported minima and maxima | `station_min` and `station_max` attributes |
| Low battery warnings | Four battery binary sensors |
| Remote sensor reception | Three diagnostic connectivity sensors |
| Setup status | One diagnostic enum sensor |
| Wi-Fi module restart | Device button |
| Station clock, receipt time, protocol fields | Downloadable diagnostics, without timestamp entities |

Readings become unavailable after 180 seconds without a valid measurement packet. Missing values are never replaced by zero. Remote readings held by the station are not presented as fresh measurements.

## Compatibility

Verified: **W0003 v01**, 50-byte measurement payload, **HF-LPT230**, firmware **4.12.17**, **Home Assistant 2026.11+**. Allow TCP from the station to HA and UDP 48899 from HA to the station on your LAN.

Other EMAX models may use different layouts. Unsupported packets are rejected. Related EM3371, EM3378D and FanJu decoders informed the format research; these models are not claimed as tested hardware. See [protocol notes](docs/protocol.md).

## Install with Marketplace / HACS

1. Open **Settings → Marketplace**, or HACS on an older installation. Read its initial warning and connect GitHub if requested.
2. Add **`Sergey7up/emax-local`** as a custom repository, category **Integration**.
3. Download **EMAX Local** and restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration → EMAX Local**.
5. Enter the station's IPv4 address, review discovered settings and confirm.

The receiver starts before channel B is changed. Wait for **Receiving measurements** after the module restarts. A compatible module normally needs no battery removal: `AT+Z` restarts its Wi-Fi module, not the display controller. Replacing an existing channel B destination requires explicit confirmation. Recovery snapshots are saved locally under `/config/emax_local_backups/`.

For a changed station IP, select **Reconfigure** from the integration's menu.

### Updates and existing installations

Install releases through the same store and restart HA. The integration domain, device MAC identity and measurement unique IDs stay stable. Install over a previous manual installation without deleting its config entry. This preserves entity customizations. The integration does not automatically rewrite unrelated YAML or migrate historical cloud entities.

## Honest data boundaries

**Battery warnings are binary, not percentages.** The power byte is treated as a bit mask: `1`, `2` and `4` identify bit positions, not charge levels. Remote channel interpretation is supported by the EM3371 decoder and matching W0003 cloud fields; individual controlled battery replacement experiments remain unverified. Battery status is unavailable when a remote sensor has no current radio reception.

**Pressure is optional and disabled by default.** The tested station sends `FFFF`, meaning no local pressure. Cloud pressure, weather forecasts, wind and rain are not substituted for measurements absent from the station upload.

Extrema belong to the station's own accumulation window; they are not guaranteed to be daily minima/maxima. Temperature extrema attributes remain in °C even when the sensor display is converted to °F.

## Support and development

Check the connection status, HA logs and downloadable diagnostics. Review logs for private data before sharing them; recovery snapshots contain network settings. [Report an issue](https://github.com/Sergey7up/emax-local/issues) with station model, HA/integration versions and expected versus actual behavior.

```sh
python -m unittest discover -s tests -v
```

Tests use synthetic fixtures, real loopback TCP/UDP sockets and isolated control sessions. Live HA setup and resumed uploads were checked separately. Artwork was AI-generated. This independent project is not affiliated with EMAX.

[MIT License](LICENSE) · [Changelog](CHANGELOG.md)
