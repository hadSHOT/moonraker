# Integration with Spoolman
#
# Copyright (C) 2023 Daniel Hultgren <daniel.cf.hultgren@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import datetime
import logging
import re
import tornado.websocket as tornado_ws
from ..common import RequestType, Sentinel
from ..utils import json_wrapper as jsonw
from typing import (
    TYPE_CHECKING,
    Dict,
    Any,
    Optional,
    Union
)

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from .http_client import HttpClient, HttpResponse
    from .database import MoonrakerDatabase
    from .announcements import Announcements
    from .klippy_apis import KlippyAPI as APIComp
    from tornado.websocket import WebSocketClientConnection

DB_NAMESPACE = "moonraker"
ACTIVE_SPOOL_KEY = "spoolman.spool_id"
CONNECTION_ERROR_LOG_TIME = 60.

class SpoolManager:
    def __init__(self, config: ConfigHelper):
        self.server = config.get_server()
        self.eventloop = self.server.get_event_loop()
        self._get_spoolman_urls(config)
        self.sync_rate_seconds = config.getint("sync_rate", default=5, minval=1)
        self.last_sync_time = datetime.datetime.now()
        self.extruded_lock = asyncio.Lock()
        self.spoolman_ws: Optional[WebSocketClientConnection] = None
        self.connection_task: Optional[asyncio.Task] = None
        self.spool_check_task: Optional[asyncio.Task] = None
        self.spool_lock = asyncio.Lock()
        self.ws_connected: bool = False
        self.reconnect_delay: float = 2.
        self.is_closing: bool = False
        self.spool_id: Optional[int] = None
        self.extruded: float = 0
        self._error_logged: bool = False
        self._highest_epos: float = 0
        self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
        self.http_client: HttpClient = self.server.lookup_component("http_client")
        self.database: MoonrakerDatabase = self.server.lookup_component("database")
        announcements: Announcements = self.server.lookup_component("announcements")
        announcements.register_feed("spoolman")
        self._register_notifications()
        self._register_listeners()
        self._register_endpoints()
        self.server.register_remote_method(
            "spoolman_set_active_spool", self.set_active_spool
        )

    def _get_spoolman_urls(self, config: ConfigHelper) -> None:
        orig_url = config.get('server')
        url_match = re.match(r"(?i:(?P<scheme>https?)://)?(?P<host>.+)", orig_url)
        if url_match is None:
            raise config.error(
                f"Section [spoolman], Option server: {orig_url}: Invalid URL format"
            )
        scheme = url_match["scheme"] or "http"
        host = url_match["host"].rstrip("/")
        ws_scheme = "wss" if scheme == "https" else "ws"
        self.spoolman_url = f"{scheme}://{host}/api"
        self.ws_url = f"{ws_scheme}://{host}/api/v1/spool"

    def _register_notifications(self):
        self.server.register_notification("spoolman:active_spool_set")

    def _register_listeners(self):
        self.server.register_event_handler(
            "server:klippy_ready", self._handle_klippy_ready
        )

    def _register_endpoints(self):
        self.server.register_endpoint(
            "/server/spoolman/spool_id",
            RequestType.GET | RequestType.POST,
            self._handle_spool_id_request,
        )
        self.server.register_endpoint(
            "/server/spoolman/proxy",
            RequestType.POST,
            self._proxy_spoolman_request,
        )

    async def component_init(self) -> None:
        self.spool_id = await self.database.get_item(
            DB_NAMESPACE, ACTIVE_SPOOL_KEY, None
        )
        self.connection_task = self.eventloop.create_task(self._connect_websocket())

    async def _connect_websocket(self) -> None:
        log_connect: bool = True
        last_err: Exception = Exception()
        while not self.is_closing:
            if log_connect:
                logging.info(f"Connecting To Spoolman: {self.ws_url}")
                log_connect = False
            try:
                self.spoolman_ws = await tornado_ws.websocket_connect(
                    self.ws_url,
                    connect_timeout=5.,
                    ping_interval=20.,
                    ping_timeout=60.
                )
                setattr(self.spoolman_ws, "on_ping", self._on_ws_ping)
                cur_time = self.eventloop.get_loop_time()
                self._last_ping_received = cur_time
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if type(last_err) is not type(e) or last_err.args != e.args:
                    logging.exception("Failed to connect to Spoolman")
                    last_err = e
            else:
                self.ws_connected = True
                self._error_logged = False
                logging.info("Connected to Spoolman Spool Manager")
                if self.spool_id is not None:
                    self._cancel_spool_check_task()
                    coro = self._check_spool_deleted()
                    self.spool_check_task = self.eventloop.create_task(coro)
                await self._read_messages()
                log_connect = True
                last_err = Exception()
            if not self.is_closing:
                await asyncio.sleep(self.reconnect_delay)

    async def _read_messages(self) -> None:
        message: Union[str, bytes, None]
        while self.spoolman_ws is not None:
            message = await self.spoolman_ws.read_message()
            if isinstance(message, str):
                self._decode_message(message)
            elif message is None:
                self.ws_connected = False
                cur_time = self.eventloop.get_loop_time()
                ping_time: float = cur_time - self._last_ping_received
                reason = code = None
                if self.spoolman_ws is not None:
                    reason = self.spoolman_ws.close_reason
                    code = self.spoolman_ws.close_code
                logging.info(
                    f"Spoolman Disconnected - Code: {code}, Reason: {reason}, "
                    f"Server Ping Time Elapsed: {ping_time}"
                )
                self.spoolman_ws = None
                break

    def _decode_message(self, message: str) -> None:
        event: Dict[str, Any] = jsonw.loads(message)
        if event.get("resource") != "spool":
            return
        if self.spool_id is not None and event.get("type") == "deleted":
            payload: Dict[str, Any] = event.get("payload", {})
            if payload.get("id") == self.spool_id:
                self.eventloop.create_task(self.set_active_spool(Sentinel.MISSING))

    def _cancel_spool_check_task(self) -> None:
        if self.spool_check_task is None or self.spool_check_task.done():
            return
        self.spool_check_task.cancel()

    async def _check_spool_deleted(self) -> None:
        if self.spool_id is not None:
            response = await self.http_client.get(
                f"{self.spoolman_url}/v1/spool/{self.spool_id}",
                connect_timeout=1., request_timeout=2.
            )
            if response.status_code == 404:
                logging.info(f"Spool ID {self.spool_id} not found, setting to None")
                await self.set_active_spool(Sentinel.MISSING)
            elif response.has_error():
                err_msg = self._get_response_error(response)
                logging.info(f"Attempt to check spool status failed: {err_msg}")
            else:
                logging.info(f"Found Spool ID {self.spool_id} on spoolman instance")
        self.spool_check_task = None

    def connected(self) -> bool:
        return self.ws_connected

    def _on_ws_ping(self, data: bytes = b"") -> None:
        self._last_ping_received = self.eventloop.get_loop_time()

    async def _handle_klippy_ready(self):
        result = await self.klippy_apis.subscribe_objects(
            {"toolhead": ["position"]}, self._handle_status_update, {}
        )
        initial_e_pos = self._eposition_from_status(result)
        logging.debug(f"Initial epos: {initial_e_pos}")
        if initial_e_pos is not None:
            self._highest_epos = initial_e_pos
        else:
            logging.error("Spoolman integration unable to subscribe to epos")
            raise self.server.error("Unable to subscribe to e position")

    def _get_response_error(self, response: HttpResponse) -> str:
        err_msg = f"HTTP error: {response.status_code} {response.error}"
        try:
            resp = response.json()
            assert isinstance(resp, dict)
            json_msg: str = resp["message"]
        except Exception:
            pass
        else:
            err_msg += f", Spoolman message: {json_msg}"
        return err_msg

    def _eposition_from_status(self, status: Dict[str, Any]) -> Optional[float]:
        position = status.get("toolhead", {}).get("position", [])
        return position[3] if len(position) > 3 else None

    async def _handle_status_update(self, status: Dict[str, Any], _: float) -> None:
        epos = self._eposition_from_status(status)
        if epos and epos > self._highest_epos:
            async with self.extruded_lock:
                self.extruded += epos - self._highest_epos
                self._highest_epos = epos

            now = datetime.datetime.now()
            difference = now - self.last_sync_time
            if difference.total_seconds() > self.sync_rate_seconds:
                self.last_sync_time = now
                logging.debug("Sync period elapsed, tracking usage")
                await self.track_filament_usage()

    async def set_active_spool(self, spool_id: Union[int, Sentinel, None]) -> None:
        async with self.spool_lock:
            deleted_spool = False
            if spool_id is Sentinel.MISSING:
                spool_id = None
                deleted_spool = True
            if self.spool_id == spool_id:
                logging.info(f"Spool ID already set to: {spool_id}")
                return
            # Store the current spool usage before switching, unless it has been deleted
            if not deleted_spool:
                if self.spool_id is not None:
                    await self.track_filament_usage()
                elif spool_id is not None:
                    # No need to track, just reset extrusion
                    async with self.extruded_lock:
                        self.extruded = 0
            self.spool_id = spool_id
            self.database.insert_item(DB_NAMESPACE, ACTIVE_SPOOL_KEY, spool_id)
            self.server.send_event(
                "spoolman:active_spool_set", {"spool_id": spool_id}
            )
            logging.info(f"Setting active spool to: {spool_id}")

    async def track_filament_usage(self):
        spool_id = self.spool_id
        if spool_id is None:
            logging.debug("No active spool, skipping tracking")
            return
        async with self.extruded_lock:
            if self.extruded > 0 and self.ws_connected:
                used_length = self.extruded

                logging.debug(
                    f"Sending spool usage: "
                    f"ID: {spool_id}, "
                    f"Length: {used_length:.3f}mm, "
                )

                response = await self.http_client.request(
                    method="PUT",
                    url=f"{self.spoolman_url}/v1/spool/{spool_id}/use",
                    body={
                        "use_length": used_length,
                    },
                )
                if response.has_error():
                    if response.status_code == 404:
                        self._error_logged = False
                        logging.info(
                            f"Spool ID {self.spool_id} not found, setting to None"
                        )
                        coro = self.set_active_spool(Sentinel.MISSING)
                        self.eventloop.create_task(coro)
                    elif not self._error_logged:
                        error_msg = self._get_response_error(response)
                        self._error_logged = True
                        logging.info(
                            f"Failed to update extrusion for spool id {spool_id}, "
                            f"received {error_msg}"
                        )
                    return
                self._error_logged = False
                self.extruded = 0

    async def _handle_spool_id_request(self, web_request: WebRequest):
        if web_request.get_request_type() == RequestType.POST:
            spool_id = web_request.get_int("spool_id", None)
            await self.set_active_spool(spool_id)
        # For GET requests we will simply return the spool_id
        return {"spool_id": self.spool_id}

    async def _proxy_spoolman_request(self, web_request: WebRequest):
        method = web_request.get_str("request_method")
        path = web_request.get_str("path")
        query = web_request.get_str("query", None)
        body = web_request.get("body", None)
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise self.server.error(f"Invalid HTTP method: {method}")
        if body is not None and method == "GET":
            raise self.server.error("GET requests cannot have a body")
        if len(path) < 4 or path[:4] != "/v1/":
            raise self.server.error(
                "Invalid path, must start with the API version, e.g. /v1"
            )
        query = f"?{query}" if query is not None else ""
        full_url = f"{self.spoolman_url}{path}{query}"
        if not self.ws_connected:
            raise self.server.error("Spoolman server not available", 503)
        logging.debug(f"Proxying {method} request to {full_url}")
        response = await self.http_client.request(
            method=method,
            url=full_url,
            body=body,
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        self.is_closing = True
        if self.spoolman_ws is not None:
            self.spoolman_ws.close(1001, "Moonraker Shutdown")
        self._cancel_spool_check_task()
        if self.connection_task is None or self.connection_task.done():
            return
        try:
            await asyncio.wait_for(self.connection_task, 2.)
        except asyncio.TimeoutError:
            pass

def load_component(config: ConfigHelper) -> SpoolManager:
    return SpoolManager(config)
