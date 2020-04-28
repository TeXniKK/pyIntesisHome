import argparse
import asyncio
import json
import logging
import sys

import aiohttp

_LOGGER = logging.getLogger("pyintesishome")
_LOGGER.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
_LOGGER.addHandler(handler)

INTESIS_NULL = 32768

DEVICE_INTESISHOME = "IntesisHome"
DEVICE_AIRCONWITHME = "airconwithme"

API_DISCONNECTED = "Disconnected"
API_CONNECTING = "Connecting"
API_AUTHENTICATED = "Connected"
API_AUTH_FAILED = "Wrong username/password"

PARAM_POWER = "onOff"
PARAM_POWER_CONSUMPTION = "instantPowerConsumption"
PARAM_ACC_POWER_CONSUMPTION = "accumulatedPowerConsumption"
PARAM_MODE = "userMode"
PARAM_FAN_SPEED = "fanSpeed"
PARAM_SETPOINT = "userSetpoint"
PARAM_SETPOINT_MAX = "maxTemperatureSetpoint"
PARAM_SETPOINT_MIN = "minTemperatureSetpoint"
PARAM_WORKING_HOURS = "onTime"
PARAM_OUTDOOR_TEMP = "outdoorTemperature"
PARAM_TEMPERATURE = "returnPathTemperature"

class IHConnectionError(Exception):
    pass


class IHAuthenticationError(ConnectionError):
    pass

class IHDataError(Exception):
    pass

# This function takes a single proto-tag---the string in between the commas
# that will be turned into a valid tag---and sanitizes it.  It either
# returns an alphanumeric string (if the argument can be made into a valid
# tag) or None (if the argument cannot be made into a valid tag; i.e., if
# the argument contains only whitespace and/or punctuation).
def sanitizeTag(str):
    str = ''.join(c if c.isalnum() else ' ' for c in str)
    words = str.split()
    numWords = len(words)
    if numWords == 0:
        return None
    elif numWords == 1:
        return words[0]
    else:
        return words[0].lower() + ''.join(w.capitalize() for w in words[1:])

