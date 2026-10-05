#!/usr/bin/env python3
"""Bevraag de Poolex IceSpa 7 lokaal via tinytuya (protocol 3.5).

Leest credentials uit devices.json (gitignored). Gebruik:
    python3 tools/query_dps.py [ip]

Toont de ruwe DP-dump plus een vertaaltabel met bekende/vermoede codes.
"""
import json
import sys
from pathlib import Path

import tinytuya

REPO = Path(__file__).resolve().parent.parent
DEV = json.load(open(REPO / "devices.json"))[0]
IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.30.111"

# Huidige interpretatie van de lokale DPs (zie entities.md / README)
LEGEND = {
    "1": "switch (aan/uit)",
    "2": "?? string '4' - mode?",
    "3": "child_lock",
    "4": "temp_set (x100)",
    "6": "temp_unit_convert",
    "7": "defrost (schrijfbaar)",
    "16": "temp_current watertemp (x100)",
    "20": "compressor_strength 0-1500",
    "23": "?? temp? (1920->19.2C)",
    "24": "?? temp? (2000->20.0C)",
    "25": "temp_effluent uitgaand (x100)",
    "26": "temp_around buitentemp (x100)",
    "33": "defrost_state",
    "101": "?? bool True - pomp/relais-status?",
    "102": "?? int 0 - fault-code kandidaat",
    "103": "?? string '0'",
    "104": "C4? heater-relais mode (0=uit)",
    "105": "C5? 500->5.0C buitentemp-drempel",
    "106": "C6? 500->5.0C verschil auto",
    "107": "C7? 200->2.0C verschil boost",
    "108": "?? 2000",
    "109": "C8? '0' pomp-relais mode",
    "110": "C9? 60 min meetinterval",
    "111": "?? 200",
    "112": "?? 200",
    "113": "?? 200",
    "114": "?? 200",
    "115": "?? bool True",
    "117": "?? 3500 (vermogen? max temp?)",
    "118": "?? 3200",
    "126": "?? 350",
    "127": "?? int 0 - fault-code kandidaat",
    "128": "?? bool",
}


def main() -> None:
    d = tinytuya.Device(DEV["id"], IP, DEV["key"], version=3.5)
    d.set_socketTimeout(10)
    st = d.status()
    if not st or "dps" not in st:
        print("Geen dps-antwoord:", st)
        return
    dps = st["dps"]
    print(json.dumps({k: dps[k] for k in sorted(dps, key=lambda x: int(x))}, indent=2))
    print("\n--- interpretatie ---")
    for k in sorted(dps, key=lambda x: int(x)):
        print(f"  DP {k:>4} = {dps[k]!r:>10}  {LEGEND.get(k, '??')}")


if __name__ == "__main__":
    main()
