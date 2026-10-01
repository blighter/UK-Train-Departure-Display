import hashlib
import json
import os
import time
import requests

from datetime import datetime


class ApiError(Exception):
    """An API failure we can explain to the user.

    ``title`` and ``detail`` are short, human-readable strings that are safe
    to render on the 256x64 display; the full exception message (including the
    detail) is still what ends up in the logs.
    """

    def __init__(self, title, detail=""):
        super().__init__(f"{title}: {detail}" if detail else title)
        self.title = title
        self.detail = detail


# The next-generation Real Time Trains API. The original api.rtt.io service
# (HTTP Basic username/password auth) is deprecated and switches off on
# 31 September 2026. The new service at https://data.rtt.io uses Bearer token
# auth: either a long-life access token, or a refresh token that is exchanged
# for a short-life access token via /api/get_access_token.
RTT_API_BASE = "https://data.rtt.io"
RTT_REQUEST_TIMEOUT = 15

# Only ONE credential is configured, in rttApi.token, exactly as issued by the
# portal. The portal hands out either a long-life access token (usable
# directly) or a long-life refresh token (which must be exchanged), and the
# app works out which on the first request: the credential is tried directly,
# and on a 401 it is tried against /api/get_access_token before the request
# is retried. Minted short-life access tokens are cached in memory and
# persisted to RTT_TOKEN_STATE_FILE (keyed on a hash of the credential, so
# pasting a new token invalidates the stored state) so restarts reuse the
# current token instead of minting a fresh one on every boot, and are renewed
# shortly before their advertised validUntil expiry.
RTT_TOKEN_STATE_FILE = ".rtt-token.json"
_TOKEN_RENEW_MARGIN = 60


class _CredentialRejected(Exception):
    """The exchange endpoint rejected the credential as a refresh token."""


def _checkResponse(response):
    """Turn a non-success HTTP response into a friendly :class:`ApiError`."""
    status = response.status_code
    if status < 400:
        return

    if status == 401:
        raise ApiError("API access denied",
                       "Check rttApi.token in config.json")
    if status == 403:
        # A valid token can still be entitled to less than the request needs
        # (e.g. time windows) - re-authing won't help.
        raise ApiError("API access denied",
                       "The token isn't entitled to this request")
    if status == 404:
        raise ApiError("Not found",
                       "RTT could not find that station or service")
    if status == 429:
        retryAfter = response.headers.get("Retry-After")
        detail = (f"Rate limited - retry in {retryAfter}s"
                  if retryAfter else "Rate limited - retrying shortly")
        raise ApiError("Too many requests", detail)
    if status >= 500:
        raise ApiError("RTT is unavailable",
                       "Realtime Trains is having a problem")
    raise ApiError("Request rejected", f"RTT returned HTTP {status}")


def _credentialHash(credential):
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


