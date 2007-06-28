# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2007 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU Lesser General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU Lesser General Public License for more details.
##
## You should have received a copy of the GNU Lesser General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
## Author(s): Johan Dahlin    <jdahlin@async.com.br>
##
##

import os
# pyserial bug
import fcntl
fcntl.O_NONBLOCK = os.O_NONBLOCK

import gobject
from kiwi.utils import gsignal
from stoqdrivers.devices.serialbase import SerialPort

class ECFAsyncPrinterStatus(gobject.GObject):
    """
    @ivar printer:
    """
    gsignal('reply', str)
    gsignal('timeout')
    def __init__(self, device_name, printer_class=None, printer=None, delay=5):
        """
        @param device_name:
        @param printer_class:
        @param delay:
        """
        if not printer and not printer_class:
            raise TypeError

        gobject.GObject.__init__(self)
        self._reply = ''
        self._device_name = device_name
        self._delay = delay

        if printer_class:
            port = self._create_port()
            printer = printer_class(port)
        else:
            port = printer.get_port()
        self.printer = printer

        self._add_timeout()
        gobject.io_add_watch(port, gobject.IO_OUT, self._fd_watch_out)
        gobject.io_add_watch(port, gobject.IO_IN, self._fd_watch_in)

    def _create_port(self):
        port = SerialPort(device=self._device_name)
        port.nonblocking()
        return port

    def _remove_timeout(self):
        if self._timeout_id != -1:
            gobject.source_remove(self._timeout_id)
            self._timeout_id = -1

    def _add_timeout(self):
        self._timeout_id = gobject.timeout_add(self._delay * 1000,
                                               self._on_timeout)

    def _fd_watch_out(self, port, condition):
        value = self.printer.query_status()
        os.write(port.fileno(), value)
        return False

    def _fd_watch_in(self, port, condition):
        c = port.read()
        self._reply += c
        if self.printer.status_reply_complete(self._reply):
            self.emit('reply', self._reply)
            self._remove_timeout()
            return False
        return True

    def _on_timeout(self):
        self._remove_timeout()
        self.emit('timeout')
        return False

    def stop(self):
        self._remove_timeout()

    def get_device_name(self):
        return self._device_name

    def get_driver(self):
        return self.printer

    def get_port(self):
        return self._port
