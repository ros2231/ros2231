# MintLink

Use your Android phone as a touchpad, keyboard, volume/media remote, clipboard bridge and
"find my device" button for a Linux Mint PC, over Bluetooth LE.

## On the PC (Linux Mint / Cinnamon, X11)

    sudo apt install xdotool xclip pulseaudio-utils python3-dbus python3-gi bluez ffmpeg openssl scrcpy adb
    python3 mintlink.py            # run as your normal user, inside your desktop session

It prints a 6-digit PIN (or pass `--pin 123456`). Type `r` + Enter in that terminal to ring the phone.

For an app-menu/desktop launcher, run `bash install-launcher.sh` once (opens a terminal window showing the PIN).

## On the phone

Web Bluetooth only works on a secure (HTTPS) page in Chrome on Android, so host the web files
(`index.html`, `manifest.webmanifest`, `sw.js`, both icons) on any HTTPS static host
(GitHub Pages, Netlify, Cloudflare Pages...). Open it, tap Connect, choose "MintLink",
enter the PIN. Use Chrome menu > Add to Home screen to install it.

## Camera share (Bluetooth only)

Open the **Share** tab on the phone and tap **Start** under "Share this phone's camera". Allow the camera
permission; a MintLink window opens on the PC showing live pictures from the phone (**Flip** switches between
the back and front camera, **Stop** or closing the window ends it). It needs only the Bluetooth link: no
internet, USB or Wi-Fi.

Expect a slideshow, not smooth video: the picture is a small JPEG sent one frame at a time over Bluetooth,
so roughly 1-4 frames per second. The app adapts size and quality to the link speed. Keep the phone screen
on (it asks Chrome to stay awake). The window needs GTK, which Mint already has
(`sudo apt install gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0` if not).

## Show your phone's screen on the PC (no internet needed)

A web page can't capture an Android screen, and Bluetooth is too slow for video, so this uses a USB cable
and `scrcpy`. Nothing is installed on the phone and no network of any kind is needed.

1. One-time, on the phone: Settings > About phone > tap *Build number* 7 times, then
   Settings > Developer options > turn on **USB debugging**.
2. Plug the phone into the PC and tap **Allow** on the "Allow USB debugging?" prompt (tick "Always allow").
3. Start it any of these ways: the **MintLink Phone Screen** launcher, `python3 mintlink.py --mirror`
   (no Bluetooth needed), `p` + Enter in the running MintLink terminal, or **Show on PC** in the app's
   Screen tab. A window with your phone's screen opens, and you can click/type in it to control the phone.
   For view-only: `--scrcpy-args '--no-control'`.

Wireless without internet also works on Android 11+ (Developer options > Wireless debugging) if the
phone and PC share any Wi-Fi, even a hotspot with no internet: `adb pair IP:PORT`, then `adb connect IP:PORT`.

## View the PC screen on the phone (Wi-Fi)

Open the **Screen** tab and tap Start. Bluetooth only carries the handshake; the picture is an HTTPS
stream over your Wi-Fi, so the phone and PC must be on the same network. The first time (and whenever
the PC's IP address changes) Chrome may not load the picture until you tap **Open in browser** once and
accept the certificate warning (the PC uses its own self-signed certificate). After that, Start works
inline. Tap the picture for fullscreen. The stream is view-only; use the Pad tab to control the pointer.
ffmpeg only runs while the phone is watching. Options: `--screen-fps 10 --screen-width 1280 --screen-port 8443`.
If you use a firewall (ufw), allow the port: `sudo ufw allow 8443/tcp`.

## Notes

- X11 only: xdotool and screen capture do not work on Wayland sessions.
- The PIN gates commands, but the BLE link itself is not encrypted, so only use it where you
  are comfortable with nearby devices being able to sniff traffic (typed text included).
- 5 wrong PINs locks that connection out for 30 seconds.
