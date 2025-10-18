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
from dbus_next import Message, MessageType, BusType
from dbus_next.service import ServiceInterface, method, dbus_property, signal
bus = None
registered_services = set()  # Track which services we have registered
last_desktop_mode = None  # Track the last known desktop mode state
name_owner_watch_rules = []  # Track D-Bus match rules we've added

class AppRequest:
    def __init__(self, sender, cookie, application, reason):
        self.sender = sender
        self.cookie = cookie
        self.application = application
        self.reason = reason
    
    async def is_connected(self):
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

async def stop_dbus():
    global bus, registered_services, last_desktop_mode, name_owner_watch_rules
    try:
        if bus is not None:
            # Remove match rules
            for rule in name_owner_watch_rules:
                try:
                    msg = Message(
                        destination='org.freedesktop.DBus',
                        path='/org/freedesktop/DBus',
                        interface='org.freedesktop.DBus',
                        member='RemoveMatch',
                        signature='s',
                        body=[rule]
                    )
                    await bus.call(msg)
                except Exception as e:
                    decky_plugin.logger.info(f"Error removing match rule: {e}")
            
            # Unregister services before disconnecting
            await unregister_dbus_services()
            bus.disconnect()
        bus = None
        registered_services = set()
        last_desktop_mode = None
        name_owner_watch_rules = []
    except Exception as e:
        decky_plugin.logger.info(f"error: {e}")

async def is_service_owned(service_name):
    """Check if a D-Bus service name is already owned by another process"""
    global bus
    try:
        message = Message(
            destination='org.freedesktop.DBus',
            path='/org/freedesktop/DBus',
            interface='org.freedesktop.DBus',
            member='NameHasOwner',
            signature='s',
            body=[service_name]
        )
        reply = await bus.call(message)
        if reply.message_type == MessageType.ERROR:
            return False
        return reply.body[0] if reply.body else False
    except Exception as e:
        decky_plugin.logger.info(f"Error checking service ownership for {service_name}: {e}")
        return False

async def is_desktop_mode():
    """
    Detect if we're in desktop mode (KDE Plasma) vs gaming mode.
    Desktop mode is detected by checking if KDE-specific services are running.
    """
    global bus
    if bus is None:
        return False
    try:
        # Check for KDE Plasma shell service as indicator of desktop mode
        kde_services = [
            'org.kde.plasmashell',
            'org.kde.KWin',
            'org.kde.Solid.PowerManagement'
        ]
        for service in kde_services:
            if await is_service_owned(service):
                return True
        return False
    except Exception as e:
        decky_plugin.logger.info(f"Error checking desktop mode: {e}")
        return False

def handle_name_owner_changed(name, old_owner, new_owner):
    """
    Callback for D-Bus NameOwnerChanged signals.
    This detects when KDE services are starting (before they register conflicting services).
    """
    global bus, registered_services, last_desktop_mode
    
    # KDE services that indicate desktop mode
    kde_services = [
        'org.kde.plasmashell',
        'org.kde.KWin', 
        'org.kde.Solid.PowerManagement'
    ]
    
    # Check if a KDE service is starting (new_owner is not empty)
    if name in kde_services and new_owner and not old_owner:
        decky_plugin.logger.info(f"KDE service {name} starting, immediately releasing our D-Bus services")
        # Schedule immediate release of our services
        import asyncio
        if bus is not None:
            asyncio.create_task(unregister_dbus_services())
            last_desktop_mode = True
    
    # Check if KDE services are stopping (new_owner is empty and old_owner existed)
    elif name in kde_services and old_owner and not new_owner:
        decky_plugin.logger.info(f"KDE service {name} stopped")
        # Will be handled by check_and_manage_services which verifies all KDE services are gone

async def register_dbus_services():
    """Register D-Bus services for gaming mode"""
    global bus, registered_services
    try:
        if 'screensaver' not in registered_services:
            interface = InhibitInterface()
            bus.export('/ScreenSaver', interface) # vlc
            bus.export('/org/freedesktop/ScreenSaver', interface) # chrome
            await bus.request_name('org.freedesktop.ScreenSaver')
            registered_services.add('screensaver')
            decky_plugin.logger.info("Registered org.freedesktop.ScreenSaver service")
        
        if 'powermanagement' not in registered_services:
            pm_interface = PMInhibitInterface()
            bus.export('/org/freedesktop/PowerManagement/Inhibit', pm_interface) # wiliwili
            await bus.request_name('org.freedesktop.PowerManagement')
            registered_services.add('powermanagement')
            decky_plugin.logger.info("Registered org.freedesktop.PowerManagement service")
        
        if 'gnome' not in registered_services:
            gnome_interface = GnomeInterface()
            bus.export('/org/gnome/SessionManager', gnome_interface) # mpv with https://github.com/Guldoman/mpv_inhibit_gnome installed
            await bus.request_name('org.gnome.SessionManager')
            registered_services.add('gnome')
            decky_plugin.logger.info("Registered org.gnome.SessionManager service")
    except Exception as e:
        decky_plugin.logger.info(f"Error registering services: {e}")

