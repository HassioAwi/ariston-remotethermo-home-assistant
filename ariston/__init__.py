"""Suppoort for Ariston."""
from datetime import timedelta
import logging
import requests
import threading
import voluptuous as vol
import json
import copy
import dateutil.parser
import time

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR
from homeassistant.components.climate import DOMAIN as CLIMATE
from homeassistant.components.sensor import DOMAIN as SENSOR
from homeassistant.components.switch import DOMAIN as SWITCH
from homeassistant.components.water_heater import DOMAIN as WATER_HEATER
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_BINARY_SENSORS,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_SENSORS,
    CONF_SWITCHES,
    CONF_USERNAME,
)
from homeassistant.exceptions import Unauthorized, UnknownUser
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import discovery
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.helpers.event import track_point_in_time
from homeassistant.util import dt as dt_util

from .binary_sensor import BINARY_SENSORS
from .const import (
    CH_MODE_TO_VALUE,
    CLIMATES,
    CONF_HVAC_OFF,
    CONF_POWER_ON,
    CONF_MAX_RETRIES,
    DATA_ARISTON,
    DEVICES,
    DOMAIN,
    MODE_TO_VALUE,
    SERVICE_SET_DATA,
    SERVICE_UPDATE,
    PARAM_MODE,
    PARAM_CH_MODE,
    PARAM_CH_SET_TEMPERATURE,
    PARAM_DHW_SET_TEMPERATURE,
    VAL_MODE_WINTER,
    VAL_MODE_SUMMER,
    VAL_MODE_OFF,
    VAL_CH_MODE_MANUAL,
    VAL_CH_MODE_SCHEDULED,
    WATER_HEATERS,
)
from .exceptions import CommError, LoginError, AristonError
from .helpers import service_signal
from .sensor import SENSORS
from .switch import SWITCHES

"""HTTP_RETRY_INTERVAL is time between 2 GET requests. Note that it often takes more than 10 seconds to properly fetch data, also potential login"""
"""MAX_ERRORS is number of errors for device to become not available"""
"""HTTP_TIMEOUT_LOGIN is timeout for login procedure"""
"""HTTP_TIMEOUT_GET is timeout to get data (can increase restart time in some cases). For tested environment often around 10 seconds, rarely above 15"""
"""HTTP_TIMEOUT_SET is timeout to set data"""

ARISTON_URL = "https://www.ariston-net.remotethermo.com"
DEFAULT_HVAC = "summer"
DEFAULT_POWER_ON = "summer"
DEFAULT_NAME = "Ariston"
DEFAULT_MAX_RETRIES = 1
DEFAULT_TIME = "00:00"
HTTP_RETRY_INTERVAL = 45
HTTP_RETRY_INTERVAL_DOWN = 80
HTTP_SET_INTERVAL = HTTP_RETRY_INTERVAL_DOWN * 2
HTTP_TIMEOUT_LOGIN = 3
HTTP_TIMEOUT_GET = 15
HTTP_TIMEOUT_SET = 15
MAX_ERRORS = 4
MAX_ERRORS_TIMER_EXTEND = 2
TIMER_SET_LOCK = 25

_LOGGER = logging.getLogger(__name__)

def _has_unique_names(devices):
    names = [device[CONF_NAME] for device in devices]
    vol.Schema(vol.Unique())(names)
    return devices


ARISTON_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_BINARY_SENSORS): vol.All(cv.ensure_list, [vol.In(BINARY_SENSORS)]),
        vol.Optional(CONF_SENSORS): vol.All(cv.ensure_list, [vol.In(SENSORS)]),
        vol.Optional(CONF_HVAC_OFF, default=DEFAULT_HVAC): vol.In(["OFF", "off", "Off", "summer", "SUMMER", "Summer"]),
        vol.Optional(CONF_POWER_ON, default=DEFAULT_POWER_ON): vol.In(["WINTER", "winter", "Winter", "summer", "SUMMER", "Summer"]),
        vol.Optional(CONF_MAX_RETRIES, default=DEFAULT_MAX_RETRIES): vol.All(int, vol.Range(min=0, max=65535)),
        vol.Optional(CONF_SWITCHES): vol.All(cv.ensure_list, [vol.In(SWITCHES)]),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.All(cv.ensure_list, [ARISTON_SCHEMA], _has_unique_names)},
    extra=vol.ALLOW_EXTRA,
)

