# UK Train Departure Display 

## NOW UPDATED TO USE [Real Time Trains API](https://www.realtimetrains.co.uk/about/developer/) !!


This has been done because Transport API now has draconian usage limitations, the free tier is now down to 30 API calls a day from 1000! 

You can still use the Transport API but you will need a commercial agreement. 

A set of python scripts to display replica near real-time UK railway station departure data on SSD13xx style screens.
Uses the publicly available [Real Time Trains API](https://www.realtimetrains.co.uk/about/developer/) 

   * [Installation](#installation)
   * [Configuration](#configuration)
   * [Running](#running)

![](normal.gif)

## Installation

To run this code, you will need Python 3.9+ (tested against the current stable release, Python 3.14).

Raspberry Pi OS ships a recent Python 3 by default; check with `python3 --version`. If you need a newer one, see [here](https://gist.github.com/SeppPenner/6a5a30ebc8f79936fa136c524417761d) for installing an alternative version on Raspbian/Raspberry Pi OS.

You will likely need to set up an alias so that when you type Python you get the latest installed version, a handy guide on how to do this on Raspbin is [here](https://linuxconfig.org/how-to-change-from-default-to-alternative-python-version-on-debian-linux).  If you used the above guide to install the latest Python your path to the executable will be /usr/local/bin/python3.x

>### Desktop emulator (`--display pygame`/`capture`)
>`pygame` may not yet publish a prebuilt wheel for the very latest Python release, in which case `pip` builds it from source. On macOS/Linux that requires SDL2's dev headers (and, for the emulator's on-screen assets to load, `SDL2_image`/`SDL2_ttf`/`SDL2_mixer` too), e.g. on macOS: `brew install sdl2 sdl2_image sdl2_mixer sdl2_ttf`. Without them the build still succeeds but silently lacks PNG support, which breaks the emulator windows (`pygame.error: File is not a Windows BMP file`). This only affects the desktop emulator — real hardware (`ssd1322` over SPI) doesn't use pygame at all.

>### Raspbian Lite
>If you're using Raspbian Lite, you'll also need to install:
>- `libopenjp2-7`
>with:
>```bash
>$ sudo apt-get install libopenjp2-7
>```

Clone this repo

Install dependencies

```bash
$ pip3 install -r requirements.txt
```
>If you installed Python using the above guide you will need to use pip3 instead of pip to install the requirements, if not or if your pip is aliased to python 3.6+ you can just use pip

## Configuration 

Sign up for the [Real Time Trains API](https://api.rtt.io), and get your username and password.

You can still use the old transportAPI (fill in the details and change apiMethod to 'transport') as an alternative but you will either need to have a commercial agreement as the limits on API calls are now so small it is not pratical to use in a real time way. 

Copy `config.sample.json` to `config.json` and complete.

```javascript
{
    "journey": {
      "departureStation": "",
      "destinationStation": null,
      "stationAbbr": {
        "International": "Intl."
      }
    },
    "refreshTime": 180,
    "transportApi": {
      "appId": "",
      "apiKey": "",
      "operatingHours": "0-23"
    },
    "rttApi":{
        "username": "",
        "password": "",
        "operatingHours": "6-23"
      },
      "apiMethod": "rtt"    
  }
```
### General Settings

`refreshTime` - how frequently it asks for new data from the chosen api, there will be two api calls each time this time elapses, be aware the free tier of transport api (not used by default) has 30 calls a day.

### Journey Settings

`departureStation` - the [short code](https://www.nationalrail.co.uk/stations_destinations/48541.aspx) for the starting station 

`destinationStation` - the optional [short code](https://www.nationalrail.co.uk/stations_destinations/48541.aspx) for the destination station 

`stationAbbr` - a list of words and their abbreviations that can be used to shorten station names, useful for small displays. 

### Real Time Trains API Settings (Default)

`username` - your real time trains username

`password` - your real time trains password

`operating hours` - the range of hours you wish the display to actively request train times, be aware the free tier API has a limit of 1000 calls a day.

### Transport API Settings (DONT USE)

`appId` - your transport api application ID

`apiKey` - your transport api key

`operating hours` - the range of hours you wish the display to actively request train times, be aware the free tier API has a limit of 1000 calls a day.

### Setting The Live API

`apiMethod` - By default this is set to 'rtt' to use the Real Time Trains API, to chnage to the transport api set this to 'transport' 

### Reliability Settings (optional)

`retryBackoffSeconds` - if a refresh fails (network blip, API error), the display keeps showing the last good data and retries after these delays in turn, e.g. `[10, 30, 60]` retries after 10s, then 30s, then 60s. Once the list is exhausted it falls back to trying again every `refreshTime` seconds. Defaults to `[10, 30, 60]` if omitted.

`staleAfterSeconds` - if no refresh has succeeded for this long, a small `!` is shown next to the clock so you can tell the board is showing old data rather than live times. Defaults to `refreshTime * 2` if omitted.

### Display Settings (optional)

`display.dimming` - dims the panel outside of your chosen hours (useful for a bedroom/hallway install), independently of `operatingHours`.

```javascript
"display": {
  "dimming": {
    "enabled": false,
    "startHour": 22,
    "endHour": 6,
    "brightness": 10,
    "normalBrightness": 255
  }
}
```

`enabled` - turn the dimming schedule on/off.

`startHour` / `endHour` - the hour range (0-23) during which the panel is dimmed; wraps midnight, so `22` to `6` dims overnight.

`brightness` - contrast level (0-255) to use during the dim window.

`normalBrightness` - contrast level (0-255) to use outside the dim window, defaults to `255`.

Not every display backend supports hardware contrast control (e.g. the desktop emulator); on those, dimming is silently a no-op.

## Running

There is an example run.sh script in the root directory that will start the application and attempt to talk to a SSD1322 display via SPI. 
You will need to adjust this to suit your own requirements

For example 

Change the `--display` flag to alter the output mechanism (a list of options can be found in this README: https://github.com/rm-hull/luma.examples). Use `capture` to save to images, and `pygame` to run a visual emulator.

Pass `--interface spi` if you are using SPI to communicate with your screen. Otherwise, the default of `i2c` should suffice.

```bash
$ python ./src/main.py --display ssd1322 --width 256 --height 64 --interface spi
```

## Example Output

### Normal Operating Hours
![](normal.gif)
### Out Of Hours / No Trains
![](outofhours.gif)

## Video demo

Chris Hutchinson tweeted a video demo of the original software running on a real device: https://twitter.com/chrishutchinson/status/1136743837244768257 I will update this with a video of the modified version at some point in the future.

## Thanks

A big thanks to Chris Hutchinson who originally built this code! He can be found on GitHub at https://github.com/chrishutchinson/

The fonts used were painstakingly put together by `DanielHartUK` and can be found on GitHub at https://github.com/DanielHartUK/Dot-Matrix-Typeface - A huge thanks for making that resource available!
