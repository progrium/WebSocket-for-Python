import base64
from hashlib import sha1
import types
import socket

import gevent.pywsgi
import gevent.coros

from ws4py import WS_KEY
from ws4py.exc import HandshakeError
from ws4py.streaming import Stream

WS_VERSION = 8

class _UpgradableWSGIHandler(gevent.pywsgi.WSGIHandler):
    def handle_one_response(self):
        connection_header = self.environ.get('HTTP_CONNECTION', '').lower()
        if connection_header == 'upgrade' and self.server.upgrade_handler:
            upgrade_header = self.environ.get('HTTP_UPGRADE', '').lower()
            handler = self.server.upgrade_handler(upgrade_header, self.environ)
            if handler:
                handler(self.socket, self.environ)
                self.rfile.close() # make sure WSGIHandler stops processing requests
                return
        gevent.pywsgi.WSGIHandler.handle_one_response(self)
        
class _UpgradableWSGIServer(gevent.pywsgi.WSGIServer):
    handler_class = _UpgradableWSGIHandler
    
    def __init__(self, *args, **kwargs):
        self.upgrade_handler = kwargs.pop('upgrade_handler', None)
        gevent.pywsgi.WSGIServer.__init__(self, *args, **kwargs)
        

class WebSocketServer(_UpgradableWSGIServer):
    def __init__(self, *args, **kwargs):
        gevent.pywsgi.WSGIServer.__init__(self, *args, **kwargs)
        self.protocols = kwargs.pop('websocket_protocols', [])
        self.extensions = kwargs.pop('websocket_extensions', [])
        self.websocket_handler = self.application
        self.application = lambda a,b: None
    
    def upgrade_handler(self, protocol, environ):
        if protocol == 'websocket':
            return self.websocket_upgrade
    
    def websocket_upgrade(self, socket, environ):
        ws_protocols = None
        ws_location = None
        ws_key = None
        ws_extensions = []
        
        if environ.get('REQUEST_METHOD') != 'GET':
            raise HandshakeError('Method is not GET')
        
        key = environ.get('HTTP_SEC_WEBSOCKET_KEY')
        if key:
            ws_key = base64.b64decode(key)
            if len(ws_key) != 16:
                raise HandshakeError("WebSocket key's length is invalid")
        
        version = environ.get('HTTP_SEC_WEBSOCKET_VERSION')
        if version:
            if version != str(WS_VERSION):
                raise HandshakeError('Unsupported WebSocket version')
        else:
            raise HandshakeError('WebSocket version required')
        
        protocols = self.protocols or []
        subprotocols = environ.get('HTTP_SEC_WEBSOCKET_PROTOCOL')
        if subprotocols:
            ws_protocols = []
            for s in subprotocols.split(','):
                s = s.strip()
                if s in protocols:
                    ws_protocols.append(s)

        exts = self.extensions or []
        extensions = environ.get('HTTP_SEC_WEBSOCKET_EXTENSIONS')
        if extensions:
            for ext in extensions.split(','):
                ext = ext.strip()
                if ext in exts:
                    ws_extensions.append(ext)
        
        location = []
        include_port = False
        if environ.get('wsgi.url_scheme') == "https":
            location.append("wss://")
            include_port = environ.get('SERVER_PORT') != '443'
        else:
            location.append("ws://")
            include_port = environ.get('SERVER_PORT') != '80'
        location.append(environ.get('SERVER_NAME'))
        if include_port:
            location.append(":%s" % environ.get('SERVER_PORT'))
        location.append(environ.get('PATH_INFO'))
        if environ.get('QUERY_STRING') != "":
            location.append("?%s" % environ.get('QUERY_STRING'))
        ws_location = ''.join(location)
        
        headers = [
            ('Upgrade', 'websocket'),
            ('Connection', 'Upgrade'),
            ('Sec-WebSocket-Version', str(WS_VERSION)),
            ('Sec-WebSocket-Accept', base64.b64encode(sha1(key + WS_KEY).digest())),
        ]
        if ws_protocols:
            headers.append(('Sec-WebSocket-Protocol', ', '.join(ws_protocols)))
        if ws_extensions:
            headers.append(('Sec-WebSocket-Extensions', ','.join(ws_extensions)))
        
        towrite = ['HTTP/1.1 101 Switching Protocols\r\n']
        for header in headers:
            towrite.append("%s: %s\r\n" % header)
        towrite.append("\r\n")
        socket.sendall(''.join(towrite))
        
        websocket = WebSocket(socket, ws_protocols, ws_extensions, environ)
        self.websocket_handler(websocket, environ)
        

