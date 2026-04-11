import asyncio
import decky_plugin
import queue
from settings import SettingsManager

def import_third_party_lib():
    import sys
    from pathlib import Path
    plugin_dir = Path(__file__).parent.resolve()
    decky_plugin.logger.info(f'plugin dir: {plugin_dir}')
    sys.path.insert(0, str(plugin_dir))
    sys.path.insert(0, str(plugin_dir.joinpath("lib")))

def setup_environ_vars():
    import os
    os.environ['XDG_RUNTIME_DIR'] = '/run/user/1000'
    os.environ['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=/run/user/1000/bus'
    os.environ['HOME'] = '/home/deck'

import_third_party_lib()
setup_environ_vars()
settings_dir = decky_plugin.DECKY_PLUGIN_SETTINGS_DIR
settings = SettingsManager(name="settings", settings_directory=settings_dir)
event_queue = queue.Queue()

from dbus_next.aio import MessageBus
from dbus_next import Message, MessageType
from dbus_next.service import ServiceInterface, method, dbus_property, signal
from dbus_next.constants import NameFlag, RequestNameReply
bus = None
_simulate_task = None
_simulate_cookie = None

class AppRequest:
    def __init__(self, sender, cookie, application, reason):
        self.sender = sender
        self.cookie = cookie
        self.application = application
        self.reason = reason
    
    async def is_connected(self):
        if not self.sender:
            return True  # timer-based request (e.g. SimulateUserActivity), always connected
        global bus
        message = Message(
            destination='org.freedesktop.DBus',
            path='/org/freedesktop/DBus',
            interface='org.freedesktop.DBus',
            member='GetConnectionUnixProcessID',
            signature='s',
            body=[self.sender]
        )
        reply = await bus.call(message)
        return reply.message_type != MessageType.ERROR

class BaseInterface(ServiceInterface):
    ignore_application = ["Steam", "./steamwebhelper"]
    request_map = {}
    cookie = 0

    def __init__(self, service):
        super().__init__(service)

    async def _inhibit_impl(self, application, reason):
        if application in BaseInterface.ignore_application: return 0
        decky_plugin.logger.info(f'called Inhibit with application={application} and reason={reason}')
        event_queue.put({"type": "Inhibit"})
        sender = ServiceInterface.last_msg.sender
        BaseInterface.cookie += 1
        BaseInterface.request_map[BaseInterface.cookie] = AppRequest(sender, BaseInterface.cookie, application, reason)
        return BaseInterface.cookie

    async def _un_inhibit_impl(self, cookie):
        if cookie == 0: return
        decky_plugin.logger.info(f'called UnInhibit with cookie={cookie}')
        if BaseInterface.request_map.pop(cookie, None) is None:
            decky_plugin.logger.info(f'cannot find cookie={cookie}')
        if len(BaseInterface.request_map) == 0:
            event_queue.put({"type": "UnInhibit"})

class InhibitInterface(BaseInterface):
    def __init__(self):
        super().__init__('org.freedesktop.ScreenSaver')

    @method()
    async def Inhibit(self, application: 's', reason: 's') -> 'u':
        return await self._inhibit_impl(application, reason)

    @method()
    async def UnInhibit(self, cookie: 'u'):
        return await self._un_inhibit_impl(cookie)

    @method()
    async def GetActive(self) -> 'b':
        return False

    @method()
    async def SimulateUserActivity(self):
        global _simulate_task, _simulate_cookie
        decky_plugin.logger.info('SimulateUserActivity called')
        real_inhibitors = {k: v for k, v in BaseInterface.request_map.items() if k != _simulate_cookie}
        if real_inhibitors:
            return
        if _simulate_task and not _simulate_task.done():
            _simulate_task.cancel()
        if _simulate_cookie is None or _simulate_cookie not in BaseInterface.request_map:
            event_queue.put({"type": "Inhibit"})
            BaseInterface.cookie += 1
            _simulate_cookie = BaseInterface.cookie
            BaseInterface.request_map[_simulate_cookie] = AppRequest('', _simulate_cookie, 'SimulateUserActivity', 'Simulated activity')

        async def auto_uninhibit():
            global _simulate_cookie
            try:
                await asyncio.sleep(90)
            except asyncio.CancelledError:
                return
            if _simulate_cookie is not None and _simulate_cookie in BaseInterface.request_map:
                BaseInterface.request_map.pop(_simulate_cookie)
                _simulate_cookie = None
                if len(BaseInterface.request_map) == 0:
                    event_queue.put({"type": "UnInhibit"})

        _simulate_task = asyncio.ensure_future(auto_uninhibit())

class PMInhibitInterface(BaseInterface):
    def __init__(self):
        super().__init__('org.freedesktop.PowerManagement.Inhibit')

    @method()
    async def Inhibit(self, application: 's', reason: 's') -> 'u':
        return await self._inhibit_impl(application, reason)

    @method()
    async def UnInhibit(self, cookie: 'u'):
        return await self._un_inhibit_impl(cookie)

class GnomeInterface(BaseInterface):
    def __init__(self):
        super().__init__('org.gnome.SessionManager')

    @method()
    async def Inhibit(self, application: 's', xid: 'u', reason: 's', flags: 'u') -> 'u':
        return await self._inhibit_impl(application, reason)

    @method()
    async def Uninhibit(self, cookie: 'u'):
        return await self._un_inhibit_impl(cookie)

class PortalRequestInterface(ServiceInterface):
    """Implements org.freedesktop.portal.Request for portal inhibit handles.
    Used by Firefox's FreeDesktopPortal wake lock path."""
    def __init__(self, cookie, handle_path):
        super().__init__('org.freedesktop.portal.Request')
        self.cookie = cookie
        self.handle_path = handle_path

    @signal()
    def Response(self) -> 'ua{sv}':
        return [0, {}]

    @method()
    async def Close(self):
        decky_plugin.logger.info(f'PortalRequest.Close called for cookie={self.cookie}')
        if self.cookie in BaseInterface.request_map:
            BaseInterface.request_map.pop(self.cookie, None)
            if len(BaseInterface.request_map) == 0:
                event_queue.put({"type": "UnInhibit"})
        global bus
        if bus:
            try:
                bus.unexport(self.handle_path, self)
            except Exception as e:
                decky_plugin.logger.info(f'PortalRequest unexport error: {e}')

class PortalInhibitInterface(BaseInterface):
    """Implements org.freedesktop.portal.Inhibit on org.freedesktop.portal.Desktop.
    Firefox tries this interface first (before org.freedesktop.ScreenSaver)."""
    _handle_counter = 0

    def __init__(self):
        super().__init__('org.freedesktop.portal.Inhibit')

    @method()
    async def Inhibit(self, parent_window: 's', flags: 'u', options: 'a{sv}') -> 'o':
        # flags: 1=Logout, 2=UserSwitch, 4=Suspend, 8=Idle
        reasons = []
        if flags & 4:
            reasons.append('Suspend')
        if flags & 8:
            reasons.append('Idle')
        reason = ', '.join(reasons) if reasons else 'Inhibit'

        sender = ServiceInterface.last_msg.sender
        sender_safe = sender.lstrip(':').replace('.', '_')

        PortalInhibitInterface._handle_counter += 1
        handle_token = f'inhibit{PortalInhibitInterface._handle_counter}'
        if 'handle_token' in options:
            token_var = options['handle_token']
            if hasattr(token_var, 'value'):
                handle_token = str(token_var.value)

        handle = f'/org/freedesktop/portal/desktop/request/{sender_safe}/{handle_token}'

        cookie = await self._inhibit_impl('Portal', reason)
        if cookie == 0:
            return handle  # application was in ignore list

        global bus
        if bus:
            request_iface = PortalRequestInterface(cookie, handle)
            bus.export(handle, request_iface)

            async def emit_response():
                await asyncio.sleep(0)
                request_iface.Response()

            asyncio.ensure_future(emit_response())

        return handle

async def stop_dbus():
    global bus
    try:
        if bus is not None:
            bus.disconnect()
        bus = None
    except Exception as e:
        decky_plugin.logger.info(f"error: {e}")

async def start_dbus():
    global bus
    await stop_dbus()
    try:
        bus = await MessageBus().connect()
        interface = InhibitInterface()
        pm_interface = PMInhibitInterface()
        gnome_interface = GnomeInterface()
        bus.export('/ScreenSaver', interface) # vlc
        bus.export('/org/freedesktop/ScreenSaver', interface) # chrome, kodi
        bus.export('/org/freedesktop/PowerManagement/Inhibit', pm_interface) # wiliwili
        bus.export('/org/gnome/SessionManager', gnome_interface) # mpv with https://github.com/Guldoman/mpv_inhibit_gnome installed
        await bus.request_name('org.freedesktop.PowerManagement')
        await bus.request_name('org.freedesktop.ScreenSaver')
        await bus.request_name('org.gnome.SessionManager')
        # Try to register as org.freedesktop.portal.Desktop so Firefox can use its
        # preferred portal-based inhibit path (tried before org.freedesktop.ScreenSaver).
        # Skip gracefully if another portal service already owns the name.
        portal_interface = PortalInhibitInterface()
        try:
            portal_reply = await bus.request_name('org.freedesktop.portal.Desktop', NameFlag.DO_NOT_QUEUE)
            if portal_reply == RequestNameReply.PRIMARY_OWNER:
                bus.export('/org/freedesktop/portal/desktop', portal_interface)
                decky_plugin.logger.info('Registered as org.freedesktop.portal.Desktop (Firefox portal path)')
            else:
                decky_plugin.logger.info(f'Portal name not available (reply={portal_reply}), skipping portal interface')
        except Exception as e:
            decky_plugin.logger.info(f'Could not register portal interface: {e}')
    except Exception as e:
        decky_plugin.logger.info(f"error: {e}")

class Plugin:

    async def start_backend(self):
        decky_plugin.logger.info("Start backend server")
        await start_dbus()

    async def stop_backend(self):
        decky_plugin.logger.info("Stop backend server")
        await stop_dbus()
        event_queue.queue.clear()

    async def is_running(self):
        global bus
        return bus is not None

    async def get_event(self):
        global bus
        if bus is None:
            return []
        res = []
        while not event_queue.empty():
            try:
                res.append(event_queue.get_nowait())
            except queue.Empty:
                continue
        if len(res) > 0:
            return res
        # check closed dbus connection
        cookies = list(BaseInterface.request_map.keys())
        clear = False
        for c in cookies:
            connected = await BaseInterface.request_map[c].is_connected()
            if not connected:
                BaseInterface.request_map.pop(c)
                clear = True
        if clear and len(BaseInterface.request_map) == 0:
            return [{"type": "UnInhibit"}]
        return []

    async def get_settings(self, key: str, defaults):
        decky_plugin.logger.info('[settings] get {}'.format(key))
        return settings.getSetting(key, defaults)

    async def set_settings(self, key: str, value):
        decky_plugin.logger.info('[settings] set {}: {}'.format(key, value))
        return settings.setSetting(key, value)

    async def _main(self):
        decky_plugin.logger.info("Hello World!")

    async def _unload(self):
        decky_plugin.logger.info("Goodnight World!")
        await stop_dbus()

    async def _uninstall(self):
        pass

    async def _migration(self):
        pass