class AristonChecker():
    """Ariston checker"""
    
    def __init__(self, hass, device, name, username, password, retries):
        """Initialize."""
        self._ariston_data = {}
        self._data_lock = threading.Lock()
        self._device = device
        self._errors = 0
        self._get_time_start = 0
        self._get_time_end = 0
        self._hass = hass
        self._init_available = False
        self._lock = threading.Lock()
        self._login = False
        self._name = name
        self._password = password
        self._plant_id = ""
        self._plant_id_lock = threading.Lock()
        self._retry_timeout = HTTP_RETRY_INTERVAL
        self._session = requests.Session()
        self._set_param = {}
        self._set_retry = 0
        self._set_max_retries = retries
        self._set_new_data = False
        self._set_scheduled = False
        self._set_time_start = 0
        self._set_time_end = 0
        self._token_lock = threading.Lock()
        self._token = None
        self._url = ARISTON_URL
        self._user = username
        self._verify = True

    @property
    def available(self):
        """Return if Aristons's API is responding."""
        return self._errors <= MAX_ERRORS and self._init_available

    def _login_session(self):
        """Login to fetch Ariston Plant ID and confirm login"""
        if not self._login:
            url = self._url + '/Account/Login'
            try:
                with self._token_lock:
                    self._token = requests.auth.HTTPDigestAuth(
                        self._user, self._password)
                login_data = {"Email": self._user, "Password": self._password}
                resp = self._session.post(
                    url,
                    auth=self._token,
                    timeout=HTTP_TIMEOUT_LOGIN,
                    json=login_data)
            except requests.exceptions.ReadTimeout as error:
                _LOGGER.warning('%s Authentication timeout', self)
                raise CommError(error)
            except LoginError:
                _LOGGER.warning('%s Authentication login error', self)
                raise
            except CommError:
                _LOGGER.warning('%s Authentication communication error', self)
                raise
            if resp.url.startswith(self._url + "/PlantDashboard/Index/"):
                with self._plant_id_lock:
                    self._plant_id = resp.url.split("/")[5]
                    self._login = True
                    _LOGGER.info('%s Plant ID is %s', self, self._plant_id)
            else:
                _LOGGER.warning('%s Authentication login error', self)
                raise LoginError

    def _get_http_data(self):
        """Get Ariston data from http"""
        self._login_session()
        if self._login and self._plant_id != "":
            if time.time() - self._set_time_start > TIMER_SET_LOCK:
                #give time to read new data
                url = self._url + '/PlantDashboard/GetPlantData/' + self._plant_id
                with self._data_lock:
                    try:
                        self._get_time_start = time.time()
                        resp = self._session.get(
                            url,
                            auth=self._token,
                            timeout=HTTP_TIMEOUT_GET,
                            verify=self._verify)
                        if resp.status_code == 599:
                            _LOGGER.warning("%s Code %s, data is %s", self, resp.status_code, resp.text)
                            raise CommError
                        elif resp.status_code == 500:
                            with self._plant_id_lock:
                                self._login = False
                            _LOGGER.warning("%s Code %s, data is %s", self, resp.status_code, resp.text)
                            raise CommError
                        elif resp.status_code != 200:
                            _LOGGER.warning("%s Unexpected reply %s", self, resp.status_code)
                            raise CommError
                        resp.raise_for_status()
                        #successful data fetching
                        self._get_time_end = time.time()
                        """
                        #uncomment below to store request time
                        f=open("/config/tmp/read_time.txt", "a+")
                        f.write("{}\n".format(self._get_time_end - self._get_time_start))
                        """
                    except requests.RequestException as error:
                        _LOGGER.warning("%s Failed due to error: %r", self, error)
                        raise CommError(error)
                    _LOGGER.info("%s Query worked. Exit code: <%s>", self, resp.status_code)
                    try:
                        self._ariston_data = copy.deepcopy(resp.json())
                        """
                        #uncomment below to log received data for troubleshooting purposes
                        with open('/config/tmp/data.json', 'w') as ariston_fetched:
                        json.dump(self._ariston_data, ariston_fetched)
                        """
                    except:
                        with self._plant_id_lock:
                                self._login = False
                        _LOGGER.warning("%s Invalid data received, not JSON", self)
                        raise CommError
            else:
                _LOGGER.debug("%s Setting data read restricted", self)
        else:
            _LOGGER.warning("%s Not properly logged in to get data", self)
            raise LoginError

    def _set_deroga_time(self):
        """Convert to 24H format if in 12H format"""
        try:
            if isinstance(self._ariston_data["zone"]["derogaUntil"], str):
                time_str_12h = self._ariston_data["zone"]["derogaUntil"]
            else:
                time_str_12h = DEFAULT_TIME
        except:
            time_str_12h = DEFAULT_TIME
        time_and_indic = time_str_12h.split(' ')
        try:
            if time_and_indic[1] == "AM":
                if time_and_indic[0] == "12:00":
                    time_str_24h = "00:00"
                else:
                    time_str_24h = time_and_indic[0]
            elif time_and_indic[1] == "PM":
                if time_and_indic[0] == "12:00":
                    time_str_24h = "12:00"
                else:
                    time_hour_minute = time_and_indic[0].split(":")
                    time_str_24h = str(int(time_hour_minute[0]) + 12) + time_hour_minute[1]
            else:
                #just check that we have hrours and minutes
                time_hour_minute = time_str_12h.split(":")
                if time_hour_minute[0] == "" or time_hour_minute[1] == "":
                    time_str_24h = DEFAULT_TIME
                else:
                    time_str_24h = time_str_12h
        except:
            time_str_24h = DEFAULT_TIME
        return time_str_24h

    def _actual_set_http_data(self, dummy=None):
        self._login_session()
        with self._data_lock:
            if not self._set_new_data:
                #scheduled setting
                self._set_scheduled = False
            else:
                #initial setting
                self._set_new_data = False
                self._set_retry = 0
            if self._login and self.available and self._plant_id != "":
                url = self._url + '/PlantDashboard/SetPlantAndZoneData/' + self._plant_id + '?zoneNum=1&umsys=si'
                data_changed = False
                set_data = {}
                set_data["NewValue"] = copy.deepcopy(self._ariston_data)
                set_data["OldValue"] = copy.deepcopy(self._ariston_data)
                # Format is received in 12H format but for some reason REST tools send it fine but python must send 24H format
                set_data["NewValue"]["zone"]["derogaUntil"] = self._set_deroga_time()
                set_data["OldValue"]["zone"]["derogaUntil"] = self._set_deroga_time()
                if PARAM_MODE in self._set_param:
                    if set_data["NewValue"]["mode"] == self._set_param[PARAM_MODE]:
                        if self._set_time_start < self._get_time_end:
                            #value should be up to date and match to remove from setting
                            del self._set_param[PARAM_MODE]
                        else:
                            #assume data was not yet changed
                            data_changed = True
                    else:
                        set_data["NewValue"]["mode"] = self._set_param[PARAM_MODE]
                        data_changed = True
                if PARAM_DHW_SET_TEMPERATURE in self._set_param:
                    if set_data["NewValue"]["dhwTemp"]["value"] == self._set_param[PARAM_DHW_SET_TEMPERATURE]:
                        if self._set_time_start < self._get_time_end:
                            #value should be up to date and match to remove from setting
                            del self._set_param[PARAM_DHW_SET_TEMPERATURE]
                        else:
                            #assume data was not yet changed
                            data_changed = True
                    else:
                        set_data["NewValue"]["dhwTemp"]["value"] = self._set_param[PARAM_DHW_SET_TEMPERATURE]
                        data_changed = True
                if PARAM_CH_SET_TEMPERATURE in self._set_param:
                    if set_data["NewValue"]["zone"]["comfortTemp"]["value"] == self._set_param[PARAM_CH_SET_TEMPERATURE]:
                        if self._set_time_start < self._get_time_end:
                            #value should be up to date and match to remove from setting
                            del self._set_param[PARAM_CH_SET_TEMPERATURE]
                        else:
                            #assume data was not yet changed
                            data_changed = True
                    else:
                        set_data["NewValue"]["zone"]["comfortTemp"]["value"] = self._set_param[PARAM_CH_SET_TEMPERATURE]
                        data_changed = True
                if PARAM_CH_MODE in self._set_param:
                    if set_data["NewValue"]["zone"]["mode"]["value"] == self._set_param[PARAM_CH_MODE]:
                        if self._set_time_start < self._get_time_end:
                            #value should be up to date and match to remove from setting
                            del self._set_param[PARAM_CH_MODE]
                        else:
                            #assume data was not yet changed
                            data_changed = True
                    else:
                        set_data["NewValue"]["zone"]["mode"]["value"] = self._set_param[PARAM_CH_MODE]
                        data_changed = True
                if data_changed == True:
                    if not self._set_scheduled:
                        if self._set_retry < self._set_max_retries:
                            #retry again after enough time to fetch data twice
                            retry_time = dt_util.now() + timedelta(seconds=HTTP_SET_INTERVAL)
                            track_point_in_time(self._hass, self._actual_set_http_data, retry_time)
                            self._set_retry = self._set_retry + 1
                            self._set_scheduled = True
                        else:
                            #no more retries, no need to keep changed data
                            if PARAM_MODE in self._set_param:
                                del self._set_param[PARAM_MODE]
                            if PARAM_DHW_SET_TEMPERATURE in self._set_param:
                                del self._set_param[PARAM_DHW_SET_TEMPERATURE]
                            if PARAM_CH_SET_TEMPERATURE in self._set_param:
                                del self._set_param[PARAM_CH_SET_TEMPERATURE]
                            if PARAM_CH_MODE in self._set_param:
                                del self._set_param[PARAM_CH_MODE]
                    try:
                        self._set_time_start = time.time()
                        resp = self._session.post(
                            url,
                            auth=self._token,
                            timeout=HTTP_TIMEOUT_SET,
                            json=set_data)
                        if resp.status_code != 200:
                            _LOGGER.warning("%s Command to set data failed with code: %s", self, resp.status_code)
                            raise CommError
                        resp.raise_for_status()
                        self._set_time_end = time.time()
                        """
                        #uncomment below to store request time
                        request_time = time.time() - self._set_time_start
                        f=open("/config/tmp/set_time.txt", "a+")
                        f.write("{}\n".format(request_time))
                        """
                    except requests.exceptions.ReadTimeout as error:
                        _LOGGER.warning('%s Request timeout', self)
                        raise CommError(error)
                    except CommError:
                        _LOGGER.warning('%s Request communication error', self)
                        raise
                    #store data in reply, but note that in some cases in fact it is not set
                    self._ariston_data = copy.deepcopy(resp.json())
                    _LOGGER.info('%s Data was changed', self)
                else:
                    _LOGGER.debug('%s Same data was used', self)         
            else:
                #api is down
                if not self._set_scheduled:
                    if self._set_retry < self._set_max_retries:
                        #retry again after enough time to fetch data twice
                        retry_time = dt_util.now() + timedelta(seconds=HTTP_SET_INTERVAL)
                        track_point_in_time(self._hass, self._actual_set_http_data, retry_time)
                        self._set_retry = self._set_retry + 1
                        self._set_scheduled = True
                    else:
                        #no more retries, no need to keep changed data
                        if PARAM_MODE in self._set_param:
                            del self._set_param[PARAM_MODE]
                        if PARAM_DHW_SET_TEMPERATURE in self._set_param:
                            del self._set_param[PARAM_DHW_SET_TEMPERATURE]
                        if PARAM_CH_SET_TEMPERATURE in self._set_param:
                            del self._set_param[PARAM_CH_SET_TEMPERATURE]
                        if PARAM_CH_MODE in self._set_param:
                            del self._set_param[PARAM_CH_MODE]
                _LOGGER.warning("%s No stable connection to set the data", self)
                raise CommError

    def _set_http_data(self, parameter_list={}):
        """Set Ariston data over http after data verification"""
        if self._ariston_data != {}:
            url = self._url + '/PlantDashboard/SetPlantAndZoneData/' + self._plant_id + '?zoneNum=1&umsys=si'
            with self._data_lock:
                # check mode and set it
                if PARAM_MODE in parameter_list:
                    wanted_mode = str(parameter_list[PARAM_MODE]).lower()
                    if wanted_mode in MODE_TO_VALUE:
                        self._set_param[PARAM_MODE] = MODE_TO_VALUE[wanted_mode]
                        _LOGGER.info('%s New mode %s', self, wanted_mode)
                    else:
                        _LOGGER.warning('%s Unknown mode: %s', self, wanted_mode)
                # check dhw temperature
                if PARAM_DHW_SET_TEMPERATURE in parameter_list:
                    wanted_dhw_temperature = str(parameter_list[PARAM_DHW_SET_TEMPERATURE]).lower()
                    try:
                        #round to nearest 1
                        temperature = round(float(wanted_dhw_temperature))
                        if temperature >= self._ariston_data["dhwTemp"]["min"] and temperature <= \
                                self._ariston_data["dhwTemp"]["max"]:
                            self._set_param[PARAM_DHW_SET_TEMPERATURE] = temperature
                            _LOGGER.info('%s New DHW temperature %s', self, temperature)
                        else:
                            _LOGGER.warning('%s Not supported DHW temperature value: %s', self, wanted_dhw_temperature)
                    except:
                        _LOGGER.warning('%s Not supported DHW temperature value: %s', self, wanted_dhw_temperature)
                        pass
                # check CH temperature
                if PARAM_CH_SET_TEMPERATURE in parameter_list:
                    wanted_ch_temperature = str(parameter_list[PARAM_CH_SET_TEMPERATURE]).lower()
                    try:
                        #round to nearest 0.5
                        temperature = round(float(wanted_ch_temperature) * 2.0) / 2.0
                        if temperature >= self._ariston_data["zone"]["comfortTemp"]["min"] and temperature <= \
                                self._ariston_data["zone"]["comfortTemp"]["max"]:
                            self._set_param[PARAM_CH_SET_TEMPERATURE] = temperature
                            _LOGGER.info('%s New CH temperature %s', self, temperature)
                        else:
                            _LOGGER.warning('%s Not supported CH temperature value: %s', self, wanted_ch_temperature)
                    except:
                        _LOGGER.warning('%s Not supported CH temperature value: %s', self, wanted_ch_temperature)
                        pass
                # check CH mode
                if PARAM_CH_MODE in parameter_list:
                    wanted_ch_mode = str(parameter_list[PARAM_CH_MODE]).lower()
                    if wanted_ch_mode in CH_MODE_TO_VALUE:
                        self._set_param[PARAM_CH_MODE] = CH_MODE_TO_VALUE[wanted_ch_mode]
                        _LOGGER.info('%s New CH mode %s', self, wanted_ch_mode)
                    else:
                        _LOGGER.warning('%s Unknown mode: %s', self, wanted_ch_mode)
                self._set_new_data = True
            self._actual_set_http_data()
        else:
            _LOGGER.warning("%s No valid data fetched from server to set changes", self)
            raise CommError

    def command(self, dummy=None):
        """trigger fetching of data"""
        with self._data_lock:
            if self._errors >= MAX_ERRORS_TIMER_EXTEND:
                #give a little rest to the system
                self._retry_timeout = HTTP_RETRY_INTERVAL_DOWN
                _LOGGER.warning('%s Retrying in %s seconds', self, self._retry_timeout)
            else:
                self._retry_timeout = HTTP_RETRY_INTERVAL
                _LOGGER.debug('%s Fetching data in %s seconds', self, self._retry_timeout)
            retry_time = dt_util.now() + timedelta(seconds=self._retry_timeout)
            track_point_in_time(self._hass, self.command, retry_time)
        try:
            self._get_http_data()
        except AristonError:
            with self._lock:
                was_online = self.available
                self._errors += 1
                _LOGGER.warning("%s errors: %i", self._name, self._errors)
                offline = not self.available
            if offline and was_online:
                with self._plant_id_lock:
                    self._login = False
                _LOGGER.error("%s is offline: Too many errors", self._name)
                dispatcher_send(self._hass, service_signal(SERVICE_UPDATE, self._name))
            raise
        with self._lock:
            was_offline = not self.available
            self._errors = 0
            self._init_available = True
        if was_offline:
            _LOGGER.info("%s Ariston back online", self._name)
            dispatcher_send(self._hass, service_signal(SERVICE_UPDATE, self._name))

