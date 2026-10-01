import os
import sys
import time
import json
import requests
import qrcode

from datetime import datetime
from PIL import ImageFont, ImageDraw, Image
from helpers import get_device
from trains import (loadDeparturesForStation, loadDestinationsForDeparture,
                    loadDeparturesForStationRTT, loadDestinationsForDepartureRTT,
                    getRttToken, ApiError)
from luma.core.error import Error as DeviceError
from luma.core.render import canvas
from luma.core.virtual import viewport, snapshot
from open import isRun

def loadConfig():
    with open('config.json', 'r') as jsonConfig:
        data = json.load(jsonConfig)
        return data

def validateOperatingHours(value, name):
    if not value:
        raise ValueError(f"Missing '{name}' in config.json")
    try:
        start, end = [int(x) for x in value.split('-')]
    except (ValueError, AttributeError):
        raise ValueError(f"config.json '{name}' must be formatted like '6-23'")
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise ValueError(f"config.json '{name}' hours must be between 0 and 23")

def validateConfig(config):
    # Catches a bad/missing key up front with a message that names the
    # actual config path, rather than letting it surface later as a bare
    # KeyError once the render loop is already running (see the
    # outOfHoursName gotcha in CLAUDE.md).
    if 'journey' not in config:
        raise ValueError("Missing 'journey' section in config.json")

    journey = config['journey']

    if not journey.get('departureStation'):
        raise ValueError(
            "Please set the journey.departureStation property in config.json")

    if 'outOfHoursName' not in journey:
        raise ValueError(
            "Missing journey.outOfHoursName in config.json - see config.sample.json "
            "(this is the text shown on the blank screen outside operating hours, "
            "or when no services are running)")

    refreshTime = config.get('refreshTime')
    if not isinstance(refreshTime, (int, float)) or isinstance(refreshTime, bool) or refreshTime <= 0:
        raise ValueError("config.json 'refreshTime' must be a positive number of seconds")

    # apiMethod only ever gets compared with == 'rtt' at runtime - anything
    # else falls through to the transportApi path - so validation follows
    # the same rule rather than rejecting values the app would accept.
    if config.get('apiMethod') == 'rtt':
        rttApi = config.get('rttApi', {})
        if not (rttApi.get('token') or rttApi.get('refreshToken')):
            if rttApi.get('username') or rttApi.get('password'):
                raise ValueError(
                    "The Real Time Trains API moved to data.rtt.io and now uses a "
                    "token instead of a username/password. Replace "
                    "rttApi.username/rttApi.password in config.json with "
                    "rttApi.token (or rttApi.refreshToken). Get one at "
                    "https://api-portal.rtt.io")
            raise ValueError(
                "Please complete the rttApi section of your config.json file "
                "(set rttApi.token or rttApi.refreshToken)")
        validateOperatingHours(rttApi.get('operatingHours'), 'rttApi.operatingHours')
    else:
        transportApi = config.get('transportApi', {})
        if not transportApi.get('appId') or not transportApi.get('apiKey'):
            raise ValueError(
                "Please complete the transportApi section of your config.json file")
        validateOperatingHours(transportApi.get('operatingHours'), 'transportApi.operatingHours')

    retryBackoffSeconds = config.get('retryBackoffSeconds')
    if retryBackoffSeconds is not None:
        if not isinstance(retryBackoffSeconds, list) or not all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0
                for x in retryBackoffSeconds):
            raise ValueError(
                "config.json 'retryBackoffSeconds' must be a list of positive numbers")

    staleAfterSeconds = config.get('staleAfterSeconds')
    if staleAfterSeconds is not None:
        if not isinstance(staleAfterSeconds, (int, float)) or isinstance(staleAfterSeconds, bool) or staleAfterSeconds <= 0:
            raise ValueError("config.json 'staleAfterSeconds' must be a positive number of seconds")

    dimming = config.get('display', {}).get('dimming')
    if dimming and dimming.get('enabled'):
        for key in ('startHour', 'endHour'):
            value = dimming.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 23):
                raise ValueError(
                    f"config.json 'display.dimming.{key}' must be an hour between 0 and 23")
        if 'brightness' not in dimming:
            raise ValueError(
                "Missing 'display.dimming.brightness' in config.json (the contrast "
                "level, 0-255, to use during the dim window)")
        # normalBrightness is optional and defaults to 255 (full brightness).
        for key in ('brightness', 'normalBrightness'):
            if key not in dimming:
                continue
            value = dimming[key]
            if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 255):
                raise ValueError(
                    f"config.json 'display.dimming.{key}' must be between 0 and 255")

    splash = config.get('display', {}).get('splash')
    if splash:
        durationSeconds = splash.get('durationSeconds')
        if durationSeconds is not None:
            if not isinstance(durationSeconds, (int, float)) or isinstance(durationSeconds, bool) or durationSeconds <= 0:
                raise ValueError(
                    "config.json 'display.splash.durationSeconds' must be a positive number of seconds")

