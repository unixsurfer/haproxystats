#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
"""
A very simple TCP server for simulating a graphite relay, copied-paste from
Python documentation. Few things were adjusted to make pylint happy and print
incoming data.
"""
import asyncio


class EchoServerClientProtocol(asyncio.Protocol):
    """
    A TCP server
    """
    def __init__(self):
        self.peername = None
        self.transport = None

    def connection_made(self, transport):
        self.peername = transport.get_extra_info('peername')
        print('Connection from {}'.format(self.peername))
        self.transport = transport

    def data_received(self, data):
        message = data.decode()
        print(message)

    def connection_lost(self, exc):
        print('client {} closed connection {}'.format(self.peername, exc))


def main():
    """
    main code
    """
    loop = asyncio.get_event_loop()
    # Each client connection will create a new protocol instance
    coro = loop.create_server(EchoServerClientProtocol, '127.0.0.1', 39991)
    server = loop.run_until_complete(coro)

    # Serve requests until Ctrl+C is pressed
    print('Serving on {}'.format(server.sockets[0].getsockname()))
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    # Close the server
    server.close()
    loop.run_until_complete(server.wait_closed())
    loop.close()

# This is the standard boilerplate that calls the main() function.
if __name__ == '__main__':
    main()