async def unregister_dbus_services():
    """Unregister D-Bus services when entering desktop mode"""
    global bus, registered_services
    try:
        if bus is None:
            return
        
        # Release all service names we own
        if 'screensaver' in registered_services:
            try:
                await bus.release_name('org.freedesktop.ScreenSaver')
                registered_services.discard('screensaver')
                decky_plugin.logger.info("Released org.freedesktop.ScreenSaver service")
            except Exception as e:
                decky_plugin.logger.info(f"Error releasing ScreenSaver: {e}")
        
        if 'powermanagement' in registered_services:
            try:
                await bus.release_name('org.freedesktop.PowerManagement')
                registered_services.discard('powermanagement')
                decky_plugin.logger.info("Released org.freedesktop.PowerManagement service")
            except Exception as e:
                decky_plugin.logger.info(f"Error releasing PowerManagement: {e}")
        
        if 'gnome' in registered_services:
            try:
                await bus.release_name('org.gnome.SessionManager')
                registered_services.discard('gnome')
                decky_plugin.logger.info("Released org.gnome.SessionManager service")
            except Exception as e:
                decky_plugin.logger.info(f"Error releasing SessionManager: {e}")
    except Exception as e:
        decky_plugin.logger.info(f"Error unregistering services: {e}")

async def check_and_manage_services():
    """
    Check if we need to register or unregister services based on desktop mode state.
    This should be called periodically to handle mode transitions.
    """
    global bus, last_desktop_mode
    
    if bus is None:
        return
    
    try:
        current_desktop_mode = await is_desktop_mode()
        
        # If mode changed, take action
        if last_desktop_mode != current_desktop_mode:
            if current_desktop_mode:
                # Switched to desktop mode - unregister our services
                decky_plugin.logger.info("Desktop mode detected, releasing D-Bus services")
                await unregister_dbus_services()
            else:
                # Switched to gaming mode - register our services
                decky_plugin.logger.info("Gaming mode detected, registering D-Bus services")
                await register_dbus_services()
            
            last_desktop_mode = current_desktop_mode
    except Exception as e:
        decky_plugin.logger.info(f"Error in check_and_manage_services: {e}")

async def setup_kde_service_monitoring():
    """
    Set up D-Bus signal monitoring to detect when KDE services start.
    This allows us to release our services BEFORE KDE tries to register conflicting ones.
    """
    global bus, name_owner_watch_rules
    
    if bus is None:
        return
    
    try:
        # KDE services to monitor
        kde_services = [
            'org.kde.plasmashell',
            'org.kde.KWin',
            'org.kde.Solid.PowerManagement'
        ]
        
        # Subscribe to NameOwnerChanged signals for each KDE service
        for service_name in kde_services:
            # Add match rule for this specific service
            rule = f"type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='{service_name}'"
            
            try:
                # Add the match rule
                msg = Message(
                    destination='org.freedesktop.DBus',
                    path='/org/freedesktop/DBus',
                    interface='org.freedesktop.DBus',
                    member='AddMatch',
                    signature='s',
                    body=[rule]
                )
                await bus.call(msg)
                name_owner_watch_rules.append(rule)
                decky_plugin.logger.info(f"Monitoring for {service_name} startup")
            except Exception as e:
                decky_plugin.logger.info(f"Error adding match rule for {service_name}: {e}")
        
        # Subscribe to the NameOwnerChanged signal
        bus.add_message_handler(handle_name_owner_changed_message)
        
    except Exception as e:
        decky_plugin.logger.info(f"Error setting up KDE monitoring: {e}")

def handle_name_owner_changed_message(msg):
    """Handle NameOwnerChanged D-Bus messages"""
    try:
        if (msg.message_type == MessageType.SIGNAL and
            msg.interface == 'org.freedesktop.DBus' and
            msg.member == 'NameOwnerChanged'):
            
            if msg.body and len(msg.body) >= 3:
                name = msg.body[0]
                old_owner = msg.body[1]
                new_owner = msg.body[2]
                handle_name_owner_changed(name, old_owner, new_owner)
    except Exception as e:
        decky_plugin.logger.info(f"Error handling NameOwnerChanged: {e}")

async def start_dbus():
    global bus, registered_services, last_desktop_mode, name_owner_watch_rules
    await stop_dbus()
    registered_services = set()
    last_desktop_mode = None
    name_owner_watch_rules = []
    try:
        bus = await MessageBus().connect()
        
        # Set up monitoring for KDE service startup BEFORE registering our services
        await setup_kde_service_monitoring()
        
        # Always register services at startup (system starts in gaming mode)
        await register_dbus_services()
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
        
        # Check and manage service registration based on desktop mode
        await check_and_manage_services()
        
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