def textsize(draw, text, font):
    # Pillow >=10 removed ImageDraw.textsize(); textbbox is the replacement.
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top

def makeFont(name, size):
    font_path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            'fonts',
            name
        )
    )
    return ImageFont.truetype(font_path, size)


def getDimmingConfig(config):
    dimming = config.get('display', {}).get('dimming')
    if not dimming or not dimming.get('enabled'):
        return None
    return dimming


# Shown once at startup (see drawSplashScreen) - on by default so the
# feature isn't invisible to configs predating it; add
# display.splash.enabled: false to config.json to turn it off.
DEFAULT_SPLASH_CONFIG = {
    'enabled': True,
    'durationSeconds': 5,
    'message': 'Created by blighter',
    'url': 'https://github.com/blighter/UK-Train-Departure-Display',
    'showQrCode': True,
}


def getSplashConfig(config):
    splash = {**DEFAULT_SPLASH_CONFIG, **config.get('display', {}).get('splash', {})}
    if not splash.get('enabled'):
        return None
    return splash


def setContrast(device, level):
    try:
        device.contrast(level)
    except (AttributeError, DeviceError) as err:
        # Not every luma backend (e.g. the desktop emulator, or a display
        # that doesn't support SETCONTRAST) can do hardware dimming -
        # that just means the dimming schedule has no visible effect.
        print(f"Warning: could not set display contrast ({err}) - dimming schedule will have no effect")


def renderDestination(departure, font):
    departureTime = departure["aimed_departure_time"]
    destinationName = departure["destination_name"]

    def drawText(draw, width, height):
        train = f"{departureTime}  {destinationName}"
        draw.text((0, 0), text=train, font=font, fill="yellow")

    return drawText


def renderServiceStatus(departure):
    def drawText(draw, width, height):
        train = ""

        status = departure.get("status") or ""
        if status.startswith("CANCELLED") or status == "DIVERTED":
            train = "Cancelled"
        else:
            if isinstance(departure["expected_departure_time"], str):
                train = 'Exp '+departure["expected_departure_time"]

            if departure["aimed_departure_time"] == departure["expected_departure_time"]:
                train = "On time"

        w, h = textsize(draw, train, font)
        draw.text((width-w,0), text=train, font=font, fill="yellow")
    return drawText

def renderPlatform(departure):
    def drawText(draw, width, height):
        if "bus" in (departure["mode"] or ""):
            draw.text((0, 0), text="BUS", font=font, fill="yellow")
        else:
            if departure["platform"]:
                draw.text((0, 0), text="Plat "+departure["platform"], font=font, fill="yellow")
    return drawText

def renderLabel(text, labelFont=None):
    def drawText(draw, width, height):
        draw.text((0, 0), text=text, font=labelFont or font, fill="yellow")
    return drawText


def renderStations(stations):
    def drawText(draw, width, height):
        global stationRenderCount, pauseCount

        if(len(stations) == stationRenderCount - 5):
            stationRenderCount = 0

        draw.text(
            (0, 0), text=stations[stationRenderCount:], width=width, font=font, fill="yellow")

        if stationRenderCount == 0 and pauseCount < 8:
            pauseCount += 1
            stationRenderCount = 0
        else:
            pauseCount = 0
            stationRenderCount += 1

    return drawText

