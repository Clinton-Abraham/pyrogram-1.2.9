# Pyrogram - Telegram MTProto API Client Library for Python
# Copyright (C) 2017-2018 Dan Tès <https://github.com/delivrance>
#
# This file is part of Pyrogram.
#
# Pyrogram is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pyrogram is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Pyrogram.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import logging
import platform
from datetime import datetime, timedelta
from hashlib import sha1
from io import BytesIO

import pyrogram
from pyrogram import __copyright__, __license__, __version__
from pyrogram.api import functions, types, core
from pyrogram.api.all import layer
from pyrogram.api.core import Object, MsgContainer, Long, FutureSalt, Int
from pyrogram.api.errors import Error, InternalServerError
from pyrogram.connection import Connection
from pyrogram.crypto import MTProto
from .internals import MsgId, MsgFactory, DataCenter

log = logging.getLogger(__name__)


class Result:
    def __init__(self):
        self.value = None
        self.event = asyncio.Event()


class Session:
    VERSION = __version__
    APP_VERSION = "Pyrogram \U0001f525 {}".format(VERSION)

    DEVICE_MODEL = "{} {}".format(
        platform.python_implementation(),
        platform.python_version()
    )

    SYSTEM_VERSION = "{} {}".format(
        platform.system(),
        platform.release()
    )

    WAIT_TIMEOUT = 15
    MAX_RETRIES = 5
    ACKS_THRESHOLD = 8
    PING_INTERVAL = 5

    notice_displayed = False

    BAD_MSG_DESCRIPTION = {
        16: "[16] msg_id too low, the client time has to be synchronized",
        17: "[17] msg_id too high, the client time has to be synchronized",
        18: "[18] incorrect two lower order msg_id bits, the server expects client message msg_id to be divisible by 4",
        19: "[19] container msg_id is the same as msg_id of a previously received message",
        20: "[20] message too old, it cannot be verified by the server",
        32: "[32] msg_seqno too low",
        33: "[33] msg_seqno too high",
        34: "[34] an even msg_seqno expected, but odd received",
        35: "[35] odd msg_seqno expected, but even received",
        48: "[48] incorrect server salt",
        64: "[64] invalid container"
    }

    def __init__(self,
                 dc_id: int,
                 test_mode: bool,
                 proxy: dict,
                 auth_key: bytes,
                 api_id: int,
                 is_cdn: bool = False,
                 client: pyrogram = None):
        if not Session.notice_displayed:
            print("Pyrogram v{}, {}".format(__version__, __copyright__))
            print("Licensed under the terms of the " + __license__, end="\n\n")
            Session.notice_displayed = True

        self.dc_id = dc_id
        self.test_mode = test_mode
        self.proxy = proxy
        self.api_id = api_id
        self.is_cdn = is_cdn
        self.client = client

        self.connection = None

        self.auth_key = auth_key
        self.auth_key_id = sha1(auth_key).digest()[-8:]

        self.session_id = Long(MsgId())
        self.msg_factory = MsgFactory()

        self.current_salt = None

        self.pending_acks = set()

        self.recv_queue = asyncio.Queue()
        self.results = {}

        self.ping_task = None
        self.ping_task_event = asyncio.Event()

        self.next_salt_task = None
        self.next_salt_task_event = asyncio.Event()

        self.net_worker_task = None
        self.recv_task = None

        self.is_connected = asyncio.Event()

    async def start(self):
        while True:
            self.connection = Connection(DataCenter(self.dc_id, self.test_mode), self.proxy)

            try:
                await self.connection.connect()

                self.net_worker_task = asyncio.ensure_future(self.net_worker())
                self.recv_task = asyncio.ensure_future(self.recv())

                self.current_salt = FutureSalt(0, 0, MTProto.INITIAL_SALT)
                self.current_salt = FutureSalt(0, 0, (await self._send(functions.Ping(0))).new_server_salt)
                self.current_salt = (await self._send(functions.GetFutureSalts(1))).salts[0]

                self.next_salt_task = asyncio.ensure_future(self.next_salt())

                if not self.is_cdn:
                    await self._send(
                        functions.InvokeWithLayer(
                            layer,
                            functions.InitConnection(
                                self.api_id,
                                self.DEVICE_MODEL,
                                self.SYSTEM_VERSION,
                                self.APP_VERSION,
                                "en", "", "en",
                                functions.help.GetConfig(),
                            )
                        )
                    )

                self.ping_task = asyncio.ensure_future(self.ping())

                log.info("Connection inited: Layer {}".format(layer))
            except (OSError, TimeoutError, Error):
                await self.stop()
            except Exception as e:
                await self.stop()
                raise e
            else:
                break

        self.is_connected.set()

        log.debug("Session started")

    async def stop(self):
        self.is_connected.clear()

        self.ping_task_event.set()
        self.next_salt_task_event.set()

        if self.ping_task is not None:
            await self.ping_task

        if self.next_salt_task is not None:
            await self.next_salt_task

        self.ping_task_event.clear()
        self.next_salt_task_event.clear()

        self.connection.close()

        await self.recv_task

        self.recv_queue.put_nowait(None)

        await self.net_worker_task

        for i in self.results.values():
            i.event.set()

        if self.client and callable(self.client.disconnect_handler):
            try:
                self.client.disconnect_handler(self.client)
            except Exception as e:
                log.error(e, exc_info=True)

        log.debug("Session stopped")

    async def restart(self):
        self.stop()
        self.start()

    async def net_worker(self):
        log.info("NetWorkerTask started")

        while True:
            packet = await self.recv_queue.get()

            if packet is None:
                break

            try:
                data = MTProto.unpack(
                    BytesIO(packet),
                    self.current_salt.salt,
                    self.session_id,
                    self.auth_key,
                    self.auth_key_id
                )

                messages = (
                    data.body.messages
                    if isinstance(data.body, MsgContainer)
                    else [data]
                )

                log.debug(data)

                for msg in messages:
                    if msg.seq_no % 2 != 0:
                        if msg.msg_id in self.pending_acks:
                            continue
                        else:
                            self.pending_acks.add(msg.msg_id)

                    if isinstance(msg.body, (types.MsgDetailedInfo, types.MsgNewDetailedInfo)):
                        self.pending_acks.add(msg.body.answer_msg_id)
                        continue

                    if isinstance(msg.body, types.NewSessionCreated):
                        continue

                    msg_id = None

                    if isinstance(msg.body, (types.BadMsgNotification, types.BadServerSalt)):
                        msg_id = msg.body.bad_msg_id
                    elif isinstance(msg.body, (core.FutureSalts, types.RpcResult)):
                        msg_id = msg.body.req_msg_id
                    elif isinstance(msg.body, types.Pong):
                        msg_id = msg.body.msg_id
                    else:
                        if self.client is not None:
                            self.client.updates_queue.put_nowait(msg.body)

                    if msg_id in self.results:
                        self.results[msg_id].value = getattr(msg.body, "result", msg.body)
                        self.results[msg_id].event.set()

                if len(self.pending_acks) >= self.ACKS_THRESHOLD:
                    log.info("Send {} acks".format(len(self.pending_acks)))

                    try:
                        await self._send(types.MsgsAck(list(self.pending_acks)), False)
                    except (OSError, TimeoutError):
                        pass
                    else:
                        self.pending_acks.clear()
            except Exception as e:
                log.error(e, exc_info=True)

        log.info("NetWorkerTask stopped")

    async def ping(self):
        log.info("PingTask started")

        while True:
            try:
                await asyncio.wait_for(self.ping_task_event.wait(), self.PING_INTERVAL)
            except asyncio.TimeoutError:
                pass
            else:
                break

            try:
                await self._send(functions.PingDelayDisconnect(
                    0, self.WAIT_TIMEOUT + 10
                ), False)
            except (OSError, TimeoutError, Error):
                pass

        log.info("PingTask stopped")

    async def next_salt(self):
        log.info("NextSaltTask started")

        while True:
            now = datetime.now()

            # Seconds to wait until middle-overlap, which is
            # 15 minutes before/after the current/next salt end/start time
            dt = (self.current_salt.valid_until - now).total_seconds() - 900

            log.debug("Current salt: {} | Next salt in {:.0f}m {:.0f}s ({})".format(
                self.current_salt.salt,
                dt // 60,
                dt % 60,
                now + timedelta(seconds=dt)
            ))

            try:
                await asyncio.wait_for(self.next_salt_task_event.wait(), dt)
            except asyncio.TimeoutError:
                pass
            else:
                break

            try:
                self.current_salt = (await self._send(functions.GetFutureSalts(1))).salts[0]
            except (OSError, TimeoutError, Error):
                self.connection.close()
                break

        log.info("NextSaltTask stopped")

    async def recv(self):
        log.info("RecvTask started")

        while True:
            packet = await self.connection.recv()

            if packet is None or len(packet) == 4:
                if packet:
                    log.warning("Server sent \"{}\"".format(Int.read(BytesIO(packet))))

                if self.is_connected.is_set():
                    asyncio.ensure_future(self.restart())

                break

            self.recv_queue.put_nowait(packet)

        log.info("RecvTask stopped")

    async def _send(self, data: Object, wait_response: bool = True):
        message = self.msg_factory(data)
        msg_id = message.msg_id

        if wait_response:
            self.results[msg_id] = Result()

        payload = MTProto.pack(
            message,
            self.current_salt.salt,
            self.session_id,
            self.auth_key,
            self.auth_key_id
        )

        try:
            await self.connection.send(payload)
        except OSError as e:
            self.results.pop(msg_id, None)
            raise e

        if wait_response:
            try:
                await asyncio.wait_for(self.results[msg_id].event.wait(), self.WAIT_TIMEOUT)
            except asyncio.TimeoutError:
                pass

            result = self.results.pop(msg_id).value

            if result is None:
                raise TimeoutError
            elif isinstance(result, types.RpcError):
                Error.raise_it(result, type(data))
            elif isinstance(result, types.BadMsgNotification):
                raise Exception(self.BAD_MSG_DESCRIPTION.get(
                    result.error_code,
                    "Error code {}".format(result.error_code)
                ))
            else:
                return result

    async def send(self, data: Object, retries: int = MAX_RETRIES):
        try:
            await asyncio.wait_for(self.is_connected.wait(), self.WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            pass

        try:
            return await self._send(data)
        except (OSError, TimeoutError, InternalServerError) as e:
            if retries == 0:
                raise e from None

            (log.warning if retries < 3 else log.info)(
                "{}: {} Retrying {}".format(
                    Session.MAX_RETRIES - retries,
                    datetime.now(), type(data)))

            await asyncio.sleep(0.5)
            return await self.send(data, retries - 1)

# class Result:
#     def __init__(self):
#         self.value = None
#         self.event = Event()
#
#
# class Session:
#     VERSION = __version__
#     APP_VERSION = "Pyrogram \U0001f525 {}".format(VERSION)
#
#     DEVICE_MODEL = "{} {}".format(
#         platform.python_implementation(),
#         platform.python_version()
#     )
#
#     SYSTEM_VERSION = "{} {}".format(
#         platform.system(),
#         platform.release()
#     )
#
#     INITIAL_SALT = 0x616e67656c696361
#     NET_WORKERS = 1
#     WAIT_TIMEOUT = 15
#     MAX_RETRIES = 5
#     ACKS_THRESHOLD = 8
#     PING_INTERVAL = 5
#
#     notice_displayed = False
#
#     BAD_MSG_DESCRIPTION = {
#         16: "[16] msg_id too low, the client time has to be synchronized",
#         17: "[17] msg_id too high, the client time has to be synchronized",
#         18: "[18] incorrect two lower order msg_id bits, the server expects client message msg_id to be divisible by 4",
#         19: "[19] container msg_id is the same as msg_id of a previously received message",
#         20: "[20] message too old, it cannot be verified by the server",
#         32: "[32] msg_seqno too low",
#         33: "[33] msg_seqno too high",
#         34: "[34] an even msg_seqno expected, but odd received",
#         35: "[35] odd msg_seqno expected, but even received",
#         48: "[48] incorrect server salt",
#         64: "[64] invalid container"
#     }
#
#     def __init__(self,
#                  dc_id: int,
#                  test_mode: bool,
#                  proxy: dict,
#                  auth_key: bytes,
#                  api_id: int,
#                  is_cdn: bool = False,
#                  client: pyrogram = None):
#         if not Session.notice_displayed:
#             print("Pyrogram v{}, {}".format(__version__, __copyright__))
#             print("Licensed under the terms of the " + __license__, end="\n\n")
#             Session.notice_displayed = True
#
#         self.dc_id = dc_id
#         self.test_mode = test_mode
#         self.proxy = proxy
#         self.api_id = api_id
#         self.is_cdn = is_cdn
#         self.client = client
#
#         self.connection = None
#
#         self.auth_key = auth_key
#         self.auth_key_id = sha1(auth_key).digest()[-8:]
#
#         self.session_id = Long(MsgId())
#         self.msg_factory = MsgFactory()
#
#         self.current_salt = None
#
#         self.pending_acks = set()
#
#         self.recv_queue = Queue()
#         self.results = {}
#
#         self.ping_thread = None
#         self.ping_thread_event = Event()
#
#         self.next_salt_thread = None
#         self.next_salt_thread_event = Event()
#
#         self.net_worker_list = []
#
#         self.is_connected = Event()
#
#     def start(self):
#         while True:
#             self.connection = Connection(DataCenter(self.dc_id, self.test_mode), self.proxy)
#
#             try:
#                 self.connection.connect()
#
#                 for i in range(self.NET_WORKERS):
#                     self.net_worker_list.append(
#                         Thread(
#                             target=self.net_worker,
#                             name="NetWorker#{}".format(i + 1)
#                         )
#                     )
#
#                     self.net_worker_list[-1].start()
#
#                 Thread(target=self.recv, name="RecvThread").start()
#
#                 self.current_salt = FutureSalt(0, 0, self.INITIAL_SALT)
#                 self.current_salt = FutureSalt(0, 0, self._send(functions.Ping(0)).new_server_salt)
#                 self.current_salt = self._send(functions.GetFutureSalts(1)).salts[0]
#
#                 self.next_salt_thread = Thread(target=self.next_salt, name="NextSaltThread")
#                 self.next_salt_thread.start()
#
#                 if not self.is_cdn:
#                     self._send(
#                         functions.InvokeWithLayer(
#                             layer,
#                             functions.InitConnection(
#                                 self.api_id,
#                                 self.DEVICE_MODEL,
#                                 self.SYSTEM_VERSION,
#                                 self.APP_VERSION,
#                                 "en", "", "en",
#                                 functions.help.GetConfig(),
#                             )
#                         )
#                     )
#
#                 self.ping_thread = Thread(target=self.ping, name="PingThread")
#                 self.ping_thread.start()
#
#                 log.info("Connection inited: Layer {}".format(layer))
#             except (OSError, TimeoutError, Error):
#                 self.stop()
#             except Exception as e:
#                 self.stop()
#                 raise e
#             else:
#                 break
#
#         self.is_connected.set()
#
#         log.debug("Session started")
#
#     def stop(self):
#         self.is_connected.clear()
#
#         self.ping_thread_event.set()
#         self.next_salt_thread_event.set()
#
#         if self.ping_thread is not None:
#             self.ping_thread.join()
#
#         if self.next_salt_thread is not None:
#             self.next_salt_thread.join()
#
#         self.ping_thread_event.clear()
#         self.next_salt_thread_event.clear()
#
#         self.connection.close()
#
#         for i in range(self.NET_WORKERS):
#             self.recv_queue.put(None)
#
#         for i in self.net_worker_list:
#             i.join()
#
#         self.net_worker_list.clear()
#
#         for i in self.results.values():
#             i.event.set()
#
#         if self.client and callable(self.client.disconnect_handler):
#             try:
#                 self.client.disconnect_handler(self.client)
#             except Exception as e:
#                 log.error(e, exc_info=True)
#
#         log.debug("Session stopped")
#
#     def restart(self):
#         self.stop()
#         self.start()
#
#     def pack(self, message: Message):
#         data = Long(self.current_salt.salt) + self.session_id + message.write()
#         padding = urandom(-(len(data) + 12) % 16 + 12)
#
#         # 88 = 88 + 0 (outgoing message)
#         msg_key_large = sha256(self.auth_key[88: 88 + 32] + data + padding).digest()
#         msg_key = msg_key_large[8:24]
#         aes_key, aes_iv = KDF(self.auth_key, msg_key, True)
#
#         return self.auth_key_id + msg_key + AES.ige256_encrypt(data + padding, aes_key, aes_iv)
#
#     def unpack(self, b: BytesIO) -> Message:
#         assert b.read(8) == self.auth_key_id, b.getvalue()
#
#         msg_key = b.read(16)
#         aes_key, aes_iv = KDF(self.auth_key, msg_key, False)
#         data = BytesIO(AES.ige256_decrypt(b.read(), aes_key, aes_iv))
#         data.read(8)
#
#         # https://core.telegram.org/mtproto/security_guidelines#checking-session-id
#         assert data.read(8) == self.session_id
#
#         message = Message.read(data)
#
#         # https://core.telegram.org/mtproto/security_guidelines#checking-sha256-hash-value-of-msg-key
#         # https://core.telegram.org/mtproto/security_guidelines#checking-message-length
#         # 96 = 88 + 8 (incoming message)
#         assert msg_key == sha256(self.auth_key[96:96 + 32] + data.getvalue()).digest()[8:24]
#
#         # https://core.telegram.org/mtproto/security_guidelines#checking-msg-id
#         # TODO: check for lower msg_ids
#         assert message.msg_id % 2 != 0
#
#         return message
#
#     def net_worker(self):
#         name = threading.current_thread().name
#         log.debug("{} started".format(name))
#
#         while True:
#             packet = self.recv_queue.get()
#
#             if packet is None:
#                 break
#
#             try:
#                 data = self.unpack(BytesIO(packet))
#
#                 messages = (
#                     data.body.messages
#                     if isinstance(data.body, MsgContainer)
#                     else [data]
#                 )
#
#                 log.debug(data)
#
#                 for msg in messages:
#                     if msg.seq_no % 2 != 0:
#                         if msg.msg_id in self.pending_acks:
#                             continue
#                         else:
#                             self.pending_acks.add(msg.msg_id)
#
#                     if isinstance(msg.body, (types.MsgDetailedInfo, types.MsgNewDetailedInfo)):
#                         self.pending_acks.add(msg.body.answer_msg_id)
#                         continue
#
#                     if isinstance(msg.body, types.NewSessionCreated):
#                         continue
#
#                     msg_id = None
#
#                     if isinstance(msg.body, (types.BadMsgNotification, types.BadServerSalt)):
#                         msg_id = msg.body.bad_msg_id
#                     elif isinstance(msg.body, (core.FutureSalts, types.RpcResult)):
#                         msg_id = msg.body.req_msg_id
#                     elif isinstance(msg.body, types.Pong):
#                         msg_id = msg.body.msg_id
#                     else:
#                         if self.client is not None:
#                             self.client.updates_queue.put(msg.body)
#
#                     if msg_id in self.results:
#                         self.results[msg_id].value = getattr(msg.body, "result", msg.body)
#                         self.results[msg_id].event.set()
#
#                 if len(self.pending_acks) >= self.ACKS_THRESHOLD:
#                     log.info("Send {} acks".format(len(self.pending_acks)))
#
#                     try:
#                         self._send(types.MsgsAck(list(self.pending_acks)), False)
#                     except (OSError, TimeoutError):
#                         pass
#                     else:
#                         self.pending_acks.clear()
#             except Exception as e:
#                 log.error(e, exc_info=True)
#
#         log.debug("{} stopped".format(name))
#
#     def ping(self):
#         log.debug("PingThread started")
#
#         while True:
#             self.ping_thread_event.wait(self.PING_INTERVAL)
#
#             if self.ping_thread_event.is_set():
#                 break
#
#             try:
#                 self._send(functions.PingDelayDisconnect(
#                     0, self.WAIT_TIMEOUT + 10
#                 ), False)
#             except (OSError, TimeoutError, Error):
#                 pass
#
#         log.debug("PingThread stopped")
#
#     def next_salt(self):
#         log.debug("NextSaltThread started")
#
#         while True:
#             now = datetime.now()
#
#             # Seconds to wait until middle-overlap, which is
#             # 15 minutes before/after the current/next salt end/start time
#             dt = (self.current_salt.valid_until - now).total_seconds() - 900
#
#             log.debug("Current salt: {} | Next salt in {:.0f}m {:.0f}s ({})".format(
#                 self.current_salt.salt,
#                 dt // 60,
#                 dt % 60,
#                 now + timedelta(seconds=dt)
#             ))
#
#             self.next_salt_thread_event.wait(dt)
#
#             if self.next_salt_thread_event.is_set():
#                 break
#
#             try:
#                 self.current_salt = self._send(functions.GetFutureSalts(1)).salts[0]
#             except (OSError, TimeoutError, Error):
#                 self.connection.close()
#                 break
#
#         log.debug("NextSaltThread stopped")
#
#     def recv(self):
#         log.debug("RecvThread started")
#
#         while True:
#             packet = self.connection.recv()
#
#             if packet is None or len(packet) == 4:
#                 if packet:
#                     log.warning("Server sent \"{}\"".format(Int.read(BytesIO(packet))))
#
#                 if self.is_connected.is_set():
#                     Thread(target=self.restart, name="RestartThread").start()
#                 break
#
#             self.recv_queue.put(packet)
#
#         log.debug("RecvThread stopped")
#
#     def _send(self, data: Object, wait_response: bool = True):
#         message = self.msg_factory(data)
#         msg_id = message.msg_id
#
#         if wait_response:
#             self.results[msg_id] = Result()
#
#         payload = self.pack(message)
#
#         try:
#             self.connection.send(payload)
#         except OSError as e:
#             self.results.pop(msg_id, None)
#             raise e
#
#         if wait_response:
#             self.results[msg_id].event.wait(self.WAIT_TIMEOUT)
#             result = self.results.pop(msg_id).value
#
#             if result is None:
#                 raise TimeoutError
#             elif isinstance(result, types.RpcError):
#                 Error.raise_it(result, type(data))
#             elif isinstance(result, types.BadMsgNotification):
#                 raise Exception(self.BAD_MSG_DESCRIPTION.get(
#                     result.error_code,
#                     "Error code {}".format(result.error_code)
#                 ))
#             else:
#                 return result
#
#     def send(self, data: Object, retries: int = MAX_RETRIES):
#         self.is_connected.wait(self.WAIT_TIMEOUT)
#
#         try:
#             return self._send(data)
#         except (OSError, TimeoutError, InternalServerError) as e:
#             if retries == 0:
#                 raise e from None
#
#             (log.warning if retries < 3 else log.info)(
#                 "{}: {} Retrying {}".format(
#                     Session.MAX_RETRIES - retries,
#                     datetime.now(), type(data)))
#
#             time.sleep(0.5)
#             return self.send(data, retries - 1)
