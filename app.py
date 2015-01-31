
import asyncio
import json
import logging
import mimetypes
import os
import signal
import sys

from aiohttp import web


logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger('CallRoulette')

BASE_DIR = os.path.dirname(__file__)
STATIC_FILES = os.path.join(BASE_DIR, 'static')
INDEX_FILE = os.path.join(BASE_DIR, 'index.html')

READ_TIMEOUT = 5.0


class LazyFileHandler:
    def __init__(self, filename, content_type):
        self.filename = filename
        self.content_type = content_type
        self.data = None

    @asyncio.coroutine
    def __call__(self, request):
        if self.data is None:
            try:
                with open(self.filename, 'rb') as f:
                    self.data = f.read()
            except IOError:
                log.warning('Could not load %s file' % self.filename)
                raise web.HTTPNotFound()
        return web.Response(body=self.data, content_type=self.content_type)


class StaticFilesHandler:
    def __init__(self, base_path):
        self.base_path = base_path
        self.cache = {}

    @asyncio.coroutine
    def __call__(self, request):
        path = request.match_info['path']
        try:
            data, content_type = self.cache[path]
        except KeyError:
            full_path = os.path.join(self.base_path, path)
            try:
                with open(full_path, 'rb') as f:
                    content_type, encoding = mimetypes.guess_type(full_path, strict=False)
                    data = f.read()
            except IOError:
                log.warning('Could not open %s file' % path)
                raise web.HTTPNotFound()
            self.cache[path] = data, content_type
            log.debug('Loaded file %s (%s)' % (path, content_type))
        return web.Response(body=data, content_type=content_type)


class Connection:
    def __init__(self, ws):
        self.ws = ws
        self._closed = False

    @property
    def closed(self):
        return self._closed or self.ws.closing

    @asyncio.coroutine
    def read(self, timeout=None):
        try:
            data = yield from asyncio.wait_for(self.ws.receive_str(), timeout)
            return data
        except asyncio.TimeoutError:
            log.warning('Timeout reading from socket')
            self.close()
        except web.WSClientDisconnectedError as e:
            log.info('WS client disconnected: %d:%s' % (e.code, e.message))
            self.close()
        return ''

    def write(self, data):
        self.ws.send_str(data)

    def close(self):
        if self._closed:
            return
        if not self.ws.closing:
            self.ws.close()
        self._closed = True

    @asyncio.coroutine
    def wait_closed(self):
        try:
            yield from self.ws.wait_closed()
        except web.WSClientDisconnectedError:
            pass


class WebSocketHandler:
    def __init__(self):
        self.waiter = None

    @asyncio.coroutine
    def __call__(self, request):
        ws = web.WebSocketResponse(protocols=('callroulette',))
        ws.start(request)

        conn = Connection(ws)
        if self.waiter is None:
            self.waiter = asyncio.Future()
            fs = [conn.read(), self.waiter]
            done, pending = yield from asyncio.wait(fs, return_when=asyncio.FIRST_COMPLETED)
            if self.waiter not in done:
                # the connection was most likely closed
                self.waiter = None
                return ws
            other = self.waiter.result()
            self.waiter = None
            reading_task = pending.pop()
            asyncio.async(self.run_roulette(conn, other, reading_task))
        else:
            self.waiter.set_result(conn)

        yield from conn.wait_closed()

        return ws

    @asyncio.coroutine
    def run_roulette(self, peerA, peerB, initial_reading_task):
        log.info('Running roulette: %s, %s' % (peerA, peerB))

        def _close_connections():
            peerA.close()
            peerB.close()

        # request offer
        data = dict(type='offer_request');
        peerA.write(json.dumps(data))

        # get offer
        # I cannot seem to cancel the reading task that was started before, which is the
        # only way one can know if the connection was closed, so use if for the initial
        # reading
        try:
            data = yield from asyncio.wait_for(initial_reading_task, READ_TIMEOUT)
        except asyncio.TimeoutError:
            data = ''
        if not data:
            return _close_connections()

        data = json.loads(data)
        if data.get('type') != 'offer' or not data.get('sdp'):
            log.warning('Invalid offer received')
            return _close_connections()

        # send offer
        data = dict(type='offer', sdp=data['sdp']);
        peerB.write(json.dumps(data))

        # wait for answer
        data = yield from peerB.read(timeout=READ_TIMEOUT)
        if not data:
            return _close_connections()

        data = json.loads(data)
        if data.get('type') != 'answer' or not data.get('sdp'):
            log.warning('Invalid answer received')
            return _close_connections()

        # dispatch answer
        data = dict(type='answer', sdp=data['sdp']);
        peerA.write(json.dumps(data))

        # wait for end
        fs = [peerA.read(), peerB.read()]
        yield from asyncio.wait(fs, return_when=asyncio.FIRST_COMPLETED)

        # close connections
        return _close_connections()


@asyncio.coroutine
def init(loop):
    app = web.Application(loop=loop)
    app.router.add_route('GET', '/', LazyFileHandler(INDEX_FILE, 'text/html'))
    app.router.add_route('GET', '/ws', WebSocketHandler())
    app.router.add_route('GET', '/static/{path:.*}', StaticFilesHandler(STATIC_FILES))

    handler = app.make_handler()
    server = yield from loop.create_server(handler, '0.0.0.0', 8080)
    print("Server started at 0.0.0.0:8080")
    return server, handler


loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
server, handler = loop.run_until_complete(init(loop))
loop.add_signal_handler(signal.SIGINT, loop.stop)
loop.run_forever()

server.close()
tasks = [server.wait_closed(), handler.finish_connections()]
loop.run_until_complete(asyncio.wait(tasks, loop=loop))
del tasks
loop.close()

sys.exit(0)

