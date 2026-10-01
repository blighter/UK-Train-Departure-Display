# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Python app that renders a UK station departure board on an SSD13xx OLED (256x64, driven over SPI from a Raspberry Pi) using the luma.oled/luma.core libraries. Departure data comes from the next-generation Real Time Trains API at `https://data.rtt.io` (`apiMethod: "rtt"`, the default), authenticated with a Bearer token; the original `api.rtt.io` username/password service is shut down. The old TransportAPI path is kept but is effectively unusable on the free tier. There are no tests and no linter config.

## Commands

```bash
pip3 install -r requirements.txt          # current stable releases (Pillow 12, luma.core 2.6); needs Python 3.9+
cp config.sample.json config.json         # gitignored; add RTT credentials + departureStation (CRS code)
./run.sh                                  # real hardware: --display ssd1322 --interface spi --rotate 2
python3 ./src/main.py --display pygame --width 256 --height 64    # desktop emulator, no hardware
python3 ./src/main.py --display capture --width 256 --height 64   # writes frames to images
```

- **Run from the repo root.** `config.json` is opened by relative path and `src/main.py` is not runnable from inside `src/`. `systemd/traindep.service` sets `WorkingDirectory` for the same reason.
- Display flags (`--display`, `--interface`, `--width`, ...) are parsed by `luma.core.cmdline` in `src/helpers.py:get_device`, not by this project.
- `pygame`, `RPi.GPIO` and `spidev` are in `requirements.txt`, so the install only works on a Pi or needs those lines skipped on a laptop. `RPi.GPIO`/`spidev` are Linux-only and will fail to build on macOS/Windows — expected, not a regression.
- If pip has to build `pygame` from source (no wheel yet for the newest Python), the emulator's on-screen PNG assets need `SDL2_image`/`SDL2_ttf`/`SDL2_mixer` dev libs present at build time (`brew install sdl2 sdl2_image sdl2_mixer sdl2_ttf` on macOS) or `pygame.error: File is not a Windows BMP file` results. See README.

## Architecture

Four modules in `src/`, all flat imports (`from trains import ...`):

- `main.py`: entry point. There is **no `main()` or `__main__` guard**. Everything from `try:` at the bottom of the file runs at import, and the render functions read module-level globals (`font`, `fontBold`, `fontBoldLarge`, `fontBoldTall`, `stationRenderCount`, `pauseCount`) that are assigned inside that `try` block. Anything you extract or reorder must keep those globals defined before the first draw.
- `trains.py`: two parallel API implementations (`*RTT` and the TransportAPI originals). Both return the same normalised departure dict (`aimed_departure_time`, `expected_departure_time`, `destination_name`, `status`, `mode`, `platform`, plus `delay_reason`/`uid` for RTT). The renderers depend only on that shape, so a new data source means producing it.
  - **RTT (data.rtt.io)**: `getRttToken(apiConfig)` returns a Bearer token — either the configured long-life `rttApi.token`, or a cached access token minted from `rttApi.refreshToken` via `/api/get_access_token`. `loadDeparturesForStationRTT` calls `/rtt/location?code=<CRS>` and maps `services[]` (scheduleMetadata / temporalData.departure / locationMetadata / destination / reasons); `loadDestinationsForDepartureRTT` calls `/rtt/service?uniqueIdentity=<uid>` and only advertises locations whose `temporalData.displayAs` is CALL/STARTS/TERMINATES. HTTP failures are raised as `ApiError(title, detail)` with strings short enough to render on the display; `main.describeError` maps other exceptions.
- `open.py`: `isRun(start_hour, end_hour)` gates API calls to `operatingHours`, including ranges that wrap midnight.
- `helpers.py`: luma device construction from CLI args.

### Render loop

`loadDataRTT`/`loadData` return a `(departures, callingAtList, stationName)` tuple. `data[0] == False` selects the blank "Welcome to <station> ... time" screen (`drawBlankSignage`), otherwise `drawSignage` builds the board; `buildView` dispatches between them. All three screens build a luma `viewport` from `snapshot` hotspots, one per row/column region; each snapshot has its own redraw `interval` (seconds; the scrolling "Calling at" text uses 0.1, the clock 1, static text 10). The `while True` loop calls `virtual.refresh()` continuously and only rebuilds the whole viewport when it actually re-fetches, which is the only time the API is hit.

The loop is deliberately crash-proof: state lives in `haveData`/`lastData`/`currentError`/`shownError`, and `fetchData` is wrapped in a broad `except`. On failure it keeps the last good board (the clock shows `!` once `isStale`), schedules a retry with `retryBackoffSeconds`, and — once the data is stale, or if nothing ever loaded — swaps to a friendly `drawMessageSignage` screen. `describeError` turns exceptions into a short `(title, detail)` for that screen. The calling-points call is also wrapped in `loadDataRTT` so a failure there just drops the scroller rather than the whole board.

The "Calling at" scroller is stateful: `renderStations` advances the global `stationRenderCount` each time it is drawn, pausing for 8 frames at the start; `drawSignage` resets both counters on every rebuild.

Layout is hard-coded for 64px height: rows at y = 0/12/24/36 and the clock at y = 50. Only the first 3 departures are rendered (RTT fetches 5) and only the first departure gets a calling-points list, which costs a second API call per refresh.

## Gotchas

- `journeyConfig['outOfHoursName']` is read by both `loadData*` functions whenever the display is out of hours (and displayed when RTT finds no services — for RTT the blank screen now shows the API's full location description, falling back to the CRS code). It is in `config.sample.json` and `validateConfig` rejects a `config.json` without it up front with a message naming the key.
- `draw.textsize` was removed in Pillow 10. `main.py` now has a local `textsize(draw, text, font)` helper built on `textbbox` that every call site uses instead — keep new text-measuring code going through it rather than calling `draw.textsize` directly.
- `timeloop` was an unused import (imported, never instantiated) and has been dropped from `requirements.txt`; the render loop is the plain `while True` at the bottom of `main.py`.
- API/network failures during a refresh no longer end the process: the loop catches everything, retries with a backoff, and shows a friendly on-screen message (see the render loop notes). Top-level `except` clauses still catch startup/config/device errors; a `DeviceError` prints an SPI/wiring hint.
- RTT parsing treats a missing/null `services` array (or an HTTP 204) as "no trains" and reads the first `destination` entry. `modeType` is lower-cased and collapsed so `BUS`/`SCHEDULED_BUS`/`REPLACEMENT_BUS` all render as a bus, and `status` is normalised to `CANCELLED` when `temporalData.departure.isCancelled` is set.
- `3d-printed-case/` (OpenSCAD + STL) and `assets/` (pinout images) are hardware reference only.
