#!/usr/bin/env python
#
# Copyright (C) 2015 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Tests for the adb program itself.

This differs from things in test_device.py in that there is no API for these
things. Most of these tests involve specific error messages or the help text.
"""
from __future__ import print_function

import binascii
import contextlib
import os
import random
import select
import socket
import struct
import subprocess
import threading
import unittest

import adb


@contextlib.contextmanager
def fake_adbd(protocol=socket.AF_INET, port=0):
    """Creates a fake ADB daemon that just replies with a CNXN packet."""

    serversock = socket.socket(protocol, socket.SOCK_STREAM)
    serversock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if protocol == socket.AF_INET:
        serversock.bind(('127.0.0.1', port))
    else:
        serversock.bind(('::1', port))
    serversock.listen(1)

    # A pipe that is used to signal the thread that it should terminate.
    readpipe, writepipe = os.pipe()

    def _adb_packet(command, arg0, arg1, data):
        bin_command = struct.unpack('I', command)[0]
        buf = struct.pack('IIIIII', bin_command, arg0, arg1, len(data), 0,
                          bin_command ^ 0xffffffff)
        buf += data
        return buf

    def _handle():
        rlist = [readpipe, serversock]
        cnxn_sent = {}
        while True:
            read_ready, _, _ = select.select(rlist, [], [])
            for ready in read_ready:
                if ready == readpipe:
                    # Closure pipe
                    os.close(ready)
                    serversock.shutdown(socket.SHUT_RDWR)
                    serversock.close()
                    return
                elif ready == serversock:
                    # Server socket
                    conn, _ = ready.accept()
                    rlist.append(conn)
                else:
                    # Client socket
                    data = ready.recv(1024)
                    if not data or data.startswith('OPEN'):
                        if ready in cnxn_sent:
                            del cnxn_sent[ready]
                        ready.shutdown(socket.SHUT_RDWR)
                        ready.close()
                        rlist.remove(ready)
                        continue
                    if ready in cnxn_sent:
                        continue
                    cnxn_sent[ready] = True
                    ready.sendall(_adb_packet('CNXN', 0x01000001, 1024 * 1024,
                                              'device::ro.product.name=fakeadb'))

    port = serversock.getsockname()[1]
    server_thread = threading.Thread(target=_handle)
    server_thread.start()

    try:
        yield port
    finally:
        os.close(writepipe)
        server_thread.join()


@contextlib.contextmanager
def adb_connect(unittest, serial):
    """Context manager for an ADB connection.

    This automatically disconnects when done with the connection.
    """

    output = subprocess.check_output(['adb', 'connect', serial])
    unittest.assertEqual(output.strip(), 'connected to {}'.format(serial))

    try:
        yield
    finally:
        # Perform best-effort disconnection. Discard the output.
        subprocess.Popen(['adb', 'disconnect', serial],
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE).communicate()


@contextlib.contextmanager
def adb_server():
    """Context manager for an ADB server.

    This creates an ADB server and returns the port it's listening on.
    """

    port = 5038
    # Kill any existing server on this non-default port.
    subprocess.check_output(['adb', '-P', str(port), 'kill-server'],
                            stderr=subprocess.STDOUT)
    read_pipe, write_pipe = os.pipe()
    proc = subprocess.Popen(['adb', '-L', 'tcp:localhost:{}'.format(port),
                             'fork-server', 'server',
                             '--reply-fd', str(write_pipe)])
    try:
        os.close(write_pipe)
        greeting = os.read(read_pipe, 1024)
        assert greeting == 'OK\n', repr(greeting)
        yield port
    finally:
        proc.terminate()
        proc.wait()


class CommandlineTest(unittest.TestCase):
    """Tests for the ADB commandline."""

    def test_help(self):
        """Make sure we get _something_ out of help."""
        out = subprocess.check_output(
            ['adb', 'help'], stderr=subprocess.STDOUT)
        self.assertGreater(len(out), 0)

    def test_version(self):
        """Get a version number out of the output of adb."""
        lines = subprocess.check_output(['adb', 'version']).splitlines()
        version_line = lines[0]
        self.assertRegexpMatches(
            version_line, r'^Android Debug Bridge version \d+\.\d+\.\d+$')
        if len(lines) == 2:
            # Newer versions of ADB have a second line of output for the
            # version that includes a specific revision (git SHA).
            revision_line = lines[1]
            self.assertRegexpMatches(
                revision_line, r'^Revision [0-9a-f]{12}-android$')

    def test_tcpip_error_messages(self):
        """Make sure 'adb tcpip' parsing is sane."""
        proc = subprocess.Popen(['adb', 'tcpip'], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        self.assertEqual(1, proc.returncode)
        self.assertIn('requires an argument', out)

        proc = subprocess.Popen(['adb', 'tcpip', 'foo'], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        self.assertEqual(1, proc.returncode)
        self.assertIn('invalid port', out)


class ServerTest(unittest.TestCase):
    """Tests for the ADB server."""

    @staticmethod
    def _read_pipe_and_set_event(pipe, event):
        """Reads a pipe until it is closed, then sets the event."""
        pipe.read()
        event.set()

    def test_handle_inheritance(self):
        """Test that launch_server() does not inherit handles.

        launch_server() should not let the adb server inherit
        stdin/stdout/stderr handles, which can cause callers of adb.exe to hang.
        This test also runs fine on unix even though the impetus is an issue
        unique to Windows.
        """
        # This test takes 5 seconds to run on Windows: if there is no adb server
        # running on the the port used below, adb kill-server tries to make a
        # TCP connection to a closed port and that takes 1 second on Windows;
        # adb start-server does the same TCP connection which takes another
        # second, and it waits 3 seconds after starting the server.

        # Start adb client with redirected stdin/stdout/stderr to check if it
        # passes those redirections to the adb server that it starts. To do
        # this, run an instance of the adb server on a non-default port so we
        # don't conflict with a pre-existing adb server that may already be
        # setup with adb TCP/emulator connections. If there is a pre-existing
        # adb server, this also tests whether multiple instances of the adb
        # server conflict on adb.log.

        port = 5038
        # Kill any existing server on this non-default port.
        subprocess.check_output(['adb', '-P', str(port), 'kill-server'],
                                stderr=subprocess.STDOUT)

        try:
            # Run the adb client and have it start the adb server.
            proc = subprocess.Popen(['adb', '-P', str(port), 'start-server'],
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)

            # Start threads that set events when stdout/stderr are closed.
            stdout_event = threading.Event()
            stdout_thread = threading.Thread(
                target=ServerTest._read_pipe_and_set_event,
                args=(proc.stdout, stdout_event))
            stdout_thread.daemon = True
            stdout_thread.start()

            stderr_event = threading.Event()
            stderr_thread = threading.Thread(
                target=ServerTest._read_pipe_and_set_event,
                args=(proc.stderr, stderr_event))
            stderr_thread.daemon = True
            stderr_thread.start()

            # Wait for the adb client to finish. Once that has occurred, if
            # stdin/stderr/stdout are still open, it must be open in the adb
            # server.
            proc.wait()

            # Try to write to stdin which we expect is closed. If it isn't
            # closed, we should get an IOError. If we don't get an IOError,
            # stdin must still be open in the adb server. The adb client is
            # probably letting the adb server inherit stdin which would be
            # wrong.
            with self.assertRaises(IOError):
                proc.stdin.write('x')

            # Wait a few seconds for stdout/stderr to be closed (in the success
            # case, this won't wait at all). If there is a timeout, that means
            # stdout/stderr were not closed and and they must be open in the adb
            # server, suggesting that the adb client is letting the adb server
            # inherit stdout/stderr which would be wrong.
            self.assertTrue(stdout_event.wait(5), "adb stdout not closed")
            self.assertTrue(stderr_event.wait(5), "adb stderr not closed")
        finally:
            # If we started a server, kill it.
            subprocess.check_output(['adb', '-P', str(port), 'kill-server'],
                                    stderr=subprocess.STDOUT)


class EmulatorTest(unittest.TestCase):
    """Tests for the emulator connection."""

    def _reset_socket_on_close(self, sock):
        """Use SO_LINGER to cause TCP RST segment to be sent on socket close."""
        # The linger structure is two shorts on Windows, but two ints on Unix.
        linger_format = 'hh' if os.name == 'nt' else 'ii'
        l_onoff = 1
        l_linger = 0

        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                        struct.pack(linger_format, l_onoff, l_linger))
        # Verify that we set the linger structure properly by retrieving it.
        linger = sock.getsockopt(socket.SOL_SOCKET, socket.SO_LINGER, 16)
        self.assertEqual((l_onoff, l_linger),
                         struct.unpack_from(linger_format, linger))

    def test_emu_kill(self):
        """Ensure that adb emu kill works.

        Bug: https://code.google.com/p/android/issues/detail?id=21021
        """
        with contextlib.closing(
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as listener:
            # Use SO_REUSEADDR so subsequent runs of the test can grab the port
            # even if it is in TIME_WAIT.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(4)
            port = listener.getsockname()[1]

            # Now that listening has started, start adb emu kill, telling it to
            # connect to our mock emulator.
            proc = subprocess.Popen(
                ['adb', '-s', 'emulator-' + str(port), 'emu', 'kill'],
                stderr=subprocess.STDOUT)

            accepted_connection, addr = listener.accept()
            with contextlib.closing(accepted_connection) as conn:
                # If WSAECONNABORTED (10053) is raised by any socket calls,
                # then adb probably isn't reading the data that we sent it.
                conn.sendall('Android Console: type \'help\' for a list ' +
                             'of commands\r\n')
                conn.sendall('OK\r\n')

                with contextlib.closing(conn.makefile()) as connf:
                    line = connf.readline()
                    if line.startswith('auth'):
                        # Ignore the first auth line.
                        line = connf.readline()
                    self.assertEqual('kill\n', line)
                    self.assertEqual('quit\n', connf.readline())

                conn.sendall('OK: killing emulator, bye bye\r\n')

                # Use SO_LINGER to send TCP RST segment to test whether adb
                # ignores WSAECONNRESET on Windows. This happens with the
                # real emulator because it just calls exit() without closing
                # the socket or calling shutdown(SD_SEND). At process
                # termination, Windows sends a TCP RST segment for every
                # open socket that shutdown(SD_SEND) wasn't used on.
                self._reset_socket_on_close(conn)

            # Wait for adb to finish, so we can check return code.
            proc.communicate()

            # If this fails, adb probably isn't ignoring WSAECONNRESET when
            # reading the response from the adb emu kill command (on Windows).
            self.assertEqual(0, proc.returncode)

    def test_emulator_connect(self):
        """Ensure that the emulator can connect.

        Bug: http://b/78991667
        """
        with adb_server() as server_port:
            with fake_adbd() as port:
                serial = 'emulator-{}'.format(port - 1)
                # Ensure that the emulator is not there.
                try:
                    subprocess.check_output(['adb', '-P', str(server_port),
                                             '-s', serial, 'get-state'],
                                            stderr=subprocess.STDOUT)
                    self.fail('Device should not be available')
                except subprocess.CalledProcessError as err:
                    self.assertEqual(
                        err.output.strip(),
                        'error: device \'{}\' not found'.format(serial))

                # Let the ADB server know that the emulator has started.
                with contextlib.closing(
                    socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
                    sock.connect(('localhost', server_port))
                    command = 'host:emulator:{}'.format(port)
                    sock.sendall('%04x%s' % (len(command), command))

                # Ensure the emulator is there.
                subprocess.check_call(['adb', '-P', str(server_port),
                                       '-s', serial, 'wait-for-device'])
                output = subprocess.check_output(['adb', '-P', str(server_port),
                                                  '-s', serial, 'get-state'])
                self.assertEqual(output.strip(), 'device')


class ConnectionTest(unittest.TestCase):
    """Tests for adb connect."""

    def test_connect_ipv4_ipv6(self):
        """Ensure that `adb connect localhost:1234` will try both IPv4 and IPv6.

        Bug: http://b/30313466
        """
        for protocol in (socket.AF_INET, socket.AF_INET6):
            try:
                with fake_adbd(protocol=protocol) as port:
                    serial = 'localhost:{}'.format(port)
                    with adb_connect(self, serial):
                        pass
            except socket.error:
                print("IPv6 not available, skipping")
                continue

    def test_already_connected(self):
        """Ensure that an already-connected device stays connected."""

        with fake_adbd() as port:
            serial = 'localhost:{}'.format(port)
            with adb_connect(self, serial):
                # b/31250450: this always returns 0 but probably shouldn't.
                output = subprocess.check_output(['adb', 'connect', serial])
                self.assertEqual(
                    output.strip(), 'already connected to {}'.format(serial))

    def test_reconnect(self):
        """Ensure that a disconnected device reconnects."""

        with fake_adbd() as port:
            serial = 'localhost:{}'.format(port)
            with adb_connect(self, serial):
                output = subprocess.check_output(['adb', '-s', serial,
                                                  'get-state'])
                self.assertEqual(output.strip(), 'device')

                # This will fail.
                proc = subprocess.Popen(['adb', '-s', serial, 'shell', 'true'],
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT)
                output, _ = proc.communicate()
                self.assertEqual(output.strip(), 'error: closed')

                subprocess.check_call(['adb', '-s', serial, 'wait-for-device'])

                output = subprocess.check_output(['adb', '-s', serial,
                                                  'get-state'])
                self.assertEqual(output.strip(), 'device')

                # Once we explicitly kick a device, it won't attempt to
                # reconnect.
                output = subprocess.check_output(['adb', 'disconnect', serial])
                self.assertEqual(
                    output.strip(), 'disconnected {}'.format(serial))
                try:
                    subprocess.check_output(['adb', '-s', serial, 'get-state'],
                                            stderr=subprocess.STDOUT)
                    self.fail('Device should not be available')
                except subprocess.CalledProcessError as err:
                    self.assertEqual(
                        err.output.strip(),
                        'error: device \'{}\' not found'.format(serial))


def main():
    """Main entrypoint."""
    random.seed(0)
    unittest.main(verbosity=3)


if __name__ == '__main__':
    main()
