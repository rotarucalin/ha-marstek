"""Marstek API implementation using UDP JSON-RPC protocol."""

import json
import logging
import socket
from contextlib import contextmanager
from ipaddress import IPv4Address
from threading import Lock
from time import monotonic, sleep

from .identity import normalize_mac

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0
# Conservative workaround for firmware sensitive to closely spaced requests.
REQUEST_GAP_SECONDS = 2.5


class MarstekAPI:
    """API client for Marstek devices using UDP JSON-RPC."""

    def __init__(self, host: str, port: int = 30000, timeout: float = DEFAULT_TIMEOUT):
        """Initialize the API client."""
        self.host = host
        self.port = port
        self.timeout = timeout
        self._request_id = 0
        self._request_lock = Lock()
        self._next_request_at = 0.0

    @contextmanager
    def _request_slot(self):
        """Serialize device I/O and let the firmware settle between requests.

        API methods run in Home Assistant's executor, so waiting here does not
        block its event loop. Polls and control commands share the same gate.
        """
        with self._request_lock:
            delay = self._next_request_at - monotonic()
            if delay > 0:
                sleep(delay)
            try:
                yield
            finally:
                # A timeout or rejected request also needs a quiet interval.
                self._next_request_at = monotonic() + REQUEST_GAP_SECONDS

    def _get_next_id(self) -> int:
        """Get next request ID."""
        self._request_id += 1
        return self._request_id

    def _log_request_error(self, method: str, error: str) -> None:
        """Keep mode-request details at DEBUG; the coordinator logs recovery."""
        _LOGGER.log(
            logging.DEBUG if method == "ES.SetMode" else logging.ERROR,
            "Marstek request failed: host=%s port=%s method=%s error=%s",
            self.host,
            self.port,
            method,
            error,
        )

    def _send_request(self, method: str, params: dict | None = None) -> dict | None:
        """Send a UDP JSON-RPC request."""
        with self._request_slot():
            return self._send_request_locked(method, params)

    def _send_request_locked(self, method: str, params: dict | None) -> dict | None:
        """Exchange one request while holding the shared device I/O gate."""
        if params is None:
            params = {"id": 0}

        request = {
            "id": self._get_next_id(),
            "method": method,
            "params": params,
        }

        try:
            # Pin sending and validation to the same IPv4 address for this request.
            resolved_ip = socket.gethostbyname(self.host)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(self.timeout)
                message = json.dumps(request).encode("utf-8")
                _LOGGER.debug(
                    "Marstek TX: host=%s ip=%s port=%s request_id=%s method=%s params=%s",
                    self.host,
                    resolved_ip,
                    self.port,
                    request["id"],
                    request["method"],
                    request["params"],
                )
                sock.sendto(message, (resolved_ip, self.port))
                deadline = monotonic() + self.timeout
                while True:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    sock.settimeout(remaining)
                    data, addr = sock.recvfrom(4096)
                    if addr[0] != resolved_ip:
                        _LOGGER.debug(
                            "Ignored packet from unexpected sender: sender=%s "
                            "host=%s port=%s method=%s request_id=%s",
                            addr,
                            self.host,
                            self.port,
                            method,
                            request["id"],
                        )
                        continue
                    try:
                        response = json.loads(data.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as err:
                        _LOGGER.debug(
                            "Ignored malformed packet: host=%s port=%s method=%s "
                            "request_id=%s error=%s",
                            self.host,
                            self.port,
                            method,
                            request["id"],
                            err,
                        )
                        continue
                    if not isinstance(response, dict):
                        _LOGGER.debug(
                            "Ignored malformed packet: host=%s port=%s method=%s "
                            "request_id=%s expected JSON object",
                            self.host,
                            self.port,
                            method,
                            request["id"],
                        )
                        continue
                    response_id = response.get("id")
                    # Requests use integer IDs, which the device must echo.
                    # Exact type checking also excludes bools and floats.
                    if type(response_id) is not int or response_id != request["id"]:
                        _LOGGER.debug(
                            "Ignored packet with mismatching request ID: "
                            "host=%s port=%s method=%s request_id=%s response_id=%r response=%r",
                            self.host,
                            self.port,
                            method,
                            request["id"],
                            response_id,
                            response,
                        )
                        continue
                    break

            _LOGGER.debug(
                "Marstek RX: host=%s method=%s request_id=%s response=%s",
                self.host,
                method,
                request["id"],
                response,
            )

            if "error" in response:
                self._log_request_error(
                    method,
                    f"API error: {response['error'].get('code')} - "
                    f"{response['error'].get('message')}",
                )
                return None

            return response.get("result")

        except TimeoutError:
            self._log_request_error(method, "Timeout communicating with device")
            return None
        except Exception as err:  # noqa: BLE001 - Preserve the best-effort API contract.
            self._log_request_error(method, f"{type(err).__name__}: {err}")
            return None

    def discover_devices(
        self, broadcast_address: str = "255.255.255.255"
    ) -> list[dict]:
        """Discover Marstek devices on the network."""
        with self._request_slot():
            return self._discover_devices_locked(broadcast_address)

    def _discover_devices_locked(self, broadcast_address: str) -> list[dict]:
        """Collect discovery replies while holding the device I/O gate."""
        request = {
            "id": self._get_next_id(),
            "method": "Marstek.GetDevice",
            "params": {"ble_mac": "0"},
        }

        devices = []
        seen_macs = set()

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(self.timeout)

                message = json.dumps(request).encode("utf-8")
                _LOGGER.debug(
                    "Marstek TX: host=%s ip=%s port=%s request_id=%s method=%s params=%s",
                    broadcast_address,
                    broadcast_address,
                    self.port,
                    request["id"],
                    request["method"],
                    request["params"],
                )
                sock.sendto(message, (broadcast_address, self.port))
                deadline = monotonic() + self.timeout

                try:
                    while True:
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            break
                        sock.settimeout(remaining)
                        data, addr = sock.recvfrom(4096)
                        device_info = self._parse_discovery_response(
                            data, addr[0], request["id"]
                        )
                        if device_info is None:
                            continue
                        mac = normalize_mac(device_info["ble_mac"])
                        if mac in seen_macs:
                            _LOGGER.debug(
                                "Ignored discovery packet: duplicate device sender=%s ble_mac=%s",
                                addr[0],
                                mac,
                            )
                            continue
                        seen_macs.add(mac)
                        devices.append(device_info)

                except TimeoutError:
                    pass

        except Exception as err:  # noqa: BLE001 - Discovery returns any devices found.
            _LOGGER.error("Error during device discovery: %s", err)

        return devices

    @staticmethod
    def _parse_discovery_response(
        data: bytes, sender: str, request_id: int
    ) -> dict | None:
        """Validate one broadcast reply without affecting other responders."""
        try:
            response = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            # ValueError also covers JSON numbers exceeding Python's digit limit.
            _LOGGER.debug("Ignored discovery packet: invalid JSON sender=%s", sender)
            return None

        reason = None
        if not isinstance(response, dict):
            reason = "non-object response"
        elif "id" not in response:
            reason = "missing request ID"
        elif type(response["id"]) is not int:
            # Match normal requests: bools, floats and strings are not integer IDs.
            reason = "invalid request ID type"
        elif response["id"] != request_id:
            reason = "mismatching request ID"
        elif "error" in response:
            error = response["error"]
            if (
                isinstance(error, dict)
                and type(error.get("code")) is int
                and isinstance(error.get("message"), str)
            ):
                reason = f"API error response: {error['code']} - {error['message']}"
            else:
                reason = "invalid API error response"
        elif not isinstance(response.get("result"), dict):
            reason = "missing/invalid result"

        if reason is not None:
            _LOGGER.debug(
                "Ignored discovery packet: %s sender=%s request_id=%s",
                reason,
                sender,
                request_id,
            )
            return None

        result = response["result"]
        device = result.get("device")
        if (
            not isinstance(device, str)
            or not device.strip()
            or normalize_mac(result.get("ble_mac")) is None
            or not isinstance(result.get("ip"), str)
        ):
            _LOGGER.debug(
                "Ignored discovery packet: missing/invalid required device fields sender=%s",
                sender,
            )
            return None

        try:
            reported_ip = IPv4Address(result["ip"])
            source_ip = IPv4Address(sender)
        except ValueError:
            _LOGGER.debug(
                "Ignored discovery packet: invalid IP address sender=%s", sender
            )
            return None
        if any(
            ip.is_unspecified or ip.is_multicast or ip == IPv4Address("255.255.255.255")
            for ip in (reported_ip, source_ip)
        ):
            _LOGGER.debug(
                "Ignored discovery packet: non-unicast IP address sender=%s", sender
            )
            return None

        # Firmware may advertise an old address; always connect to the UDP sender.
        if reported_ip != source_ip:
            _LOGGER.debug(
                "Discovery IP mismatch: reported=%s sender=%s; using sender",
                reported_ip,
                sender,
            )
        return {**result, "ip": sender}

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
        return isinstance(result, dict) and result.get("set_result") is True

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
        return isinstance(result, dict) and result.get("set_result") is True

    def set_es_mode_manual(
        self,
        time_num: int,
        start_time: str,
        end_time: str,
        week_set: int,
        power: int,
        enable: int = 1,
        manual_set: int | None = None,
    ) -> bool:
        """Set energy system to Manual mode.

        `manual_set` is only accepted by the Venus E mini, so it is omitted
        unless the caller's capability profile declares support for it.
        """
        manual_cfg = {
            "time_num": time_num,
            "start_time": start_time,
            "end_time": end_time,
            "week_set": week_set,
            "power": power,
            "enable": enable,
        }
        if manual_set is not None:
            manual_cfg["manual_set"] = manual_set

        params = {
            "id": 0,
            "config": {
                "mode": "Manual",
                "manual_cfg": manual_cfg,
            },
        }
        result = self._send_request("ES.SetMode", params)
        return isinstance(result, dict) and result.get("set_result") is True

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
        return isinstance(result, dict) and result.get("set_result") is True
