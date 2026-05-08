import logging

from common.logger_factory import LoggerFactory

"""
_logging.py
websocket - WebSocket client library for Python

Copyright 2022 engn33r

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
try:
    from logging import NullHandler
except ImportError:
    class NullHandler(logging.Handler):
        def emit(self, record):
            pass


_traceEnabled = False

__all__ = ["dump", "error", "warning", "debug", "trace"]


def dump(title, message):
    LoggerFactory.get_logger().debug("--- " + title + " ---")
    LoggerFactory.get_logger().debug(message)
    LoggerFactory.get_logger().debug("-----------------------")


def error(msg):
    LoggerFactory.get_logger().error(msg)


def warning(msg):
    LoggerFactory.get_logger().warning(msg)


def debug(msg):
    LoggerFactory.get_logger().debug(msg)


def info(msg):
    LoggerFactory.get_logger().info(msg)


def trace(msg):
    LoggerFactory.get_logger().debug(msg)


