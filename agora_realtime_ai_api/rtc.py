import asyncio
import base64
import json
import logging
import math
import os
from typing import Any, AsyncIterator

from agora.rtc.agora_base import (
    AudioScenarioType,
    ChannelProfileType,
    ClientRoleType,
)
from agora.rtc.agora_service import (
    AgoraService,
    AgoraServiceConfig,
    RTCConnConfig,
)
from agora.rtc.audio_frame_observer import AudioFrame, IAudioFrameObserver
from agora.rtc.audio_pcm_data_sender import PcmAudioFrame
from agora.rtc.local_user import LocalUser
from agora.rtc.local_user_observer import IRTCLocalUserObserver
from agora.rtc.rtc_connection import RTCConnection, RTCConnInfo
from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
from pyee.asyncio import AsyncIOEventEmitter

from .logger import setup_logger
from .token_builder.realtimekit_token_builder import RealtimekitTokenBuilder

# Set up the logger with color and timestamp support
logger = setup_logger(name=__name__, log_level=logging.INFO)


class RtcOptions:
    def __init__(
        self,
        *,
        channel_name: str = None,
        uid: int = 0,
        sample_rate: int = 24000,
        channels: int = 1,
        enable_pcm_dump: bool = False,
    ):
        self.channel_name = channel_name
        self.uid = uid
        self.sample_rate = sample_rate
        self.channels = channels
        self.enable_pcm_dump = enable_pcm_dump

    def build_token(self, appid: str, appcert: str) -> str:
        return RealtimekitTokenBuilder.build_token(
            appid, appcert, self.channel_name, self.uid
        )


class AudioStream:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()

    def __aiter__(self) -> AsyncIterator[PcmAudioFrame]:
        return self

    async def __anext__(self) -> PcmAudioFrame:
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration

        return item


class ChannelEventObserver(
    IRTCConnectionObserver, IRTCLocalUserObserver, IAudioFrameObserver
):
    def __init__(self, event_emitter: AsyncIOEventEmitter, options: RtcOptions) -> None:
        self.loop = asyncio.get_event_loop()
        self.emitter = event_emitter
        self.audio_streams = dict[str, AudioStream]()
        self.options = options

    def emit_event(self, event_name: str, *args):
        """Helper function to emit events."""
        self.loop.call_soon_threadsafe(self.emitter.emit, event_name, *args)

    def on_connected(
        self, agora_rtc_conn: RTCConnection, conn_info: RTCConnInfo, reason
    ):
        logger.info(f"Connected to RTC: {agora_rtc_conn} {conn_info} {reason}")
        self.emit_event("connection_state_changed", agora_rtc_conn, conn_info, reason)

    def on_disconnected(
        self, agora_rtc_conn: RTCConnection, conn_info: RTCConnInfo, reason
    ):
        logger.info(f"Disconnected from RTC: {agora_rtc_conn} {conn_info} {reason}")
        self.emit_event("connection_state_changed", agora_rtc_conn, conn_info, reason)

    def on_connecting(
        self, agora_rtc_conn: RTCConnection, conn_info: RTCConnInfo, reason
    ):
        logger.info(f"Connecting to RTC: {agora_rtc_conn} {conn_info} {reason}")
        self.emit_event("connection_state_changed", agora_rtc_conn, conn_info, reason)

    def on_connection_failure(self, agora_rtc_conn, conn_info, reason):
        logger.error(f"Connection failure: {agora_rtc_conn} {conn_info} {reason}")
        self.emit_event("connection_state_changed", agora_rtc_conn, conn_info, reason)

    def on_user_joined(self, agora_rtc_conn: RTCConnection, user_id):
        logger.info(f"User joined: {agora_rtc_conn} {user_id}")
        self.emit_event("user_joined", agora_rtc_conn, user_id)

    def on_user_left(self, agora_rtc_conn: RTCConnection, user_id, reason):
        logger.info(f"User left: {agora_rtc_conn} {user_id} {reason}")
        self.emit_event("user_left", agora_rtc_conn, user_id, reason)

    def on_stream_message(
        self, agora_local_user: LocalUser, user_id, stream_id, data, length
    ):
        # logger.info(f"Stream message", agora_local_user, user_id, stream_id, length)
        self.emit_event("stream_message", agora_local_user, user_id, stream_id, data, length)

    def on_stream_message_error(self, agora_rtc_conn, user_id_str, stream_id, code, missed, cached):
        logger.warn(f"Stream message error: {user_id_str} {stream_id} {code} {missed} {cached}")

    def on_audio_subscribe_state_changed(
        self,
        agora_local_user,
        channel,
        user_id,
        old_state,
        new_state,
        elapse_since_last_state,
    ):
        logger.info(
            f"Audio subscribe state changed: {user_id} {new_state} {elapse_since_last_state}"
        )
        self.emit_event(
            "audio_subscribe_state_changed",
            agora_local_user,
            channel,
            user_id,
            old_state,
            new_state,
            elapse_since_last_state,
        )

    def on_playback_audio_frame_before_mixing(
        self, agora_local_user: LocalUser, channelId, uid, frame: AudioFrame
    ):
        audio_frame = PcmAudioFrame()
        audio_frame.samples_per_channel = frame.samples_per_channel
        audio_frame.bytes_per_sample = frame.bytes_per_sample
        audio_frame.number_of_channels = frame.channels
        audio_frame.sample_rate = self.options.sample_rate
        audio_frame.data = frame.buffer

        # print(
        #     "on_playback_audio_frame_before_mixing",
        #     audio_frame.samples_per_channel,
        #     audio_frame.bytes_per_sample,
        #     audio_frame.number_of_channels,
        #     audio_frame.sample_rate,
        #     len(audio_frame.data),
        # )
        self.loop.call_soon_threadsafe(
            self.audio_streams[uid].queue.put_nowait, audio_frame
        )
        return 0