class RttAuth:
    """Resolve the Bearer credential configured in ``rttApi.token``.

    The portal-issued credential is either a long-life access token or a
    long-life refresh token; the type is discovered on the first request.
    Minted access tokens are kept until shortly before they expire instead
    of being requested per API call, and the learned state is persisted so a
    restarted board doesn't mint (or mis-probe) again.
    """

    def __init__(self, apiConfig, stateFile=RTT_TOKEN_STATE_FILE):
        # Legacy configs kept the credential under refreshToken; both
        # spellings are accepted so existing deployments keep working.
        credential = (apiConfig.get("token")
                      or apiConfig.get("refreshToken") or "").strip()
        if not credential:
            raise ValueError(
                "Please set rttApi.token (your Real Time Trains API token) "
                "in config.json")
        self.credential = credential
        self.stateFile = stateFile
        # "refresh" - the credential must be exchanged for access tokens;
        # None - not yet learned (tried directly first).
        self.mode = None
        self.accessToken = None
        self.validUntil = 0.0
        self._loadState()

    def request(self, path, params):
        """GET an RTT API endpoint, recovering once from a stale token."""
        response = self._get(path, params)
        if response.status_code == 401 and self._recover():
            response = self._get(path, params)
        if response.status_code == 204:
            # A valid query with no services returns 204 No Content, not a
            # JSON body.
            return {}
        _checkResponse(response)
        try:
            return response.json()
        except ValueError as err:
            raise ApiError("Unexpected response",
                           "Real Time Trains sent unreadable data") from err

    def _get(self, path, params):
        return requests.get(
            f"{RTT_API_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._bearer()}",
                     "Accept": "application/json"},
            timeout=RTT_REQUEST_TIMEOUT)

    def _bearer(self):
        if self.mode == "refresh":
            if not self.accessToken or \
                    self.validUntil - time.time() <= _TOKEN_RENEW_MARGIN:
                try:
                    self._mint()
                except _CredentialRejected:
                    # The credential was learned to be a refresh token, so a
                    # rejection here means it has been revoked or expired.
                    raise ApiError("API access denied",
                                   "Check rttApi.token in config.json")
            return self.accessToken
        # Unknown mode: try the credential directly first so access-token
        # users never touch the exchange endpoint.
        return self.credential

    def _recover(self):
        """React to a 401; True means the caller should retry the request."""
        if self.mode == "refresh":
            # The minted token was rejected before its advertised expiry
            # (revocation, clock skew on a Pi with no RTC): re-mint once.
            self.accessToken = None
            self.validUntil = 0.0
        try:
            self._mint()
        except _CredentialRejected:
            # The portal doesn't accept this credential as a refresh token
            # either - the original 401 stands (dead token in config).
            return False
        return True

    def _mint(self):
        """Exchange the credential for a short-life access token."""
        response = requests.get(
            f"{RTT_API_BASE}/api/get_access_token",
            headers={"Authorization": f"Bearer {self.credential}"},
            timeout=RTT_REQUEST_TIMEOUT)
        if response.status_code in (400, 401, 403):
            raise _CredentialRejected()
        _checkResponse(response)

        try:
            data = response.json()
        except ValueError as err:
            raise ApiError("API login failed",
                           "RTT sent unreadable login data") from err

        token = data.get("token")
        if not token:
            raise ApiError("API login failed",
                           "RTT did not return an access token")

        validUntil = _parseTimestamp(data.get("validUntil"))
        if validUntil is None:
            validUntil = time.time() + 300
        self.mode = "refresh"
        self.accessToken = token
        self.validUntil = validUntil
        self._saveState()

    def _loadState(self):
        try:
            with open(self.stateFile, "r") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return
        if state.get("credentialHash") != _credentialHash(self.credential):
            return
        if state.get("mode") != "refresh":
            return
        self.mode = "refresh"
        self.accessToken = state.get("token") or None
        validUntil = state.get("validUntil")
        if isinstance(validUntil, (int, float)) \
                and not isinstance(validUntil, bool):
            self.validUntil = float(validUntil)

    def _saveState(self):
        state = {
            "credentialHash": _credentialHash(self.credential),
            "mode": "refresh",
            "token": self.accessToken,
            "validUntil": self.validUntil,
        }
        tmpFile = f"{self.stateFile}.tmp"
        try:
            with open(tmpFile, "w") as f:
                json.dump(state, f)
                f.write("\n")
            os.chmod(tmpFile, 0o600)
            os.replace(tmpFile, self.stateFile)
        except OSError:
            # A read-only working directory must not break the board; the
            # worst case is re-minting on every restart.
            try:
                os.remove(tmpFile)
            except OSError:
                pass


def getRttAuth(apiConfig):
    """Build the Bearer resolver for the configured RTT credential."""
    return RttAuth(apiConfig)


def _parseDateTime(value):
    """Parse an ISO 8601 / RFC3339 string, normalising a trailing ``Z`` first."""
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parseTimestamp(value):
    """Parse an ISO 8601 / RFC3339 timestamp into a POSIX timestamp."""
    parsed = _parseDateTime(value)
    return parsed.timestamp() if parsed else None


def _formatTime(value):
    """Format an API timestamp as a local ``HH:MM`` string, or None."""
    parsed = _parseDateTime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%H:%M")


def abbrStation(journeyConfig, inputStr):
    dict = journeyConfig['stationAbbr']
    for key in dict.keys():
        inputStr = inputStr.replace(key, dict[key])
    return inputStr


