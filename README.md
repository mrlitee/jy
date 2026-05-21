# Pedaku — Web-based Motorcycle Diagnostic (ELM327)

Aplikasi web untuk diagnosa sepeda motor multi-merek (Honda, Yamaha, Suzuki, Kawasaki, KTM, BMW, Ducati) lewat adaptor **ELM327** (USB / Bluetooth / WiFi).

Backend: Python + Flask. Frontend: vanilla HTML/CSS/JS — tanpa framework, tanpa CDN, ringan.

> Tujuan utama: **bisa jalan di Termux/Android** dan diakses dari browser HP yang sama (atau HP lain di WiFi).

## Fitur

- Single-page UI dengan tabs: Connect, Dashboard, Live Chart, DTC, Live Test, Raw, Help
- **Real-time streaming via Server-Sent Events** — UI update mengikuti data ECU, bukan polling timer (no delay)
- **Adaptive sampler**: RPM/TPS @ 10 Hz, MAP/Speed @ 5 Hz, suhu/baro @ 0.2–1 Hz
- **Priority I/O queue**: klik DTC scan / actuator test mendahului sampling, respons instan
- Auto-protocol detection (ISO 9141, KWP2000, ISO 15765 CAN)
- 15 PID live (RPM, TPS, MAP, ECT, IAT, O2, lambda, fuel trim, dll.)
- **Live Chart** multi-series dengan window 15s/60s/3m/10m, hover tooltip — tanpa CDN/Chart.js
- **Sparkline** mini di setiap gauge tile
- DTC read/clear (current/pending/permanent) dengan database multi-merek
- **Freeze Frame** (Mode 02) — snapshot kondisi saat DTC tersimpan
- **Health Score** otomatis dari DTC count + suhu + tegangan + fuel trim
- **PDF Report** — VIN, ECU, DTC, snapshot live data, health score
- Active actuator tests (fuel pump, ISC, fan, injector, coil, MIL)
- Raw command sender (AT + OBD-II) untuk debug
- **Demo / Simulator mode** — coba seluruh UI tanpa adapter ELM327
- Mobile-friendly (responsive, dark theme)

## Instalasi

```bash
git clone https://github.com/mrlitee/pedaku.git
cd pedaku
pip install -r requirements.txt
python run_web.py
```

Lalu buka di browser:
- HP Anda sendiri: `http://localhost:5000`
- HP/laptop lain di WiFi yang sama: `http://<ip-termux>:5000`

Cek IP Termux:
```bash
ifconfig | grep inet
```

## Cara Konek ke ELM327

### A) WiFi ELM327 (paling mudah)
1. Sambungkan HP ke WiFi adapter (`WiFi_OBDII`)
2. Di tab Connect: kind = **TCP**, address = `192.168.0.10:35000`, pilih brand → Connect

### B) Bluetooth native (Linux desktop / Termux rooted)
Pakai stack BlueZ tanpa bridge app, tanpa `rfcomm bind` manual.
1. Pair adapter sekali via `bluetoothctl`:
   ```bash
   bluetoothctl
   power on
   agent on
   scan on
   pair AA:BB:CC:DD:EE:FF
   trust AA:BB:CC:DD:EE:FF
   exit
   ```
   PIN umumnya `1234` atau `0000`.
2. Di tab Connect: kind = **Bluetooth**, klik **Scan paired devices** dan pilih dari dropdown — atau ketik MAC manual `AA:BB:CC:DD:EE:FF`. Tombol **Connect** akan membuka RFCOMM channel 1.
3. Adapter pakai channel non-default? Tambahkan `@<channel>` di address, mis. `AA:BB:CC:DD:EE:FF@2`.

> Native Bluetooth membutuhkan `socket.AF_BLUETOOTH` (Linux/Termux dengan BlueZ). Di Termux non-root jalur ini **tidak akan jalan** karena Android tidak mengizinkan akses langsung ke socket Bluetooth dari user-space — pakai opsi C berikut.

### C) Bluetooth via bridge app (Termux non-root)
1. Pair ELM327 di Settings Android (PIN `1234`)
2. Install bridge app dari Play Store: cari "**Bluetooth TCP bridge**"
3. Konfigurasi bridge → mode TCP server di port `35000`
4. Di tab Connect: kind = **TCP**, address = `127.0.0.1:35000`, brand → Connect

### D) rfcomm bind manual (Termux rooted, alternatif)
```bash
sudo rfcomm bind 0 AA:BB:CC:DD:EE:FF 1
sudo chmod 666 /dev/rfcomm0
```
Di tab Connect: kind = **Serial**, address = `/dev/rfcomm0`

### E) USB OTG
Di tab Connect: kind = **Serial**, address = `/dev/ttyUSB0` (perlu permission)

## Struktur

```
pedaku/
├── run_web.py                  entry point
├── requirements.txt            flask + pyserial
├── src/pedaku/
│   ├── server.py               Flask app + REST API
│   ├── core/                   ELM327 driver, protocols, PID, DTC, live test
│   │   ├── elm327.py
│   │   ├── transport.py
│   │   ├── protocols.py
│   │   ├── obd_pid.py
│   │   ├── dtc.py
│   │   ├── live_test.py
│   │   └── session.py
│   ├── data/                   DTC databases per brand (JSON)
│   └── utils/
├── templates/index.html        single-page UI
└── static/
    ├── style.css               dark theme
    └── app.js                  frontend logic
```

## REST API

| Method | Path | Description |
|---|---|---|
| GET | `/api/brands` | list brand profiles |
| GET | `/api/state` | connection state + cached live snapshot |
| GET | `/api/health` | derived 0–100 health score |
| GET | `/api/bluetooth/scan` | list paired Bluetooth devices (best-effort) |
| POST | `/api/connect` | body: `{kind, address, brand}` — `kind` ∈ `tcp` \| `bluetooth` \| `serial` \| `demo` |
| POST | `/api/disconnect` | |
| GET | `/api/dtcs` | list DTCs |
| POST | `/api/dtcs/clear` | clear all DTCs |
| GET | `/api/freeze` | Mode 02 freeze frame snapshot |
| GET | `/api/pids/all` | cached snapshot of all live PIDs |
| GET | `/api/pids/meta` | metadata for all live PIDs |
| GET | `/api/pid/<code>` | one PID (cached, falls back to one-shot read if stale) |
| GET | `/api/history?count=120` | recent ring-buffer for all PIDs |
| GET | `/api/stream` | **Server-Sent Events** — live PID samples |
| GET | `/api/report` | downloadable PDF diagnostic report |
| GET | `/api/tests` | catalog of active tests |
| POST | `/api/test/<idx>/start` | run test |
| POST | `/api/test/<idx>/stop` | stop test |
| POST | `/api/raw` | body: `{cmd}` raw OBD/AT |

## Disclaimer

Live test mengaktifkan aktuator nyata. Lakukan dengan motor diam, dudukan stabil. Tegangan aki ≥11V. Beberapa ELM327 clone tidak reliable di KWP2000.

## Lisensi

MIT