class IntesisHome:
    def __init__(
        self,
        username,
        password,
        loop=None,
        websession=None,
        device_type=DEVICE_INTESISHOME,
        ip="",
    ):
        # Select correct API for device type
        self._device_type = device_type
        self._api_url = 'http://' + ip + '/api.cgi'
        self._ip = ip
        self._username = username
        self._password = password
        self._cmdServer = None
        self._cmdServerPort = None
        self._connectionRetires = 0
        self._authToken = None
        self._devices = {}
        self._connected = False
        self._connecting = False
        self._sendQueue = asyncio.Queue()
        self._sendQueueTask = None
        self._updateCallbacks = []
        self._errorMessage = None
        self._webSession = websession
        self._ownSession = False
        self._reconnectionAttempt = 0

        if loop:
            _LOGGER.debug("Using the provided event loop")
            self._eventLoop = loop
        else:
            _LOGGER.debug("Getting the running loop from asyncio")
            self._eventLoop = asyncio.get_running_loop()

        if not self._webSession:
            _LOGGER.debug("Creating new websession")
            self._webSession = aiohttp.ClientSession()
            self._ownSession = True

    async def _handle_packets(self):
        while True:
            await asyncio.sleep(5)
            try:
                #await self.poll_status()
                for devId, dev in self._devices.items():
                    await self._query_state(devId)
            except IHConnectionError:
                _LOGGER.error(
                    f"pyIntesisHome lost connection to the {self._device_type} server."
                )

                self._connected = False
                self._connecting = False
                self._authToken = None
                self._sendQueueTask.cancel()
                await self._send_update_callback()
                return

    async def _send_queue(self):
        while self._connected or self._connecting:
            data = await self._sendQueue.get()
            try:
                toSend = {
                    "command": "setdatapointvalue",
                    "data":
                    {
                        "sessionID": self._authToken,
                        "uid": int(data.get("uid")),
                        "value": int(data.get("value"))
                    }
                }

                await self._process_query(toSend)
                _LOGGER.debug(f"Sending command {data}")
            except Exception as e:
                _LOGGER.error(f"Exception: {repr(e)}")
                return

    async def _process_query(self, request_json):
        status_response = None
        try:
            async with self._webSession.post(
                url=self._api_url, json=request_json
            ) as resp:
                status_response = await resp.json(content_type="application/json")
                _LOGGER.debug(status_response)
        except (aiohttp.client_exceptions.ClientError) as e:
            self._errorMessage = f"Error connecting to {self._device_type} API: {e}"
            _LOGGER.error(f"{type(e)} Exception. {repr(e.args)} / {e}")
            raise IHConnectionError
        except (aiohttp.client_exceptions.ClientConnectorError) as e:
            raise IHConnectionError

        if status_response.get("success") == False:
            self._errorMessage = status_response.get("error").get("message")
            _LOGGER.error(f"Error from API {repr(self._errorMessage)}")
            raise IHAuthenticationError()
        return status_response.get("data")

    def _get_param_value(self, deviceId, name):
        val = self._devices[str(deviceId)].get(name)
        _LOGGER.debug(name)
        _LOGGER.debug(val)
        
        for attr, value in self._devices[deviceId]["dataMap"].items():
            if value.get("id") == name:
                if "values" in value:
                    val = value.get("values").get(str(val))
                return val

    def _get_param_value_id(self, obj, value):
        if "values" in obj:
            for attr, item in obj.get("values").items():
                if value == item:
                    return attr
            _LOGGER.error('Unknown value ' + value)
            raise IHDataError
        else:
            return value

    def _has_feature(self, deviceId, name):
        for attr, value in self._devices[deviceId]["dataMap"].items(): #TODO: per device
            if value.get("id") == name:
                return True
        return False

    def _get_param_options(self, deviceId, name):
        for attr, value in self._devices[deviceId]["dataMap"].items():
            if value.get("id") == name:
                res_list = list()
                for keyItem, valItem in value.get("values").items():
                    res_list.append(valItem)
                return res_list
        return None

    async def connect(self):
        """Public method for connecting to IntesisHome/Airconwithme API"""
        if not self._connected and not self._connecting:
            self._connecting = True
            self._connectionRetires = 0

            # Get authentication token over HTTP POST
            while not self._authToken:
                if self._connectionRetires:
                    _LOGGER.debug(
                        "Couldn't get API details, retrying in %i minutes", self._connectionRetires
                    )
                    await asyncio.sleep(self._connectionRetires * 60)
                self._authToken = await self.poll_status()
                self._connectionRetires += 1

            _LOGGER.debug(
                "Connected to %s at %s",
                self._device_type,
                self._ip,
            )

            try:
                self._eventLoop.create_task(self._handle_packets())
                self._sendQueueTask = self._eventLoop.create_task(self._send_queue())
            except Exception as e:
                _LOGGER.error(f"{type(e)} Exception. {repr(e.args)} / {e}")
                self._connected = False
                self._connecting = False
                self._sendQueueTask.cancel()

    async def stop(self):
        """Public method for shutting down connectivity with the envisalink."""
        self._connected = False

        if self._ownSession:
            await self._webSession.close()

    def get_devices(self):
        """Public method to return the state of all IntesisHome devices"""
        return self._devices

    def get_device(self, deviceId):
        """Public method to return the state of the specified device"""
        return self._devices.get(str(deviceId))

    def get_device_property(self, deviceId, property_name):
        return self._devices[str(deviceId)].get(property_name)

    async def poll_status(self, sendcallback=False):
        """Public method to query IntesisHome for state of device. Notifies subscribers if sendCallback True."""
        get_token = {
            "command": "login",
            "data": {
                "username": self._username,
                "password": self._password,
            }
        }

        response = await self._process_query(get_token)
        self._authToken = response.get("id").get("sessionID")
        _LOGGER.debug(
            "Token: %s",
            self._authToken,
        )

        get_info = {
            "command": "getinfo",
            "data": {
                "sessionID": self._authToken,
            }
        }
        response = await self._process_query(get_info)
        device = response.get("info")
        deviceId = device["sn"]
        
        #todo id

        if deviceId in self._devices:
            self._devices[deviceId]["rssi"] = device["rssi"],
        else:
            self._devices[deviceId] = {
                "name": device["ownSSID"], #TODO
                "widgets": [42],
                "model": device["deviceModel"],
                "rssi": device["rssi"],
            }

        if "dataMap" not in self._devices[deviceId]:
            dp_names_reponse = None
            try:
                async with self._webSession.get(
                    url="http://" + self._ip + "/js/data/data.json",
                ) as resp:
                    dp_names_reponse = await resp.json(content_type="application/octet-stream")
                    #_LOGGER.debug(dp_names_reponse)
            except (aiohttp.client_exceptions.ClientError) as e:
                self._errorMessage = f"Error reading {self._device_type} datamap names: {e}"
                _LOGGER.error(f"{type(e)} Exception. {repr(e.args)} / {e}")
                raise IHConnectionError
            except (aiohttp.client_exceptions.ClientConnectorError) as e:
                raise IHConnectionError

            dp_names = dp_names_reponse.get("signals").get("uid")
            dp_labels = dp_names_reponse.get("signals").get("uidTextvalues")
            data_map = {}
            for attr, value in dp_names.items():
                if isinstance(value[1], str):
                    data_map[int(attr)] = {
                        "name": value[0],
                        "id": sanitizeTag(value[0]),
                        "values": dp_labels.get(value[1]),
                    }
                else:
                    data_map[int(attr)] = {
                        "name": value[0],
                        "id": sanitizeTag(value[0]),
                    }

            get_avail_data = {
                "command": "getavailabledatapoints",
                "data": {
                    "sessionID": self._authToken,
                    "uid": "all"
                }
            }
            response = await self._process_query(get_avail_data)
            #todo filter
            self._devices[deviceId]["dataMap"] = data_map
        
        await self._query_state(deviceId)

        if sendcallback:
            await self._send_update_callback(deviceId=str(deviceId))

        return self._authToken

    async def _query_state(self, deviceId):
        _LOGGER.debug("Refreshing " + deviceId + " state")
        get_data = {
            "command": "getdatapointvalue",
            "data": {
                "sessionID": self._authToken,
                "uid": "all"
            }
        }
        response = await self._process_query(get_data)

        for status in response.get("dpval"):
            self._update_device_state(deviceId, status["uid"], status["value"])

    async def _set_value(self, deviceId, uid, toSet):
        """Internal method to send a command to the API"""
        for attr, value in self._devices[deviceId]["dataMap"].items():
            if value.get("id") == uid:
                message = {
                    "deviceId": deviceId,
                    "uid": attr,
                    "value": self._get_param_value_id(value, toSet)
                }

                #message = (
                #    '{"command":"set","data":{"deviceId":%s,"uid":%i,"value":%i,"seqNo":0}}'
                #    % (deviceId, uid, value)
                #)
                self._sendQueue.put_nowait(message)
                return

    def _update_device_state(self, deviceId, uid, value):
        """Internal method to update the state table of IntesisHome/Airconwithme devices"""
        deviceId = str(deviceId)

        if uid in self._devices[deviceId]["dataMap"]:
            # If the value is null (32768), set as None
            if value == INTESIS_NULL:
                self._devices[deviceId][self._devices[deviceId]["dataMap"][uid]["name"]] = None
            else:
                # Translate known UIDs to configuration item names
                value_map = self._devices[deviceId]["dataMap"][uid].get("values")
                if value_map:
                    self._devices[deviceId][self._devices[deviceId]["dataMap"][uid]["id"]] = value_map.get(
                        value, value
                    )
                else:
                    self._devices[deviceId][self._devices[deviceId]["dataMap"][uid]["id"]] = value
        else:
            # Log unknown UIDs
            self._devices[deviceId][f"unknown_uid_{uid}"] = value

    async def set_preset_mode(self, deviceId, preset: str):
        """Internal method for setting the mode with a string value."""
        #if preset in COMMAND_MAP["climate_working_mode"]["values"]:
        #    await self._set_value(
        #        deviceId,
        #        COMMAND_MAP["climate_working_mode"]["uid"],
        #        COMMAND_MAP["climate_working_mode"]["values"][preset],
        #    )

    def get_run_hours(self, deviceId) -> str:
        """Public method returns the run hours of the IntesisHome controller."""
        run_hours = self._get_param_options(deviceId, PARAM_WORKING_HOURS)
        return run_hours

    async def set_mode(self, deviceId, mode: str):
        """Internal method for setting the mode with a string value."""
        await self._set_value(deviceId,
            PARAM_MODE,
            mode
        )

    async def set_temperature(self, deviceId, setpoint):
        """Public method for setting the temperature"""
        set_temp = int(setpoint * 10)
        await self._set_value(deviceId, PARAM_SETPOINT, set_temp)

    async def set_fan_speed(self, deviceId, fan: str):
        """Public method to set the fan speed"""
        await self._set_value(
            deviceId, PARAM_FAN_SPEED, fan
        )

    async def set_vertical_vane(self, deviceId, vane: str):
        """Public method to set the vertical vane"""
        #await self._set_value(
        #    deviceId, COMMAND_MAP["vvane"]["uid"], COMMAND_MAP["vvane"]["values"][vane]
        #)

    async def set_horizontal_vane(self, deviceId, vane: str):
        """Public method to set the horizontal vane"""
       # await self._set_value(
       #     deviceId, COMMAND_MAP["hvane"]["uid"], COMMAND_MAP["hvane"]["values"][vane]
       # )

    async def set_mode_heat(self, deviceId):
        """Public method to set device to heat asynchronously."""
        await self.set_mode(deviceId, "Heat")

    async def set_mode_cool(self, deviceId):
        """Public method to set device to cool asynchronously."""
        await self.set_mode(deviceId, "Cool")

    async def set_mode_fan(self, deviceId):
        """Public method to set device to fan asynchronously."""
        await self.set_mode(deviceId, "Fan")

    async def set_mode_auto(self, deviceId):
        """Public method to set device to auto asynchronously."""
        await self.set_mode(deviceId, "Auto")

    async def set_mode_dry(self, deviceId):
        """Public method to set device to dry asynchronously."""
        await self.set_mode(deviceId, "Dry")

    async def set_power_off(self, deviceId):
        """Public method to turn off the device asynchronously."""
        await self._set_value(
            deviceId, PARAM_POWER, "Off"
        )

    async def set_power_on(self, deviceId):
        """Public method to turn on the device asynchronously."""
        await self._set_value(
            deviceId, PARAM_POWER, "On"
        )

    def get_mode(self, deviceId) -> str:
        """Public method returns the current mode of operation."""
        return self._get_param_value(deviceId, PARAM_MODE)

    def get_mode_list(self, deviceId) -> list:
        """Public method to return the list of device modes."""
        return self._get_param_options(deviceId, PARAM_MODE)

    def get_fan_speed(self, deviceId):
        """Public method returns the current fan speed."""
        return self._get_param_value(deviceId, PARAM_FAN_SPEED)

    def get_fan_speed_list(self, deviceId):
        """Public method to return the list of possible fan speeds."""
        return self._get_param_options(deviceId, PARAM_FAN_SPEED) #todo : check config

    def get_device_name(self, deviceId) -> str:
        return self._devices[str(deviceId)].get("name")

    def get_power_state(self, deviceId) -> str:
        """Public method returns the current power state."""
        return self._get_param_value(deviceId, PARAM_POWER)

    def get_instant_power_consumption(self, deviceId) -> int:
        """Public method returns the current power state."""
        instant_power = self._get_param_value(deviceId, PARAM_POWER_CONSUMPTION)
        if instant_power:
            return int(instant_power)

    def get_total_power_consumption(self, deviceId) -> int:
        """Public method returns the current power state."""
        accumulated_power = self._get_param_value(deviceId, PARAM_ACC_POWER_CONSUMPTION)
        if accumulated_power:
            return int(accumulated_power)

    def get_cool_power_consumption(self, deviceId) -> int:
        """Public method returns the current power state."""
        aquarea_cool = self._get_param_value(deviceId, "aquarea_cool_consumption") #todo
        if aquarea_cool:
            return int(aquarea_cool)

    def get_heat_power_consumption(self, deviceId) -> int:
        """Public method returns the current power state."""
        aquarea_heat = self._get_param_value(deviceId, "aquarea_heat_consumption") #todo
        if aquarea_heat:
            return int(aquarea_heat)

    def get_tank_power_consumption(self, deviceId) -> int:
        """Public method returns the current power state."""
        aquarea_tank = self._get_param_value(deviceId, "aquarea_tank_consumption") #todo
        if aquarea_tank:
            return int(aquarea_tank)

    def get_preset_mode(self, deviceId) -> str:
        return self._get_param_value(deviceId, "climate_working_mode") #todo

    def is_on(self, deviceId) -> bool:
        """Return true if the controlled device is turned on"""
        return self._get_param_value(deviceId, PARAM_POWER) == "On"

    def has_vertical_swing(self, deviceId) -> bool:
        vvane_config = self._get_param_value(deviceId, "config_vertical_vanes") #Vane Up/Down Position
        return vvane_config and vvane_config > 1024

    def has_horizontal_swing(self, deviceId) -> bool:
        hvane_config = self._get_param_value(deviceId, "config_horizontal_vanes") #todo
        return hvane_config and hvane_config > 1024

    def has_setpoint_control(self, deviceId) -> bool:
        return self._has_feature(deviceId, PARAM_SETPOINT)

    def get_setpoint(self, deviceId) -> float:
        """Public method returns the target temperature."""
        setpoint = self._get_param_value(deviceId, PARAM_SETPOINT)
        if setpoint:
            setpoint = int(setpoint) / 10
        return setpoint

    def get_temperature(self, deviceId) -> float:
        """Public method returns the current temperature."""
        temperature = self._get_param_value(deviceId, PARAM_TEMPERATURE)
        if temperature:
            temperature = int(temperature) / 10
        return temperature

    def get_outdoor_temperature(self, deviceId) -> float:
        """Public method returns the current temperature."""
        outdoor_temp = self._get_param_value(deviceId, PARAM_OUTDOOR_TEMP)
        if outdoor_temp:
            outdoor_temp = int(outdoor_temp) / 10
        return outdoor_temp

    def get_max_setpoint(self, deviceId) -> float:
        """Public method returns the current maximum target temperature."""
        temperature = self._get_param_value(deviceId, PARAM_SETPOINT_MAX)
        if temperature:
            temperature = int(temperature) / 10
        return temperature

    def get_min_setpoint(self, deviceId) -> float:
        """Public method returns the current minimum target temperature."""
        temperature = self._get_param_value(deviceId, PARAM_SETPOINT_MIN)
        if temperature:
            temperature = int(temperature) / 10
        return temperature

    def get_rssi(self, deviceId) -> str:
        """Public method returns the current wireless signal strength."""
        rssi = self._get_param_value(deviceId, "rssi")
        return rssi

    def get_vertical_swing(self, deviceId) -> str:
        """Public method returns the current vertical vane setting."""
        swing = self._get_param_value(deviceId, "vvane") #todo
        return swing

    def get_horizontal_swing(self, deviceId) -> str:
        """Public method returns the current horizontal vane setting."""
        swing = self._get_param_value(deviceId, "hvane") #todo
        return swing

    async def _send_update_callback(self, deviceId=None):
        """Internal method to notify all update callback subscribers."""
        if self._updateCallbacks:
            for callback in self._updateCallbacks:
                await callback(device_id=deviceId)
        else:
            _LOGGER.debug("Update callback has not been set by client")

    @property
    def is_connected(self) -> bool:
        """Returns true if the TCP connection is established."""
        return self._connected

    @property
    def connection_retries(self) -> int:
        return self._connectionRetires

    @property
    def error_message(self) -> str:
        """Returns the last error message, or None if there were no errors."""
        return self._errorMessage

    @property
    def device_type(self) -> str:
        """Returns the device type (IntesisHome or airconwithme)."""
        return self._device_type

    @property
    def is_disconnected(self) -> bool:
        """Returns true when the TCP connection is disconnected and idle."""
        return not self._connected and not self._connecting

    async def add_update_callback(self, method):
        """Public method to add a callback subscriber."""
        self._updateCallbacks.append(method)