class WebSocket(object):
    def __init__(self, sock, protocols, extensions, environ):
        self.stream = Stream()
        
        self.protocols = protocols
        self.extensions = extensions
        self.environ = environ

        self.sock = sock
        self.sock.settimeout(30.0)
        
        self.client_terminated = False
        self.server_terminated = False
        
        self._lock = gevent.coros.Semaphore()

    def close(self, code=1000, reason=''):
        """
        Call this method to initiate the websocket connection
        closing by sending a close frame to the connected peer.

        Once this method is called, the server_terminated
        attribute is set. Calling this method several times is
        safe as the closing frame will be sent only the first
        time.

        @param code: status code describing why the connection is closed
        @param reason: a human readable message describing why the connection is closed
        """
        if not self.server_terminated:
            self.server_terminated = True
            self.write_to_connection(self.stream.close(code=code, reason=reason))

    @property
    def terminated(self):
        """
        Returns True if both the client and server have been
        marked as terminated.
        """
        return self.client_terminated is True and self.server_terminated is True

    def write_to_connection(self, bytes):
        """
        Writes the provided bytes to the underlying connection.

        @param bytes: data tio send out
        """
        return self.sock.sendall(bytes)

    def read_from_connection(self, amount):
        """
        Reads bytes from the underlying connection.

        @param amount: quantity to read (if possible)
        """
        return self.sock.recv(amount)
        
    def close_connection(self):
        """
        Shutdowns then closes the underlying connection.
        """
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
        except:
            pass

    def send(self, payload, binary=False):
        """
        Sends the given payload out.

        If payload is some bytes or a bytearray,
        then it is sent as a single message not fragmented.

        If payload is a generator, each chunk is sent as part of
        fragmented message.

        @param payload: string, bytes, bytearray or a generator
        @param binary: if set, handles the payload as a binary message
        """
        if isinstance(payload, basestring) or isinstance(payload, bytearray):
            if not binary:
                self.write_to_connection(self.stream.text_message(payload).single())
            else:
                self.write_to_connection(self.stream.binary_message(payload).single())
                
        elif type(payload) == types.GeneratorType:
            bytes = payload.next()
            first = True
            for chunk in payload:
                if not binary:
                    self.write_to_connection(self.stream.text_message(bytes).fragment(first=first))
                else:
                    self.write_to_connection(self.stream.binary_message(payload).fragment(first=first))
                bytes = chunk
                first = False
            if not binary:
                self.write_to_connection(self.stream.text_message(bytes).fragment(last=True))
            else:
                self.write_to_connection(self.stream.text_message(bytes).fragment(last=True))

    def receive(self):
        """
        Performs the operation of reading from the underlying
        connection in order to feed the stream of bytes.

        We start with a small size of two bytes to be read
        from the connection so that we can quickly parse an
        incoming frame header. Then the stream indicates
        whatever size must be read from the connection since
        it knows the frame payload length.

        Note that we perform some automatic opererations:

        * On a closing message, we respond with a closing
          message and finally close the connection
        * We respond to pings with pong messages.
        * Whenever an error is raised by the stream parsing,
          we initiate the closing of the connection with the
          appropiate error code.
        """
        next_size = 2
        #try:
        while not self.terminated:
            bytes = self.read_from_connection(next_size)
            if not bytes and next_size > 0:
                raise IOError()
            
            with self._lock:
                s = self.stream
                next_size = s.parser.send(bytes)
                
                for ping in s.pings:
                    self.write_to_connection(s.pong(str(ping.data)))
                s.pings = []
                s.pongs = []
                
                if s.closing is not None:
                    if not self.server_terminated:
                        next_size = 2
                        self.close(s.closing.code, s.closing.reason)
                    else:
                        self.client_terminated = True
                    raise IOError()
        
                elif s.errors:
                    errors = s.errors[:]
                    for error in s.errors:
                        self.close(error.code, error.reason)
                        s.errors.remove(error)
                    raise IOError()
                        
                elif s.has_message:
                    message = str(s.message)
                    s.message.data = None
                    s.message = None
                    return message
        
        #except:
        #    print "".join(traceback.format_exception(*exc_info()))
        #finally:
        #    self.client_terminated = self.server_terminated = True
        #    self.close_connection()

if __name__ == '__main__':
    def echo_handler(websocket, environ):
        while True:
            websocket.send(websocket.receive())
    
    server = WebSocketServer(('127.0.0.1', 8088), echo_handler)
    server.serve_forever()