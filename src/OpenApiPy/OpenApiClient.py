from twisted.internet.defer import timeout
from twisted.internet.protocol import Factory
from twisted.internet.endpoints import clientFromString
from twisted.application.internet import ClientService
from protocol import Protocol
from protobuf import Protobuf
import threading
from twisted.internet import reactor

class Client(ClientService):
    EVENT_CONNECT_NAME = "connect"
    EVENT_DISCONNECT_NAME = "disconnect"
    EVENT_MESSAGE_NAME = "message"

    class Protocol(Protocol):
        client = None

        def connectionMade(self):
            super().connectionMade()
            self.client.connect()

        def connectionLost(self, reason):
            super().connectionLost(reason)
            self.client.disconnect()

        def receive(self, message):
            self.client.receive(message)

    class Factory(Factory):
        client = None

        def __init__(self, *args, **kwargs):
            super().__init__()
            self.client = kwargs['client']

        def buildProtocol(self, addr):
            p = super().buildProtocol(addr)
            p.client = self.client
            return p

    def __init__(self, host, port, retryPolicy=None, clock=None, prepareConnection=None):
        self._runningReactor = reactor
        endpoint = clientFromString(self._runningReactor, f"ssl:{host}:{port}")
        factory = Client.Factory.forProtocol(Client.Protocol, client=self)
        super().__init__(endpoint, factory, retryPolicy=retryPolicy, clock=clock, prepareConnection=prepareConnection)

    def start(self, timeout=None):
        def run(timeout):
            self.startService()
            if timeout:
                self._runningReactor.callLater(timeout, self.stop)
            self._runningReactor.run(installSignalHandlers=False)
        self._reactorThread = threading.Thread(target=run,args=(timeout,))
        self._reactorThread.start()

    def stop(self):
        self.stopService()
        if self._runningReactor.running:
            self._runningReactor.stop()

    def connect(self):
        self.exec_events(self.EVENT_CONNECT_NAME)

    def disconnect(self):
        self.exec_events(self.EVENT_DISCONNECT_NAME)

    _responseCallbacks = dict()

    def receive(self, message):
        payload = Protobuf.extract(message)
        kargs = dict(msg=message, msgid=message.clientMsgId,
                     msgtype=message.payloadType,
                     payload=payload,
                     **{fv[0].name: fv[1] for fv in payload.ListFields()})

        if "ctidTraderAccountId" in kargs:
            kargs["ctid"] = payload.ctidTraderAccountId

        self.exec_events(self.EVENT_MESSAGE_NAME, **kargs)

        if (message.clientMsgId is not None and message.clientMsgId in self._responseCallbacks):
            responseCallback = self._responseCallbacks[message.clientMsgId]
            self._responseCallbacks.pop(message.clientMsgId)
            responseCallback(message)

    def send(self, message, msgid=None, responseCallback=None, **params):
        if type(message) in [str, int]:
            message = Protobuf.get(message, **params)

        if msgid is None and responseCallback is not None:
            msgid = str(id(responseCallback))

        if (msgid is not None):
            self._responseCallbacks[msgid] = responseCallback

        def protocol_send(protocol):
            protocol.send(message, msgid=msgid)

        con = self.whenConnected()
        con.addCallback(protocol_send)
        return con

    _events = dict()

    def event(self, name_or_func=None, **filters):
        if not self._events:  # lazy create
            for e in [self.EVENT_CONNECT_NAME,
                      self.EVENT_DISCONNECT_NAME, self.EVENT_MESSAGE_NAME]:
                self._events[e] = []

        if callable(name_or_func):  # callable append
            name = name_or_func.__name__
            self._events[name].append(name_or_func)
            return name_or_func

        def decorate(func):  # decorate with args
            evname = name_or_func

            from functools import wraps

            @wraps(func)
            def func_wrap(*args, **kwargs):
                for k, v in filters.items():
                    if k not in kwargs or kwargs[k] != v:
                        return
                func(*args, **kwargs)

            self._events[evname].append(func_wrap)
            return func

        return decorate

    def message(self, **filters):
        if 'msgtype' in filters and type(filters['msgtype']) in [str, int]:
            filters['msgtype'] = Protobuf.get_type(filters['msgtype'])

        return self.event(self.EVENT_MESSAGE_NAME, **filters)

    def exec_events(self, name, *args, **kwargs):
        if name not in self._events:
            return

        for f in self._events[name]:
            f(*args, **kwargs)

if __name__ == "__main__":
    c = Client("demo.ctraderapi.com", 5035) # Demo connection

    @c.event
    def connect():
        c.send("VersionReq", responseCallback=callback)

    def callback(message):
        print("Called back: ", message)

    @c.message(msgtype="VersionRes")
    def version(msg, payload, version, **kargs):
        print("Server version: ", version)
        c.stop()

    c.start(timeout=5) # optional timeout in seconds