def loadDeparturesForStationRTT(journeyConfig, auth):
    if journeyConfig["departureStation"] == "":
        raise ValueError(
            "Please set the journey.departureStation property in config.json")

    departureStation = journeyConfig["departureStation"]

    # The API's default lookup window is 60 minutes, which can be too narrow
    # for quieter stations to fill the board; widen it so there are always a
    # few services to show.
    data = auth.request("/rtt/location",
                        {"code": departureStation, "timeWindow": 120})

    queryLocation = (data.get("query") or {}).get("location") or {}
    stationName = queryLocation.get("description") or departureStation

    services = data.get("services") or []
    translated_departures = []

    # Iterate the whole window and stop once we have five displayable rows,
    # rather than slicing to five first - otherwise a run of non-passenger or
    # untimed services at the front can leave the board under-filled even
    # though valid departures follow.
    for item in services:
        scheduleMetadata = item.get("scheduleMetadata") or {}
        temporalData = item.get("temporalData") or {}
        departure = temporalData.get("departure") or {}

        # Skip non-passenger movements (freight, empty stock) so the board
        # only shows services people can actually catch.
        if scheduleMetadata.get("inPassengerService") is False:
            continue
        locationMetadata = item.get("locationMetadata") or {}
        destinations = item.get("destination") or []

        destinationName = departureStation
        if destinations:
            destinationLocation = destinations[0].get("location") or {}
            destinationName = destinationLocation.get("description") or destinationName

        aimed = _formatTime(departure.get("scheduleAdvertised")) \
            or _formatTime(departure.get("scheduleInternal"))
        if aimed is None:
            # Without an advertised time there is nothing useful to render.
            continue

        expected = _formatTime(departure.get("realtimeForecast")) \
            or _formatTime(departure.get("realtimeEstimate")) \
            or _formatTime(departure.get("realtimeActual")) \
            or aimed

        displayAs = temporalData.get("displayAs")
        if departure.get("isCancelled"):
            status = "CANCELLED"
        elif displayAs:
            status = displayAs
        else:
            status = "CALL"

        modeType = (scheduleMetadata.get("modeType") or "TRAIN").upper()
        mode = "bus" if "BUS" in modeType else modeType.lower()

        platformData = locationMetadata.get("platform") or {}
        platform = platformData.get("actual") or platformData.get("planned")

        # Prefer a cancellation reason over a delay reason - a cancelled
        # service can also carry a stale delay reason, and neither is present
        # for an on-time service.
        reasons = item.get("reasons") or []
        cancelReasons = [r for r in reasons if (r or {}).get("type") == "CANCEL"]
        chosenReasons = cancelReasons or reasons
        delay_reason = None
        if chosenReasons:
            delay_reason = chosenReasons[0].get("longText") \
                or chosenReasons[0].get("shortText")

        translated_departures.append({
            'uid': scheduleMetadata.get("uniqueIdentity"),
            'destination_name': abbrStation(journeyConfig, destinationName),
            'aimed_departure_time': aimed,
            'expected_departure_time': expected,
            'status': status,
            'mode': mode,
            'platform': platform,
            'delay_reason': delay_reason,
        })

        if len(translated_departures) >= 5:
            break

    return translated_departures, stationName


def loadDestinationsForDepartureRTT(journeyConfig, auth, uniqueIdentity):
    if not uniqueIdentity:
        return []

    data = auth.request("/rtt/service", {"uniqueIdentity": uniqueIdentity})
    service = data.get("service") or {}
    locations = service.get("locations") or []

    calling_at = []
    foundDepartureStation = False

    for location in locations:
        geo = location.get("location") or {}
        temporalData = location.get("temporalData") or {}
        displayAs = temporalData.get("displayAs")

        if not foundDepartureStation:
            codes = (geo.get("shortCodes") or []) + (geo.get("longCodes") or [])
            if journeyConfig["departureStation"] in codes:
                foundDepartureStation = True
            continue

        # Only advertise points the train actually calls at - the service
        # response also contains locations it merely passes through.
        if displayAs not in ("CALL", "STARTS", "TERMINATES"):
            continue

        description = geo.get("description")
        if description:
            calling_at.append(abbrStation(journeyConfig, description))

    if len(calling_at) == 1:
        calling_at[0] = calling_at[0] + ' only.'

    return calling_at


def loadDeparturesForStation(journeyConfig, appId, apiKey):
    if journeyConfig["departureStation"] == "":
        raise ValueError(
            "Please set the journey.departureStation property in config.json")

    if appId == "" or apiKey == "":
        raise ValueError(
            "Please complete the transportApi section of your config.json file")

    departureStation = journeyConfig["departureStation"]

    URL = f"http://transportapi.com/v3/uk/train/station/{departureStation}/live.json"

    PARAMS = {'app_id': appId,
              'app_key': apiKey,
              'calling_at': journeyConfig["destinationStation"]}

    r = requests.get(url=URL, params=PARAMS, timeout=RTT_REQUEST_TIMEOUT)

    data = r.json()

    if "error" in data:
        raise ValueError(data["error"])

    #apply abbreviations / replacements to station names (long stations names dont look great on layout)
    #see config file for replacement list
    for item in data["departures"]["all"]:
         item['origin_name'] = abbrStation(journeyConfig, item['origin_name'])
         item['destination_name'] = abbrStation(journeyConfig, item['destination_name'])

    return data["departures"]["all"], data["station_name"]


def loadDestinationsForDeparture(journeyConfig, timetableUrl):
    r = requests.get(url=timetableUrl, timeout=RTT_REQUEST_TIMEOUT)

    data = r.json()

    if "error" in data:
        raise ValueError(data["error"])

    #apply abbreviations / replacements to station names (long stations names dont look great on layout)
    #see config file for replacement list
    foundDepartureStation = False

    for item in list(data["stops"]):
        if item['station_code'] == journeyConfig['departureStation']:
            foundDepartureStation = True

        if foundDepartureStation == False:
            data["stops"].remove(item)
            continue

        item['station_name'] = abbrStation(journeyConfig, item['station_name'])

    departureDestinationList = list(map(lambda x: x["station_name"], data["stops"]))[1:]

    if len(departureDestinationList) == 1:
        departureDestinationList[0] = departureDestinationList[0] + ' only.'

    return departureDestinationList
