# Archer Browser — Handoff

This folder contains the complete source for the `archer-browser` React Native app.

## What it is
A private Android browser built with Expo 52 / React Native, sideloaded APK only.
Designed for Samsung Galaxy S24 Ultra. Full immersive (no status/nav bar), keep-awake.

## Where it lives
Target repo: `acbrewer12/archer-browser` (branch: `main`)

## How to push
In a session with `acbrewer12/archer-browser` connected, push all files in this
folder (except this HANDOFF.md) to the root of `acbrewer12/archer-browser` on `main`.

## Files to push
- `App.js` — full app (tabs, WebViews, address bar, swipe, modal, AsyncStorage)
- `package.json` — Expo 52 dependencies
- `app.json` — package com.ayden.archerbrowser, owner aydencatman
- `eas.json` — EAS build profile: preview → APK
- `babel.config.js` — babel-preset-expo
- `.gitignore`

## Build command (after npm install)
```
eas build --platform android --profile preview
```

## Default tabs
- ARCHER    → https://aydencatman-archer.hf.space/display
- KHLOE     → https://aydencatman-archer.hf.space/passenger
- SIMULATOR → https://aydencatman-archer.hf.space/simulator
- VALET     → https://aydencatman-archer.hf.space/valet
- FANS      → https://aydencatman-archer.hf.space/fans
- GITHUB    → https://github.com/acbrewer12/Archer
- CLAUDE    → https://claude.ai

## Key design decisions
- All WebViews always mounted; inactive ones use `display: 'none'` to preserve state
- `homeUrl` = WebView source (only changes on explicit nav)
- `currentUrl` = tracked via onNavigationStateChange for address bar display
- Address bar navigation uses `injectJavaScript('window.location.href = url; true;')` — no component remount
- Edge PanResponders (28px strips left/right) for swipe-to-switch-tab
- BackHandler navigates WebView back history
- NavigationBar hidden + overlay-swipe for full immersive
- AsyncStorage key: `archer_browser_v1`
- Font: ShareTechMono_400Regular
- Color scheme: black bg, #cc0000 accent
