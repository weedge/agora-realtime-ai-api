# -*- coding: utf-8 -*-
__copyright__ = "Copyright (c) 2014-2024 Agora.io, Inc."

import logging

from ..logger import setup_logger
from .RtcTokenBuilder2 import RtcTokenBuilder

# Set up the logger with color and timestamp support
logger = setup_logger(name=__name__, log_level=logging.INFO)

class RealtimekitTokenBuilder(RtcTokenBuilder):
    @staticmethod
    def build_token(app_id:str, app_certificate:str, channel_name:str, uid:0, expiration_in_seconds:int = 3600):
        """
        Build the RTC token.
        :return: The RTC token.
        """
        if not app_id :
            raise ValueError("Need to set environment variable AGORA_APP_ID")
        logger.info("App Id: %s" % app_id)
        if not app_certificate :
            return ""
        token_expiration_in_seconds = expiration_in_seconds
        join_channel_privilege_expiration_in_seconds = expiration_in_seconds
        pub_audio_privilege_expiration_in_seconds = expiration_in_seconds
        pub_video_privilege_expiration_in_seconds = expiration_in_seconds
        pub_data_stream_privilege_expiration_in_seconds = expiration_in_seconds

        token = RtcTokenBuilder.build_token_with_uid_and_privilege(
            app_id, app_certificate, channel_name, uid, token_expiration_in_seconds,
            join_channel_privilege_expiration_in_seconds, pub_audio_privilege_expiration_in_seconds, pub_video_privilege_expiration_in_seconds, pub_data_stream_privilege_expiration_in_seconds)
        logger.info("Token with int uid and privilege: {}".format(token))

        return token