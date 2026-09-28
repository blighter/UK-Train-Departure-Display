import os
import sys
import time
import json
import requests

from datetime import datetime
from PIL import ImageFont, Image
from helpers import get_device
from trains import loadDeparturesForStation, loadDestinationsForDeparture, loadDeparturesForStationRTT, loadDestinationsForDepartureRTT
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
        if not rttApi.get('username') or not rttApi.get('password'):
            raise ValueError(
                "Please complete the rttApi section of your config.json file")
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

        if departure["status"] == "CANCELLED" or departure["status"] == "CANCELLED_CALL" or departure["status"] == "CANCELLED_PASS":
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
        if departure["mode"] == "bus":
            draw.text((0, 0), text="BUS", font=font, fill="yellow")
        else:
            if departure["platform"]:
                draw.text((0, 0), text="Plat "+departure["platform"], font=font, fill="yellow")
    return drawText

def renderLabel(text):
    def drawText(draw, width, height):
        draw.text((0, 0), text=text, font=font, fill="yellow")
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

    departures, stationName = loadDeparturesForStationRTT(
        journeyConfig, apiConfig["username"], apiConfig["password"])

    if len(departures) == 0:
        return False, False, stationName

    firstDepartureDestinations = loadDestinationsForDepartureRTT(
        journeyConfig, apiConfig["username"], apiConfig["password"], departures[0]["time_table_url"])    

    #return False, False, journeyConfig['outOfHoursName']
    return departures, firstDepartureDestinations, stationName

def fetchData(config):
    if config["apiMethod"] == 'rtt':
        return loadDataRTT(config["rttApi"], config["journey"])
    return loadData(config["transportApi"], config["journey"])

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

    data = fetchData(config)

    if data[0] == False:
        virtual = drawBlankSignage(
            device, width=widgetWidth, height=widgetHeight, departureStation=data[2])
    else:
        virtual = drawSignage(device, width=widgetWidth,
                              height=widgetHeight, data=data)

    lastSuccessfulRefresh = time.time()
    isStale = False
    retryCount = 0
    nextAttemptTime = time.time() + refreshTime

    while True:
        now = time.time()

        if now >= nextAttemptTime:
            try:
                data = fetchData(config)

                if data[0] == False:
                    virtual = drawBlankSignage(
                        device, width=widgetWidth, height=widgetHeight, departureStation=data[2])
                else:
                    virtual = drawSignage(device, width=widgetWidth,
                                          height=widgetHeight, data=data)

                lastSuccessfulRefresh = time.time()
                retryCount = 0
                nextAttemptTime = time.time() + refreshTime
            except requests.exceptions.RequestException as err:
                # A transient network/API failure shouldn't kill the whole
                # process - keep showing the last good data and retry with
                # a backoff, without blocking the render loop (the clock and
                # scroller keep moving while we wait).
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
    print(f"Error: {err}")
except requests.exceptions.RequestException as err:
    print(f"Error: {err}")
except ValueError as err:
    print(f"Error: {err}")
except KeyError as err:
    print(f"Error: Please ensure the {err} environment variable is set")