def setup(hass, config):
    """Set up the Ariston component."""
    hass.data.setdefault(DATA_ARISTON, {DEVICES: {}, CLIMATES: [], WATER_HEATERS: []})
    api_list = []
    for device in config[DOMAIN]:
        name = device[CONF_NAME]
        username = device[CONF_USERNAME]
        password = device[CONF_PASSWORD]
        retries = device[CONF_MAX_RETRIES]
        entity_id = "climate."+name
        try:
            api = AristonChecker(hass, device=device, name=name, username=username, password=password, retries=retries)
            api_list.append(api)
            api.command()
        except LoginError as ex:
            _LOGGER.error("Login error for %s: %s", name, ex)
            pass
        except AristonError as ex:
            _LOGGER.error("Communication error for %s: %s", name, ex)
            pass
        binary_sensors = device.get(CONF_BINARY_SENSORS)
        sensors = device.get(CONF_SENSORS)
        switches = device.get(CONF_SWITCHES)
        hass.data[DATA_ARISTON][DEVICES][name] = AristonDevice(api)
        discovery.load_platform(
            hass, CLIMATE,
            DOMAIN,
            {CONF_NAME: name},
            config)
        discovery.load_platform(
            hass, WATER_HEATER,
            DOMAIN,
            {CONF_NAME: name},
            config)
        if switches:
            discovery.load_platform(
                hass,
                SWITCH,
                DOMAIN,
                {CONF_NAME: name, CONF_SWITCHES: switches},
                config,
            )
        if binary_sensors:
            discovery.load_platform(
                hass,
                BINARY_SENSOR,
                DOMAIN,
                {CONF_NAME: name, CONF_BINARY_SENSORS: binary_sensors},
                config,
            )
        if sensors:
            discovery.load_platform(
                hass,
                SENSOR,
                DOMAIN,
                {CONF_NAME: name, CONF_SENSORS: sensors},
                config
            )

    def set_ariston_data(call):
        """Handle the service call."""
        entity_id = call.data.get(ATTR_ENTITY_ID, "")
        try:
            domain = entity_id.split(".")[0]
        except:
            _LOGGER.warning("invalid entity_id domain")
            raise AristonError
        if domain.lower() != "climate":
            _LOGGER.warning("invalid entity_id domain")
            raise AristonError
        try:
            device = entity_id.split(".")[1]
        except:
            _LOGGER.warning("invalid entity_id device")
            raise AristonError
        for api in api_list:
            if api._name.lower() == device.lower():
                try:
                    with api._data_lock:
                        parameter_list = {}
                        data = call.data.get(PARAM_MODE, "")
                        if data != "":
                            parameter_list[PARAM_MODE] = data
                        data = call.data.get(PARAM_CH_MODE, "")
                        if data != "":
                            parameter_list[PARAM_CH_MODE] = data
                        data = call.data.get(PARAM_CH_SET_TEMPERATURE, "")
                        if data != "":
                            parameter_list[PARAM_CH_SET_TEMPERATURE] = data
                        data = call.data.get(PARAM_DHW_SET_TEMPERATURE, "")
                        if data != "":
                            parameter_list[PARAM_DHW_SET_TEMPERATURE] = data
                    _LOGGER.debug("device found")
                    api._set_http_data(parameter_list)
                except CommError:
                    _LOGGER.warning("Communication error for Ariston")
                    raise
                return
        _LOGGER.warning("Entity %s not found", entity_id)
        raise AristonError
        return

    hass.services.register(DOMAIN, SERVICE_SET_DATA, set_ariston_data)

    if not hass.data[DATA_ARISTON][DEVICES]:
        return False

    # Return boolean to indicate that initialization was successful.
    return True


class AristonDevice:
    """Representation of a base Ariston discovery device."""

    def __init__(
            self,
            api,
    ):
        """Initialize the entity."""
        self.api = api