def renderTime(draw, width, height):
    rawTime = datetime.now().time()
    hour, minute, second = str(rawTime).split('.')[0].split(':')

    w1, h1 = textsize(draw, "{}:{}".format(hour, minute), fontBoldLarge)
    w2, h2 = textsize(draw, ":00", fontBoldTall)

    draw.text(((width - w1 - w2) / 2, 0), text="{}:{}".format(hour, minute),
              font=fontBoldLarge, fill="yellow")
    draw.text((((width - w1 -w2) / 2) + w1, 5), text=":{}".format(second),
              font=fontBoldTall, fill="yellow")

    # isStale is set in the refresh loop when data hasn't been fetched
    # successfully within staleAfterSeconds - flag it in the corner rather
    # than silently keep showing an old board as if it were current.
    if isStale:
        staleText = "!"
        sw, sh = textsize(draw, staleText, fontBold)
        draw.text((width - sw, 0), text=staleText, font=fontBold, fill="yellow")


def renderWelcomeTo(xOffset):
    def drawText(draw, width, height):
        text = "Welcome to"
        draw.text((int(xOffset), 0), text=text, font=fontBold, fill="yellow")

    return drawText


def renderDepartureStation(departureStation, xOffset):
    def draw(draw, width, height):
        text = departureStation
        draw.text((int(xOffset), 0), text=text, font=fontBold, fill="yellow")

    return draw


def renderDots(draw, width, height):
    text = ".  .  ."
    draw.text((0, 0), text=text, font=fontBold, fill="yellow")


def loadData(apiConfig, journeyConfig):
    runHours = [int(x) for x in apiConfig['operatingHours'].split('-')]
    if isRun(runHours[0], runHours[1]) == False:
        return False, False, journeyConfig['outOfHoursName']

    departures, stationName = loadDeparturesForStation(
        journeyConfig, apiConfig["appId"], apiConfig["apiKey"])

    if len(departures) == 0:
        return False, False, stationName

    firstDepartureDestinations = loadDestinationsForDeparture(
        journeyConfig, departures[0]["service_timetable"]["id"])

    return departures, firstDepartureDestinations, stationName

def loadDataRTT(apiConfig, journeyConfig):
    runHours = [int(x) for x in apiConfig['operatingHours'].split('-')]
    if isRun(runHours[0], runHours[1]) == False:
        return False, False, journeyConfig['outOfHoursName']

    token = getRttToken(apiConfig)

    departures, stationName = loadDeparturesForStationRTT(journeyConfig, token)

    if len(departures) == 0:
        return False, False, stationName

    try:
        firstDepartureDestinations = loadDestinationsForDepartureRTT(
            journeyConfig, token, departures[0]["uid"])
    except (ApiError, requests.exceptions.RequestException) as err:
        # The calling-at list is a nice-to-have second call; never lose the
        # whole board because this one failed.
        print(f"Warning: could not load calling points ({err})")
        firstDepartureDestinations = []

    return departures, firstDepartureDestinations, stationName

def fetchData(config):
    if config["apiMethod"] == 'rtt':
        return loadDataRTT(config["rttApi"], config["journey"])
    return loadData(config["transportApi"], config["journey"])

def wrapText(draw, text, font, maxWidth):
    # Character-by-character rather than word-wrapping - the splash screen's
    # longest text is a URL, which has no spaces to break on anyway.
    lines = []
    current = ""
    for ch in text:
        candidate = current + ch
        if current and textsize(draw, candidate, font)[0] > maxWidth:
            lines.append(current)
            current = ch
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def buildQrImage(url):
    # box_size=1 (one pixel per module) is as crisp as this gets - the
    # panel is only 64px tall, so there's no headroom to draw modules any
    # bigger once the url needs more than a handful of QR versions.
    #
    # Dark modules on a lit background (not the reverse) - most scanners,
    # including zbar, don't recognise a light-on-dark ("inverted") code, so
    # this has to be a bright yellow square with black modules rather than
    # yellow modules on the display's usual black background.
    qr = qrcode.QRCode(border=1, box_size=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="yellow")