class Channel:
    def __init__(self, rtc: "RtcEngine", options: RtcOptions) -> None:
        self.loop = asyncio.get_event_loop()
        self.stream_message_queue = asyncio.Queue()

        # Create the event emitter
        self.emitter = AsyncIOEventEmitter(self.loop)
        self.connection_state = 0
        self.options = options
        self.remote_users = dict[str, Any]()
        self.rtc = rtc
        self.chat = Chat(self)
        self.channelId = options.channel_name
        self.uid = options.uid
        self.enable_pcm_dump = options.enable_pcm_dump
        self.token = options.build_token(rtc.appid, rtc.appcert) if rtc.appcert else ""
        conn_config = RTCConnConfig(
            client_role_type=ClientRoleType.CLIENT_ROLE_BROADCASTER,
            channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
        )
        self.connection = self.rtc.agora_service.create_rtc_connection(conn_config)

        self.channel_event_observer = ChannelEventObserver(
            self.emitter,
            options=options,
        )
        self.connection.register_observer(self.channel_event_observer)

        self.local_user = self.connection.get_local_user()
        self.local_user.set_playback_audio_frame_before_mixing_parameters(
            options.channels, options.sample_rate
        )
        self.local_user.register_local_user_observer(self.channel_event_observer)
        self.local_user.register_audio_frame_observer(self.channel_event_observer)
        # self.local_user.subscribe_all_audio()

        self.media_node_factory = self.rtc.agora_service.create_media_node_factory()
        self.audio_pcm_data_sender = (
            self.media_node_factory.create_audio_pcm_data_sender()
        )
        self.audio_track = self.rtc.agora_service.create_custom_audio_track_pcm(
            self.audio_pcm_data_sender
        )
        self.audio_track.set_enabled(1)
        self.local_user.publish_audio(self.audio_track)

        self.stream_id = self.connection.create_data_stream(False, False)
        self.received_chunks = {}
        self.waiting_message = None
        self.msg_id = ""
        self.msg_index = ""

        self.on(
            "user_joined",
            lambda agora_rtc_conn, user_id: self.remote_users.update({user_id: True}),
        )

        def handle_user_left(agora_rtc_conn, user_id, reason):
            if user_id in self.remote_users:
                self.remote_users.pop(user_id, None)
            if user_id in self.channel_event_observer.audio_streams:
                audio_stream = self.channel_event_observer.audio_streams.pop(user_id, None)
                audio_stream.queue.put_nowait(None)

        self.on(
            "user_left",
            handle_user_left,
        )

        def handle_audio_subscribe_state_changed(
            agora_local_user,
            channel,
            user_id,
            old_state,
            new_state,
            elapse_since_last_state,
        ):
            if new_state == 3:  # Successfully subscribed
                if user_id not in self.channel_event_observer.audio_streams:
                    self.channel_event_observer.audio_streams.update(
                        {user_id: AudioStream()}
                    )

        self.on("audio_subscribe_state_changed", handle_audio_subscribe_state_changed)
        self.on(
            "connection_state_changed",
            lambda agora_rtc_conn, conn_info, reason: setattr(
                self, "connection_state", conn_info.state
            ),
        )

        def log_exception(t: asyncio.Task[Any]) -> None:
            if not t.cancelled() and t.exception():
                logger.error(
                    "unhandled exception",
                    exc_info=t.exception(),
                )

        asyncio.create_task(self._process_stream_message()).add_done_callback(log_exception)

    async def connect(self) -> None:
        """
        Connects to a channel.

        Parameters:
            channelId: The channel ID.
            uid: The user ID.

        Returns:
            Channel: The connected channel.
        """
        if self.connection_state == 3:
            return

        future = asyncio.Future()

        def callback(agora_rtc_conn: RTCConnection, conn_info: RTCConnInfo, reason):
            logger.info(f"Connection state changed: {conn_info.state}")
            if conn_info.state == 3:  # Connection successful
                future.set_result(None)
            elif conn_info.state == 5:  # Connection failed
                future.set_exception(
                    Exception(f"Connection failed with state: {conn_info.state} reason:{reason}")
                )

        self.on("connection_state_changed", callback)
        logger.info(
            f"Connecting to channel {self.channelId} with token {self.token} uid {self.uid}")
        self.connection.connect(self.token, self.channelId, f"{self.uid}")

        if self.enable_pcm_dump:
            agora_parameter = self.connection.get_agora_parameter()
            agora_parameter.set_parameters("{\"che.audio.frame_dump\":{\"location\":\"all\",\"action\":\"start\",\"max_size_bytes\":\"120000000\",\"uuid\":\"123456789\",\"duration\":\"1200000\"}}")

        try:
            await future
        except Exception as e:
            raise Exception(
                f"Failed to connect to channel {self.channelId}: {str(e)}"
            ) from e
        finally:
            self.off("connection_state_changed", callback)

    async def disconnect(self) -> None:
        """
        Disconnects the channel.
        """
        if self.connection_state == 1:
            return

        disconnected_future = asyncio.Future[None]()

        def callback(agora_rtc_conn: RTCConnection, conn_info: RTCConnInfo, reason):
            self.off("connection_state_changed", callback)
            if conn_info.state == 1:
                disconnected_future.set_result(None)

        self.on("connection_state_changed", callback)
        self.connection.disconnect()
        await disconnected_future

    def get_audio_frames(self, uid: str) -> AudioStream | None:
        """
        Returns the audio frames from the channel.

        Returns:
            AudioStream: The audio stream.
        """
        return None if self.channel_event_observer.audio_streams.get(uid) is None \
            else self.channel_event_observer.audio_streams.get(uid)

    async def push_audio_frame(self, frame: bytes) -> None:
        """
        Pushes an audio frame to the channel.

        Parameters:
            frame: The audio frame to push.
        """
        audio_frame = PcmAudioFrame()
        audio_frame.data = bytearray(frame)
        audio_frame.timestamp = 0
        audio_frame.bytes_per_sample = 2
        audio_frame.number_of_channels = self.options.channels
        audio_frame.sample_rate = self.options.sample_rate
        audio_frame.samples_per_channel = int(
            len(frame) / audio_frame.bytes_per_sample / audio_frame.number_of_channels
        )

        ret = self.audio_pcm_data_sender.send_audio_pcm_data(audio_frame)
        logger.debug(f"Pushed audio frame: {ret}, audio frame length: {len(frame)}")
        if ret < 0:
            raise Exception(f"Failed to send audio frame: {ret}, audio frame length: {len(frame)}")

    async def clear_sender_audio_buffer(self) -> None:
        """
        Clears the audio buffer which is used to send.
        """
        self.audio_track.clear_sender_buffer()

    async def subscribe_audio(self, uid: str) -> None:
        """
        Subscribes to the audio of a user.

        Parameters:
            uid: The user ID to subscribe to.
        """
        future = asyncio.Future()

        def callback(
            agora_local_user,
            channel,
            user_id,
            old_state,
            new_state,
            elapse_since_last_state,
        ):
            if new_state == 3:  # Successfully subscribed
                future.set_result(None)
            # elif new_state == 1:  # Subscription failed
            #     future.set_exception(
            #         Exception(
            #             f"Failed to subscribe {user_id} audio: state changed from {old_state} to {new_state}"
            #         )
            #     )

        self.on("audio_subscribe_state_changed", callback)
        self.local_user.subscribe_audio(uid)

        try:
            await future
        except Exception as e:
            raise Exception(
                f"Audio subscription failed for user {uid}: {str(e)}"
            ) from e
        finally:
            self.off("audio_subscribe_state_changed", callback)

    async def unsubscribe_audio(self, uid: str) -> None:
        """
        Unsubscribes from the audio of a user.

        Parameters:
            uid: The user ID to unsubscribe from.
        """
        future = asyncio.Future()

        def callback(
            agora_local_user,
            channel,
            user_id,
            old_state,
            new_state,
            elapse_since_last_state,
        ):
            if new_state == 3:  # Successfully unsubscribed
                future.set_result(None)
            else:  # Failed to unsubscribe
                future.set_exception(
                    Exception(
                        f"Failed to unsubscribe {user_id} audio: state changed from {old_state} to {new_state}"
                    )
                )

        self.on("audio_subscribe_state_changed", callback)
        self.local_user.unsubscribe_audio(uid)

        try:
            await future
        except Exception as e:
            raise Exception(
                f"Audio unsubscription failed for user {uid}: {str(e)}"
            ) from e
        finally:
            self.off("audio_subscribe_state_changed", callback)

    async def _process_stream_message(self) -> None:
        """
        Processes stream messages.
        """
        while True:
            item = await self.stream_message_queue.get()
            ret = self.connection.send_stream_message(self.stream_id, item)
            if ret < 0:
                logger.error(f"Failed to send stream message: {ret}")
            self.stream_message_queue.task_done()
            # wait to avoid too frequent message sending
            await asyncio.sleep(0.04)

    async def send_stream_message(self, data: str) -> None:
        """
        Sends a stream message to the channel.

        Parameters:
            data: The data to send.
            msg_id: The message ID.
        """
        await self.stream_message_queue.put(data)

    def on(self, event_name: str, callback):
        """
        Allows external components to subscribe to events.

        Parameters:
            event_name: The name of the event to subscribe to.
            callback: The callback to call when the event is emitted.

        """
        self.emitter.on(event_name, callback)

    def once(self, event_name: str, callback):
        """
        Allows external components to subscribe to events once.

        Parameters:
            event_name: The name of the event to subscribe to.
            callback: The callback to call when the event is emitted.
        """
        self.emitter.once(event_name, callback)

    def off(self, event_name: str, callback):
        """
        Allows external components to unsubscribe from events.

        Parameters:
            event_name: The name of the event to unsubscribe from.
            callback: The callback to remove from the event.
        """
        self.emitter.remove_listener(event_name, callback)


