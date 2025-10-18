# Decky Screen Saver

[中文说明](./README_ZH.md)

This is a plugin for Decky Loader (A plugin loader for the Steam Deck), it will automatically inhibit screensaver during video playback under SteamOS game mode.

### How to install

1. Install Decky Loader: https://decky.xyz
2. Download `ScreenSaver.zip` from: https://github.com/xfangfang/DeckyInhibitScreenSaver/releases
3. Unzip `ScreenSaver.zip` to the `/home/deck/homebrew/plugins` directory and restart Steam

[Welcome to buy me a cup of coffee](https://www.paypal.me/xfangfang)

### How does this plugin work

In SteamDeck game mode, when using the browser or video player, SteamDeck will automatically suspend in a few minutes. You need to manually modify the relevant system settings to prevent this behavior.

This plugin registers and monitors the missing D-Bus services in game mode, automatically preventing the system from suspending when receiving a request from a application. And restore the default settings when the application closes or cancels the request (dimming: 5 minutes, suspending: 10 minutes)

**Desktop Mode Compatibility:** The plugin registers D-Bus services with the `ALLOW_REPLACEMENT` flag, which allows KDE to seamlessly take over the service names when switching to desktop mode. This flag tells D-Bus that other applications (like KDE Plasma) can replace our ownership of these services without conflicts. When switching back to gaming mode and KDE releases the names, the plugin automatically re-registers them. This provides a clean, conflict-free transition between modes.


### Compatible application
- [x] VLC
- [x] Chrome
- [x] mpv (Works out-of-the-box with the Flathub build; all other packages require [mpv_inhibit_gnome](https://github.com/Guldoman/mpv_inhibit_gnome))