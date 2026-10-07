"""Marstek API implementation using UDP JSON-RPC protocol."""

import json
import logging
import socket
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import IPv4Address
from threading import Lock
from time import monotonic, sleep

from .identity import normalize_mac

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0
# Conservative workaround for firmware sensitive to closely spaced requests.
REQUEST_GAP_SECONDS = 2.5
# Replies to timed-out requests are still correlated within this bounded window.
LATE_RESPONSE_WINDOW_SECONDS = 120.0
MAX_RECENT_REQUESTS = 64
# A mode write supersedes control telemetry requested before it.
CONTROL_READ_METHODS = frozenset({"ES.GetStatus", "ES.GetMode", "Bat.GetStatus"})

STATE_PENDING = "pending"
STATE_COMPLETED = "completed"
STATE_TIMED_OUT = "timed_out"
STATE_FAILED = "failed"
STATE_LATE = "late_received"


@dataclass
class RecentRequest:
    """One transmitted request, kept briefly so a late reply can be correlated."""

    request_id: int
    method: str
    sent_at: float
    endpoint: tuple[str, int]
    kind: str  # "read" or "write"
    state: str = STATE_PENDING


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
        # One socket keeps the local port stable, so replies that miss their
        # deadline are still delivered and can be matched to their request.
        self._socket: socket.socket | None = None
        # Guards correlation state, which the event loop also reads.
        self._state_lock = Lock()
        self._recent: OrderedDict[int, RecentRequest] = OrderedDict()
        # Per read method: newest request whose data was accepted, and the
        # request at or below which late data is obsolete (invalidated).
        self._newest_read: dict[str, int] = {}
        self._read_floor: dict[str, int] = {}
        self._late_results: dict[str, tuple[int, dict]] = {}

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

    def close(self) -> None:
        """Release the device socket; a later request opens a new one."""
        with self._request_lock:
            self._close_socket()

    def _close_socket(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def _connection(self) -> socket.socket:
        """Return the shared request socket, opening it on first use."""
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return self._socket

    def _prune_recent(self, now: float) -> None:
        """Drop requests beyond the correlation window or the size bound."""
        while self._recent:
            oldest = next(iter(self._recent.values()))
            if (
                len(self._recent) <= MAX_RECENT_REQUESTS
                and now - oldest.sent_at <= LATE_RESPONSE_WINDOW_SECONDS
            ):
                break
            self._recent.popitem(last=False)

    def _register_request(
        self, request: dict, endpoint: tuple[str, int]
    ) -> RecentRequest:
        method = request["method"]
        entry = RecentRequest(
            request["id"],
            method,
            monotonic(),
            endpoint,
            "read" if ".Get" in method else "write",
        )
        with self._state_lock:
            self._recent[entry.request_id] = entry
            self._prune_recent(entry.sent_at)
            if method == "ES.SetMode":
                for read_method in CONTROL_READ_METHODS:
                    self._read_floor[read_method] = entry.request_id
                    self._late_results.pop(read_method, None)
        return entry

    def _finish_request(self, entry: RecentRequest | None, state: str) -> None:
        if entry is not None:
            with self._state_lock:
                entry.state = state

    def _record_read(self, method: str, request_id: int) -> None:
        """Note fresh data, discarding an older late reply not yet collected."""
        with self._state_lock:
            if request_id > self._newest_read.get(method, 0):
                self._newest_read[method] = request_id
            superseded = self._late_results.pop(method, None)
        if superseded is not None:
            _LOGGER.debug(
                "Ignored late response because newer data exists: host=%s "
                "request_id=%s method=%s newer_request_id=%s",
                self.host,
                superseded[0],
                method,
                request_id,
            )

    def invalidate_reads(self, method: str) -> None:
        """Reject late replies to requests sent before the caller dropped data."""
        with self._state_lock:
            self._read_floor[method] = self._request_id
            self._late_results.pop(method, None)

    def take_late_results(self) -> dict[str, tuple[int, dict]]:
        """Return accepted late read results as `{method: (request_id, result)}`."""
        with self._state_lock:
            results, self._late_results = self._late_results, {}
        return results

    def _handle_unmatched(self, response: dict, sender: str, request: dict) -> None:
        """Classify a reply for another request; only a timed-out one is late."""
        response_id = response.get("id")
        # Requests use integer IDs, which the device must echo.
        # Exact type checking also excludes bools and floats.
        if type(response_id) is not int:
            _LOGGER.debug(
                "Ignored packet with invalid request ID: host=%s port=%s "
                "method=%s request_id=%s response_id=%r response=%r",
                self.host,
                self.port,
                request["method"],
                request["id"],
                response_id,
                response,
            )
            return
        if response_id == 0:
            _LOGGER.debug(
                "Ignored malformed response with id=0: host=%s port=%s "
                "method=%s request_id=%s response=%r",
                self.host,
                self.port,
                request["method"],
                request["id"],
                response,
            )
            return

        now = monotonic()
        newer_id = None
        with self._state_lock:
            self._prune_recent(now)
            entry = self._recent.get(response_id)
            if (
                entry is None
                or entry.state != STATE_TIMED_OUT
                or entry.endpoint[0] != sender
            ):
                entry = None
            else:
                entry.state = STATE_LATE
                result = response.get("result")
                usable = (
                    entry.kind == "read"
                    and "error" not in response
                    and isinstance(result, dict)
                )
                if usable:
                    newer_id = max(
                        self._newest_read.get(entry.method, 0),
                        self._read_floor.get(entry.method, 0),
                    )
                    if newer_id < response_id:
                        newer_id = None
                        self._newest_read[entry.method] = response_id
                        self._late_results[entry.method] = (response_id, result)

        if entry is None:
            _LOGGER.debug(
                "Ignored response with unknown or expired request ID: host=%s "
                "response_id=%s current_request_id=%s current_method=%s "
                "response=%r",
                self.host,
                response_id,
                request["id"],
                request["method"],
                response,
            )
        elif not usable:
            # Writes are only correlated: a returned command result stands, and
            # acknowledgement still comes from mode/status verification.
            _LOGGER.debug(
                "Correlated late response without applying it: host=%s "
                "request_id=%s method=%s type=%s age=%.1fs response=%r",
                self.host,
                response_id,
                entry.method,
                entry.kind,
                now - entry.sent_at,
                response,
            )
        elif newer_id is not None:
            _LOGGER.debug(
                "Ignored late response because newer data exists: host=%s "
                "request_id=%s method=%s age=%.1fs newer_request_id=%s",
                self.host,
                response_id,
                entry.method,
                now - entry.sent_at,
                newer_id,
            )
        else:
            _LOGGER.debug(
                "Accepted late response: host=%s request_id=%s method=%s "
                "age=%.1fs current_request_id=%s",
                self.host,
                response_id,
                entry.method,
                now - entry.sent_at,
                request["id"],
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
        entry = None

        try:
            # Pin sending and validation to the same IPv4 address for this request.
            resolved_ip = socket.gethostbyname(self.host)
            sock = self._connection()
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
            entry = self._register_request(request, (resolved_ip, self.port))
            sock.sendto(message, (resolved_ip, self.port))
            deadline = monotonic() + self.timeout
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError
                sock.settimeout(remaining)
                try:
                    data, addr = sock.recvfrom(4096)
                except ConnectionResetError:
                    # Windows reports ICMP errors for earlier datagrams here.
                    continue
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
                if type(response_id) is int and response_id == request["id"]:
                    break
                self._handle_unmatched(response, addr[0], request)

            self._finish_request(entry, STATE_COMPLETED)
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

            result = response.get("result")
            if entry.kind == "read" and result is not None:
                self._record_read(method, request["id"])
            return result

        except TimeoutError:
            # Keep the socket: the reply may still arrive and be correlated.
            self._finish_request(entry, STATE_TIMED_OUT)
            self._log_request_error(method, "Timeout communicating with device")
            return None
        except Exception as err:  # noqa: BLE001 - Preserve the best-effort API contract.
            self._finish_request(entry, STATE_FAILED)
            self._close_socket()
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