class ChatMessage:
    def __init__(self, message: str, msg_id: str) -> None:
        self.message = message
        self.msg_id = msg_id


# Constants
MAX_CHUNK_SIZE_BYTES = 1024  # 1KB limit for the entire chunk after UTF-8 conversion


class Chat:
    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        self.loop = self.channel.loop
        self.queue = asyncio.Queue()

        def log_exception(t: asyncio.Task[Any]) -> None:
            if not t.cancelled() and t.exception():
                logger.error(
                    "unhandled exception",
                    exc_info=t.exception(),
                )

        asyncio.create_task(self._process_message()).add_done_callback(log_exception)

    async def send_message(self, item: ChatMessage) -> None:
        """
        Sends a message to the channel.

        Parameters:
            item: The message to send.
        """
        await self.queue.put(item)
        # await self.queue.put_nowait(item)

    def _text_to_base64_chunks(self, text: str, msg_id: str) -> list:
        # Ensure msg_id does not exceed 50 characters
        if len(msg_id) > 32:
            raise ValueError("msg_id cannot exceed 32 characters.")

        # Convert text to bytearray
        byte_array = bytearray(text, 'utf-8')

        # Encode the bytearray into base64
        base64_encoded = base64.b64encode(byte_array).decode('utf-8')

        # Initialize list to hold the final chunks
        chunks = []

        # We'll split the base64 string dynamically based on the final byte size
        part_index = 0
        total_parts = None  # We'll calculate total parts once we know how many chunks we create

        # Process the base64-encoded content in chunks
        current_position = 0
        total_length = len(base64_encoded)

        while current_position < total_length:
            part_index += 1

            # Start guessing the chunk size by limiting the base64 content part
            estimated_chunk_size = MAX_CHUNK_SIZE_BYTES  # We'll reduce this dynamically
            content_chunk = ""
            count = 0
            while True:
                # Create the content part of the chunk
                content_chunk = base64_encoded[current_position:current_position + estimated_chunk_size]

                # Format the chunk
                formatted_chunk = f"{msg_id}|{part_index}|{total_parts if total_parts else '???'}|{content_chunk}"

                # Check if the byte length of the formatted chunk exceeds the max allowed size
                if len(bytearray(formatted_chunk, 'utf-8')) <= MAX_CHUNK_SIZE_BYTES:
                    break
                else:
                    # Reduce the estimated chunk size if the formatted chunk is too large
                    estimated_chunk_size -= 100  # Reduce content size gradually
                    count += 1

            logger.debug(f"chunk estimate guess: {count}")

            # Add the current chunk to the list
            chunks.append(formatted_chunk)
            current_position += estimated_chunk_size  # Move to the next part of the content

        # Now that we know the total number of parts, update the chunks with correct total_parts
        total_parts = len(chunks)
        updated_chunks = [
            chunk.replace("???", str(total_parts)) for chunk in chunks
        ]

        return updated_chunks

    async def _process_message(self) -> None:
        """
        Processes messages in the queue.
        """

        while True:
            item: ChatMessage = await self.queue.get()
            chunks = self._text_to_base64_chunks(item.message, item.msg_id)
            for chunk in chunks:
                await self.channel.send_stream_message(chunk)
            self.queue.task_done()


class RtcEngine:
    def __init__(self, appid: str, appcert: str):
        self.appid = appid
        self.appcert = appcert

        if not appid:
            raise Exception("App ID is required)")

        config = AgoraServiceConfig()
        config.audio_scenario = AudioScenarioType.AUDIO_SCENARIO_CHORUS
        config.appid = appid
        config.log_path = os.path.join(
            os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.join(os.path.abspath(__file__)))
                )
            ),
            "agorasdk.log",
        )
        self.agora_service = AgoraService()
        self.agora_service.initialize(config)

    def create_channel(self, options: RtcOptions) -> Channel:
        """
        Creates a channel.

        Parameters:
            channelId: The channel ID.
            uid: The user ID.

        Returns:
            Channel: The created channel.
        """
        return Channel(self, options)

    def destroy(self) -> None:
        """
        Destroys the RTC engine.
        """
        self.agora_service.release()
