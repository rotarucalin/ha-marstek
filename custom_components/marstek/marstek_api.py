"""Marstek API implementation using UDP JSON-RPC protocol."""
import json
import logging
import socket

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0


class MarstekAPI:
    """API client for Marstek devices using UDP JSON-RPC."""

    def __init__(self, host: str, port: int = 30000, timeout: float = DEFAULT_TIMEOUT):
        """Initialize the API client."""
        self.host = host
        self.port = port
        self.timeout = timeout
        self._request_id = 0

    def _get_next_id(self) -> int:
        """Get next request ID."""
        self._request_id += 1
        return self._request_id

    def _send_request(self, method: str, params: dict | None = None) -> dict | None:
        """Send a UDP JSON-RPC request."""
        if params is None:
            params = {"id": 0}

        request = {
            "id": self._get_next_id(),
            "method": method,
            "params": params,
        }

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(self.timeout)
                message = json.dumps(request).encode("utf-8")
                sock.sendto(message, (self.host, self.port))
                data, _ = sock.recvfrom(4096)
                response = json.loads(data.decode("utf-8"))

            if "error" in response:
                _LOGGER.error(
                    "API error: %s - %s",
                    response["error"].get("code"),
                    response["error"].get("message"),
                )
                return None

            return response.get("result")

        except socket.timeout:
            _LOGGER.error("Timeout communicating with device at %s:%s", self.host, self.port)
            return None
        except json.JSONDecodeError as err:
            _LOGGER.error("Failed to decode JSON response: %s", err)
            return None
        except Exception as err:
            _LOGGER.error("Error communicating with device: %s", err)
            return None

    def discover_devices(self, broadcast_address: str = "255.255.255.255") -> list[dict]:
        """Discover Marstek devices on the network."""
        request = {
            "id": self._get_next_id(),
            "method": "Marstek.GetDevice",
            "params": {"ble_mac": "0"},
        }

        devices = []

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(self.timeout)

                message = json.dumps(request).encode("utf-8")
                sock.sendto(message, (broadcast_address, self.port))

                try:
                    while True:
                        data, addr = sock.recvfrom(4096)
                        response = json.loads(data.decode("utf-8"))

                        if "result" in response:
                            device_info = response["result"]
                            device_info["ip"] = addr[0]
                            devices.append(device_info)

                except socket.timeout:
                    pass

        except Exception as err:
            _LOGGER.error("Error during device discovery: %s", err)

        return devices

    def get_device_info(self) -> dict | None:
        """Get device information."""
        return self._send_request("Marstek.GetDevice", {"ble_mac": "0"})

    def get_wifi_status(self) -> dict | None:
        """Get WiFi status."""
        return self._send_request("Wifi.GetStatus", {"id": 0})

    def get_ble_status(self) -> dict | None:
        """Get Bluetooth status."""
        return self._send_request("BLE.GetStatus", {"id": 0})

    def get_battery_status(self) -> dict | None:
        """Get battery status."""
        return self._send_request("Bat.GetStatus", {"id": 0})

    def get_pv_status(self) -> dict | None:
        """Get photovoltaic status."""
        return self._send_request("PV.GetStatus", {"id": 0})

    def get_es_status(self) -> dict | None:
        """Get energy system status."""
        return self._send_request("ES.GetStatus", {"id": 0})

    def get_es_mode(self) -> dict | None:
        """Get energy system mode."""
        return self._send_request("ES.GetMode", {"id": 0})

    def get_em_status(self) -> dict | None:
        """Get energy meter status."""
        return self._send_request("EM.GetStatus", {"id": 0})

    def set_es_mode_auto(self) -> bool:
        """Set energy system to Auto mode."""
        params = {
            "id": 0,
            "config": {
                "mode": "Auto",
                "auto_cfg": {"enable": 1},
            },
        }
        result = self._send_request("ES.SetMode", params)
        return result is not None and result.get("set_result", False)

    def set_es_mode_ai(self) -> bool:
        """Set energy system to AI mode."""
        params = {
            "id": 0,
            "config": {
                "mode": "AI",
                "ai_cfg": {"enable": 1},
            },
        }
        result = self._send_request("ES.SetMode", params)
        return result is not None and result.get("set_result", False)

    def set_es_mode_manual(
        self,
        time_num: int,
        start_time: str,
        end_time: str,
        week_set: int,
        power: int,
        enable: int = 1,
    ) -> bool:
        """Set energy system to Manual mode."""
        params = {
            "id": 0,
            "config": {
                "mode": "Manual",
                "manual_cfg": {
                    "time_num": time_num,
                    "start_time": start_time,
                    "end_time": end_time,
                    "week_set": week_set,
                    "power": power,
                    "enable": enable,
                },
            },
        }
        result = self._send_request("ES.SetMode", params)
        return result is not None and result.get("set_result", False)

    def set_es_mode_passive(self, power: int, cd_time: int = 3600) -> bool:
        """Set energy system to Passive mode."""
        params = {
            "id": 0,
            "config": {
                "mode": "Passive",
                "passive_cfg": {
                    "power": power,
                    "cd_time": cd_time,
                },
            },
        }
        result = self._send_request("ES.SetMode", params)
        return result is not None and result.get("set_result", False)
