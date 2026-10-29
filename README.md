[README.md](https://github.com/user-attachments/files/34145257/README.md)

# Pitot Tube Telemetry

<p align="center">
  <img src="https://github.com/user-attachments/assets/42e3d4a1-a3af-4ed0-9fdc-c37d4f9b5c01" alt="Pitot Monitor telemetry dashboard" width="900" />
</p>

A prototype Pitot-static air-data system built around an Arduino Nano and two BMP180 pressure/temperature sensors. The repository contains the Arduino firmware, a Windows telemetry dashboard, circuit documentation, and 3D models of the Pitot tube.

> **Status: experimental prototype.** This project has not been certified or validated as an aircraft instrument. Do not use it as the sole source of airspeed data for flight-critical decisions. Validate the hardware, pressure plumbing, calibration, and readings against a trusted reference before any flight testing.

## Overview

The system reads static pressure and Pitot total pressure using two BMP180 sensors. The Arduino calculates the pressure difference, air density, and estimated airspeed, then streams measurements over USB serial. The desktop dashboard displays live values and plots and records errors reported by the firmware.

### Main features

- Arduino Nano firmware for two BMP180 sensors on separate I²C buses.
- Startup zero-offset calibration while the probe is stationary and both sensors experience the same ambient pressure.
- Temperature-compensated pressure calculations and air-density estimation.
- EMA filtering and a zero-pressure dead band to reduce small fluctuations near zero airspeed.
- Serial telemetry over USB at **115200 baud**.
- Windows dashboard with six live plots, numeric readouts, Arduino/sensor status indicators, an error log, and CSV export.
- CAD source and STL model for the Pitot tube.

## Repository layout

```text
Pitot_tube_telemetry/
├── dashboard/
│   └── PitotMonitor_Windows_v8/
│       └── PitotMonitor/       # Windows dashboard package
├── firmware/                  # Arduino Nano sketch
├── media/                     # Screenshots and project media
├── schematics/                # Circuit diagrams and related files
├── Pitot_tube.f3z              # Fusion 360 archive
├── Pitot_tube_STL.stl          # 3D-printable STL model
└── README.md
```

## Hardware

### Required components

- Arduino Nano compatible board.
- Two BMP180 sensor breakout modules.
- Pitot-static probe and pressure tubing.
- USB cable for programming and serial telemetry.
- Suitable wiring and power supply for the specific sensor modules.

Both BMP180 sensors commonly use the same I²C address (`0x77`), so this design places them on separate buses: one on the Nano's hardware I²C pins and one on a software I²C bus.

### Wiring

| Signal | BMP180 #1 — static pressure | BMP180 #2 — Pitot total pressure |
|---|---|---|
| SDA | Arduino A4 | Arduino D4 |
| SCL | Arduino A5 | Arduino D5 |
| GND | Arduino GND | Arduino GND |
| VCC | According to the breakout module | According to the breakout module |

Connect the static-pressure port to BMP180 #1 and the Pitot/total-pressure port to BMP180 #2. The Arduino and both sensor modules must share a common ground.

**Power and I²C voltage:** a bare BMP180 is a 3.3 V device. Some breakout boards include a regulator and level shifting; others do not. Check the exact module before applying 5 V. I²C pull-up resistors must pull SDA/SCL to a voltage safe for the sensor. Do not add extra pull-ups blindly if the breakout already includes them.

## Arduino firmware

1. Open the sketch located in `firmware/` with the Arduino IDE.
2. Install **SoftwareWire 1.5.1** from the Arduino Library Manager. The current firmware reads BMP180 registers directly, so it does not require `Adafruit BMP085 Library`.
3. Select **Arduino Nano** and the correct processor/bootloader option for your board.
4. Select the correct COM port and upload the sketch.
5. Open Serial Monitor at **115200 baud** to check the startup and telemetry output.

The firmware reads each sensor's factory calibration coefficients from its internal registers. On startup it also performs a software zero-offset calibration using multiple pressure readings. During this procedure, keep the Pitot tube stationary and make sure there is no airflow over the probe; both sensors should see effectively the same ambient pressure.

### Measurement calculation

The firmware calculates the corrected pressure difference as:

```text
ΔP = Pt − Ps − zero_offset
```

Air density is estimated using the ideal-gas relation for dry air:

\[
\rho = \frac{P_s}{R_{air} T}
\]

where `Ps` is static pressure in Pa, `T` is the absolute temperature in kelvin, and `R_air = 287.05 J/(kg·K)`. Estimated airspeed is then:

\[
V = \sqrt{\frac{2\Delta P}{\rho}}
\]

Small negative pressure differences are treated as zero for the airspeed calculation. Filtering and a zero band reduce noise, but they cannot remove the sensor's underlying measurement uncertainty.

## Windows telemetry dashboard

The dashboard package is located at `dashboard/PitotMonitor_Windows_v8/PitotMonitor/`.

1. Connect the Arduino Nano to the computer via USB.
2. Close Arduino IDE's **Serial Monitor** and **Serial Plotter**. A serial port normally cannot be opened by two applications at once.
3. Open the dashboard package and run its included `PitotMonitor.exe` (if the package contains the executable). Keep all accompanying runtime files/folders in place; do not move the executable out of the package directory.
4. Select the Arduino's COM port and connect at **115200 baud**.

The dashboard is intended to provide:

- live numeric values for T1, T2, static pressure Ps, total pressure Pt, airspeed V, and air density ρ;
- plots for both temperatures, both pressures, airspeed, and air density;
- connection/status indicators for the Arduino and each BMP180, based on firmware telemetry;
- an error log and CSV data export;
- mouse-based graph zooming (where supported by the packaged dashboard): wheel to zoom, `Ctrl` + wheel for the vertical axis, `Shift` + wheel for the time axis, and double-click to restore automatic scaling.

### Serial data format

A normal measurement line has this key-value format:

```text
T1=25.9 T2=25.9 Ps=99562 Pt=99578 dP=16.0 V=5.25 rho=1.160 ERR=0
```

The numbers above are an **illustrative example**, not guaranteed sensor readings. The fields are:

| Field | Meaning | Unit |
|---|---|---|
| `T1` | Temperature of the static-pressure sensor | °C |
| `T2` | Temperature of the Pitot total-pressure sensor | °C |
| `Ps` | Static pressure | Pa |
| `Pt` | Pitot total pressure | Pa |
| `dP` | Corrected/filtered differential pressure | Pa |
| `V` | Estimated airspeed | m/s |
| `rho` | Estimated air density | kg/m³ |
| `ERR` | Current firmware error code | — |

The firmware may also emit standalone diagnostic lines such as `ERROR=2 PITOT SENSOR` when the error state changes. The dashboard's per-sensor status is based on the telemetry sent by the Arduino; the PC does not independently query the sensors.

## Error codes

| Code | Meaning | First checks |
|---:|---|---|
| `0` | Normal operation (`OK`) | No action required |
| `1` | Static BMP180 problem | Check sensor #1 wiring and I²C bus A4/A5 |
| `2` | Pitot BMP180 problem | Check sensor #2 wiring and I²C bus D4/D5 |
| `3` | Zero calibration failed | Keep the probe still; check that both pressure ports see the same ambient pressure |
| `4` | Measurement/read error | Inspect sensor wiring, connections, and power |
| `5` | Pressure value outside the accepted range | Check sensor readings and pressure plumbing |
| `6` | Temperature invalid/out of range | Check sensor communication and returned values |
| `7` | Air-density calculation invalid | Check the pressure and temperature inputs |

## Troubleshooting

### The dashboard says the COM port cannot be opened / “Access is denied”

Close Arduino IDE Serial Monitor/Serial Plotter and any other terminal or dashboard instance using the same COM port. Refresh the port list in the dashboard and reconnect.

### The dashboard cannot find the Arduino

- Check the USB cable (some cables provide power only and do not carry data).
- Check Windows Device Manager for the assigned COM port and USB-serial driver.
- Choose the same port in the dashboard that appears in Device Manager.
- Confirm the firmware is uploaded and the dashboard uses 115200 baud.

### The firmware reports `ERROR=2 PITOT SENSOR`

The software I²C bus did not successfully initialize/read the second BMP180. Check `SDA → D4`, `SCL → D5`, common GND, module power, pull-ups, and the `0x77` address. A standalone software-I²C test that reads `CHIP ID = 0x55` confirms the device responds on that bus, but does not by itself prove every subsequent register transaction is error-free.

### Airspeed spikes while the probe is stationary

The firmware obtains differential pressure by subtracting two **absolute-pressure** BMP180 measurements. Noise and offset from both sensors appear in the difference, and airspeed depends on the square root of that small pressure difference. A few pascals of residual pressure noise can therefore look like several metres per second near zero. Startup zeroing and filtering help, but this sensor arrangement is not a precision differential-pressure measurement.

For improved low-speed accuracy, consider a suitable differential pressure sensor and validate the whole system against a known reference. Do not rely on this prototype as a flight-critical airspeed instrument without testing and independent validation.

## CAD files

- `Pitot_tube.f3z` — Fusion 360 archive for the editable model, if compatible with your Fusion 360 version.
- `Pitot_tube_STL.stl` — mesh model suitable for slicing and 3D printing. Check dimensions, wall thickness, sealing, and tube-port alignment before manufacturing.

## Limitations and safety

- BMP180 sensors are absolute pressure sensors, not a matched differential-pressure pair.
- Sensor mismatch, temperature drift, vibration, pneumatic leaks, tubing layout, airflow disturbance, and pressure-port alignment can affect the calculated airspeed.
- The speed equation above is an incompressible-flow approximation; compressibility corrections may be required at higher airspeeds.
- The project is experimental and is not an approved or certified aviation instrument. Perform bench testing and comparison with calibrated equipment before considering any operational use.