def help():
    print("syntax: pyintesishome [options] command [command_args]")
    print("options:")
    print("   --user <username>       ... username on user.intesishome.com")
    print("   --password <password>   ... password on user.intesishome.com")
    print("   --id <number>           ... specify device id of unit to control")
    print()
    print("commands: show")
    print("    show                   ... show current state")
    print()
    print("examples:")
    print("    pyintesishome.py --user joe@user.com --password swordfish show")


async def main(loop):
    logging.basicConfig(level=logging.DEBUG)
    parser = argparse.ArgumentParser(description="Commands: mode fan temp")
    parser.add_argument(
        "--user",
        type=str,
        dest="user",
        help="username for user.intesishome.com",
        metavar="USER",
        default=None,
    )
    parser.add_argument(
        "--password",
        type=str,
        dest="password",
        help="password for user.intesishome.com",
        metavar="PASSWORD",
        default=None,
    )
    parser.add_argument(
        "--device",
        type=str,
        dest="device",
        help="IntesisHome or airconwithme",
        metavar="IntesisHome or airconwithme",
        default=DEVICE_INTESISHOME,
    )
    args = parser.parse_args()

    if (not args.user) or (not args.password):
        help()
        sys.exit(0)

    controller = IntesisHome(
        args.user, args.password, loop=loop, device_type=args.device
    )
    await controller.connect()
    print(repr(controller.get_devices()))
    await controller.stop()


if __name__ == "__main__":
    import time

    s = time.perf_counter()
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(main(loop))
    elapsed = time.perf_counter() - s
    print(f"{__file__} executed in {elapsed:0.2f} seconds.")
