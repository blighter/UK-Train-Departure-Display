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

def renderCallingAt(draw, width, height):
    stations = "Calling at:"
    draw.text((0, 0), text=stations, font=font, fill="yellow")


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
    callingAt = "Calling at:"

    departures, firstDepartureDestinations, departureStation = data

    with canvas(device) as draw:
        w, h = textsize(draw, callingAt, font)

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

    rowTwoA = snapshot(callingWidth, 10, renderCallingAt, interval=100)
    rowTwoB = snapshot(width - callingWidth, 10,
                       renderStations(", ".join(firstDepartureDestinations)), interval=0.1)
    virtualViewport.add_hotspot(rowTwoA, (0, 12))
    virtualViewport.add_hotspot(rowTwoB, (callingWidth, 12))

    rowTime = snapshot(width, 14, renderTime, interval=1)
    virtualViewport.add_hotspot(rowTime, (0, 50))

    return virtualViewport


try:
    config = loadConfig()

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

    if config["apiMethod"] == 'rtt':
        data = loadDataRTT(config["rttApi"], config["journey"])
    else:
        data = loadData(config["transportApi"], config["journey"])      

    if data[0] == False:
        virtual = drawBlankSignage(
            device, width=widgetWidth, height=widgetHeight, departureStation=data[2])
    else:
        virtual = drawSignage(device, width=widgetWidth,
                              height=widgetHeight, data=data)

    timeAtStart = time.time()
    timeNow = time.time()

    while True:
        if(timeNow - timeAtStart >= config["refreshTime"]):
            try:
                if config["apiMethod"] == 'rtt':
                    data = loadDataRTT(config["rttApi"], config["journey"])
                else:
                    data = loadData(config["transportApi"], config["journey"])

                if data[0] == False:
                    virtual = drawBlankSignage(
                        device, width=widgetWidth, height=widgetHeight, departureStation=data[2])
                else:
                    virtual = drawSignage(device, width=widgetWidth,
                                          height=widgetHeight, data=data)
            except requests.exceptions.RequestException as err:
                # A transient network/API failure shouldn't kill the whole
                # process - keep showing the last good data and try again
                # on the next refresh.
                print(f"Warning: {err} - keeping previous display, retrying next refresh")

            timeAtStart = time.time()

        timeNow = time.time()
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