def drawSplashScreen(device, width, height, splashConfig):
    message = splashConfig.get('message') or "Created by blighter"
    url = splashConfig.get('url')
    showQrCode = bool(splashConfig.get('showQrCode') and url)

    image = Image.new(device.mode, (width, height), "black")
    draw = ImageDraw.Draw(image)

    textX = 4
    if showQrCode:
        qrImage = buildQrImage(url)
        qrSize = qrImage.size[0]
        if qrSize <= height - 4:
            image.paste(qrImage.convert(device.mode), (4, (height - qrSize) // 2))
            textX = 4 + qrSize + 8
        # else: a longer/custom url needed a QR version too big for this
        # display's 64px height - fall back to text-only rather than show a
        # code cropped down to something unscannable.

    maxTextWidth = width - textX - 2
    y = 4

    for line in wrapText(draw, message, fontBold, maxTextWidth):
        draw.text((textX, y), text=line, font=fontBold, fill="yellow")
        y += 12

    if url:
        y += 3
        for line in wrapText(draw, url, font, maxTextWidth):
            if y > height - 10:
                break
            draw.text((textX, y), text=line, font=font, fill="yellow")
            y += 11

    device.display(image)


def drawBlankSignage(device, width, height, departureStation):
    global stationRenderCount, pauseCount

    with canvas(device) as draw:
        welcomeSize = textsize(draw, "Welcome to", fontBold)

    with canvas(device) as draw:
        stationSize = textsize(draw, departureStation, fontBold)

    device.clear()

    virtualViewport = viewport(device, width=width, height=height)

    rowOne = snapshot(width, 10, renderWelcomeTo(
        (width - welcomeSize[0]) / 2), interval=10)
    rowTwo = snapshot(width, 10, renderDepartureStation(
        departureStation, (width - stationSize[0]) / 2), interval=10)
    rowThree = snapshot(width, 10, renderDots, interval=10)
    rowTime = snapshot(width, 14, renderTime, interval=1)

    if len(virtualViewport._hotspots) > 0:
        for hotspot, xy in virtualViewport._hotspots:
            virtualViewport.remove_hotspot(hotspot, xy)

    virtualViewport.add_hotspot(rowOne, (0, 0))
    virtualViewport.add_hotspot(rowTwo, (0, 12))
    virtualViewport.add_hotspot(rowThree, (0, 24))
    virtualViewport.add_hotspot(rowTime, (0, 50))

    return virtualViewport


def drawMessageSignage(device, width, height, title, detail):
    # Friendly full-screen state shown while connecting, or when the API has
    # been unreachable long enough that stale times shouldn't be trusted.
    # The clock keeps ticking so the board never looks frozen.
    device.clear()

    measureImage = Image.new(device.mode, (width, height), "black")
    measureDraw = ImageDraw.Draw(measureImage)
    titleLines = wrapText(measureDraw, title, fontBold, width - 4)[:2]
    detailLines = wrapText(measureDraw, detail, font, width - 4)

    virtualViewport = viewport(device, width=width, height=height)

    y = 0
    for line in titleLines:
        virtualViewport.add_hotspot(
            snapshot(width, 10, renderLabel(line, fontBold), interval=10), (0, y))
        y += 11

    y += 1
    for line in detailLines:
        if y > 38:
            break
        virtualViewport.add_hotspot(
            snapshot(width, 10, renderLabel(line, font), interval=10), (0, y))
        y += 11

    rowTime = snapshot(width, 14, renderTime, interval=1)
    virtualViewport.add_hotspot(rowTime, (0, 50))

    return virtualViewport


def buildView(device, width, height, data):
    if data[0] == False:
        return drawBlankSignage(
            device, width=width, height=height, departureStation=data[2])
    return drawSignage(device, width=width, height=height, data=data)


def describeError(err):
    # Returns a (title, detail) pair short enough to read on the OLED.
    if isinstance(err, ApiError):
        return err.title, err.detail
    if isinstance(err, requests.exceptions.Timeout):
        return "No connection", "The request to Real Time Trains timed out"
    if isinstance(err, requests.exceptions.ConnectionError):
        return "No connection", "Check the display's network connection"
    if isinstance(err, requests.exceptions.RequestException):
        return "Train times unavailable", "Could not reach Real Time Trains"
    if isinstance(err, ValueError):
        return "Unexpected response", "Real Time Trains sent unreadable data"
    return "Something went wrong", "The board will keep trying"


def drawSignage(device, width, height, data):
    global stationRenderCount, pauseCount

    device.clear()

    virtualViewport = viewport(device, width=width, height=height)

    status = "Exp 00:00"

    departures, firstDepartureDestinations, departureStation = data

    # A delayed/cancelled first departure gets its reason scrolled in place
    # of the calling points list - more useful than a calling-at list nobody
    # will reach.
    delayReason = departures[0].get('delay_reason') if departures else None
    scrollLabel = "Reason:" if delayReason else "Calling at:"
    scrollText = delayReason if delayReason else ", ".join(firstDepartureDestinations)

    with canvas(device) as draw:
        w, h = textsize(draw, scrollLabel, font)

    callingWidth = w
    width = virtualViewport.width

    # First measure the text size
    with canvas(device) as draw:
        w, h = textsize(draw, status, font)
        pw, ph = textsize(draw, "Plat 88", font)

    if len(virtualViewport._hotspots) > 0:
        for hotspot, xy in virtualViewport._hotspots:
            virtualViewport.remove_hotspot(hotspot, xy)

    stationRenderCount = 0
    pauseCount = 0

    # Row y=0 is the next departure (in bold); y=12 is the "Calling at"
    # scroller below; y=24/36 show the two departures after that, if any.
    # Only the first 3 departures are ever shown, even though the API
    # returns more.
    departureRows = [(0, fontBold), (24, font), (36, font)]

    for departure, (y, departureFont) in zip(departures, departureRows):
        destinationHotspot = snapshot(
            width - w - pw, 10, renderDestination(departure, departureFont), interval=10)
        statusHotspot = snapshot(w, 10, renderServiceStatus(departure), interval=1)
        platformHotspot = snapshot(pw, 10, renderPlatform(departure), interval=10)

        virtualViewport.add_hotspot(destinationHotspot, (0, y))
        virtualViewport.add_hotspot(statusHotspot, (width - w, y))
        virtualViewport.add_hotspot(platformHotspot, (width - w - pw, y))

    rowTwoA = snapshot(callingWidth, 10, renderLabel(scrollLabel), interval=100)
    rowTwoB = snapshot(width - callingWidth, 10,
                       renderStations(scrollText), interval=0.1)
    virtualViewport.add_hotspot(rowTwoA, (0, 12))
    virtualViewport.add_hotspot(rowTwoB, (callingWidth, 12))

    rowTime = snapshot(width, 14, renderTime, interval=1)
    virtualViewport.add_hotspot(rowTime, (0, 50))

    return virtualViewport


try:
    config = loadConfig()
    validateConfig(config)

    device = get_device()
    font = makeFont("Dot Matrix Regular.ttf", 10)
    fontBold = makeFont("Dot Matrix Bold.ttf", 10)
    fontBoldTall = makeFont("Dot Matrix Bold Tall.ttf", 10)
    fontBoldLarge = makeFont("Dot Matrix Bold.ttf", 20)

    widgetWidth = 256
    widgetHeight = 64

    stationRenderCount = 0
    pauseCount = 0
    loop_count = 0

    refreshTime = config["refreshTime"]
    # How long to wait before retrying after a failed refresh, backing off
    # each attempt; once these are exhausted, fall back to trying again on
    # the normal refreshTime cadence rather than hammering the API.
    retryBackoffSeconds = config.get("retryBackoffSeconds", [10, 30, 60])
    # If nothing has refreshed successfully for this long, the board is
    # considered stale (see isStale / renderTime).
    staleAfterSeconds = config.get("staleAfterSeconds") or (refreshTime * 2)

    dimmingConfig = getDimmingConfig(config)
    isDimmed = False
    if dimmingConfig:
        isDimmed = isRun(dimmingConfig["startHour"], dimmingConfig["endHour"])
        setContrast(device, dimmingConfig["brightness"] if isDimmed
                    else dimmingConfig.get("normalBrightness", 255))

    splashConfig = getSplashConfig(config)
    if splashConfig:
        drawSplashScreen(device, width=widgetWidth, height=widgetHeight, splashConfig=splashConfig)
        time.sleep(splashConfig["durationSeconds"])

    # State for the render loop. The board starts on a friendly "connecting"
    # screen so that a failure on the very first fetch shows a message rather
    # than ending the process.
    lastSuccessfulRefresh = time.time()
    isStale = False
    haveData = False
    lastData = None
    currentError = None
    shownError = None
    retryCount = 0
    nextAttemptTime = 0

    virtual = drawMessageSignage(
        device, width=widgetWidth, height=widgetHeight,
        title="Connecting", detail="Fetching train times...")
    # Show the connecting screen during the first (possibly slow) fetch.
    virtual.refresh()

    while True:
        now = time.time()

        if now >= nextAttemptTime:
            try:
                data = fetchData(config)

                lastData = data
                haveData = True
                currentError = None
                lastSuccessfulRefresh = time.time()
                retryCount = 0
                nextAttemptTime = time.time() + refreshTime

                virtual = buildView(device, widgetWidth, widgetHeight, data)
                shownError = None
            except Exception as err:
                # A transient network/API failure shouldn't kill the whole
                # process - keep showing the last good data (once it's stale,
                # the friendly error screen takes over) and retry with a
                # backoff, without blocking the render loop.
                currentError = describeError(err)

                if retryCount < len(retryBackoffSeconds):
                    delay = retryBackoffSeconds[retryCount]
                    retryCount += 1
                    print(f"Warning: {err} - keeping previous display, "
                          f"retrying in {delay}s (attempt {retryCount}/{len(retryBackoffSeconds)})")
                else:
                    delay = refreshTime
                    retryCount = 0
                    print(f"Warning: {err} - keeping previous display, "
                          f"giving up retries for now, trying again in {delay}s")

                nextAttemptTime = time.time() + delay

        isStale = (time.time() - lastSuccessfulRefresh) >= staleAfterSeconds

        # Keep the last good board through a brief blip (the clock's "!" marks
        # it stale), but swap to a friendly message once the outage has lasted,
        # or if nothing has ever loaded. Comparing the message itself means a
        # change of error (e.g. "No connection" -> "API access denied") redraws.
        showError = currentError if (currentError is not None and
                                     (not haveData or isStale)) else None
        if showError != shownError:
            if showError:
                virtual = drawMessageSignage(
                    device, width=widgetWidth, height=widgetHeight,
                    title=showError[0], detail=showError[1])
            elif haveData:
                virtual = buildView(device, widgetWidth, widgetHeight, lastData)
            else:
                virtual = drawMessageSignage(
                    device, width=widgetWidth, height=widgetHeight,
                    title="Connecting", detail="Fetching train times...")
            shownError = showError

        if dimmingConfig:
            shouldBeDimmed = isRun(dimmingConfig["startHour"], dimmingConfig["endHour"])
            if shouldBeDimmed != isDimmed:
                isDimmed = shouldBeDimmed
                setContrast(device, dimmingConfig["brightness"] if isDimmed
                            else dimmingConfig.get("normalBrightness", 255))

        virtual.refresh()

except KeyboardInterrupt:
    pass
except DeviceError as err:
    print(f"Display error: {err}")
    print("Could not drive the OLED display - check the wiring and that SPI is "
          "enabled (sudo raspi-config > Interface Options > SPI).")
except ValueError as err:
    # A bad/missing config is fatal, and exits non-zero so systemd reports the
    # unit as failed (rather than a clean stop) instead of leaving an
    # unattended board frozen on its last frame with no visible signal.
    print(f"Error: {err}")
    sys.exit(1)
except KeyError as err:
    print(f"Error: Please ensure the {err} environment variable is set")
    sys.exit(1)
