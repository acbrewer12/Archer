/**
 * Archer App — connects to your Archer Flask server on the truck WiFi.
 * Built for Ayden, 13, Salem MO.
 *
 * Features:
 *  - Connection status bar (green/red + tier badge)
 *  - Offline overlay with retry (after 3 missed pings)
 *  - Archer message notifications when backgrounded
 *  - WebView with keep-awake and back button support
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  View, Text, TextInput, TouchableOpacity, StyleSheet,
  ActivityIndicator, Alert, BackHandler,
  KeyboardAvoidingView, Platform, AppState,
} from 'react-native';
import { WebView } from 'react-native-webview';
import * as SecureStore from 'expo-secure-store';
import { activateKeepAwakeAsync, deactivateKeepAwake } from 'expo-keep-awake';
import { StatusBar as ExpoStatusBar } from 'expo-status-bar';
import * as Notifications from 'expo-notifications';

const DEFAULT_PORT = '7860';
const STORE_KEY    = 'archer_server_ip';
const POLL_MS      = 5000;
const FAIL_THRESH  = 3;

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: false,
    shouldSetBadge:  false,
  }),
});

// ── Helpers ──────────────────────────────────────────────
function buildUrl(ip) {
  if (!ip) return null;
  if (ip.startsWith('http')) return ip.replace(/\/$/, '');
  const [host, port] = ip.split(':');
  return `http://${host}:${port || DEFAULT_PORT}`;
}

async function pingServer(baseUrl) {
  try {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), 3000);
    const r = await fetch(`${baseUrl}/sim/status`, { signal: controller.signal });
    clearTimeout(id);
    if (!r.ok) return { ok: false, tier: null };
    const d = await r.json();
    return { ok: true, tier: d.tier ?? null };
  } catch {
    return { ok: false, tier: null };
  }
}

async function fetchArcherMsg(baseUrl) {
  try {
    const r = await fetch(`${baseUrl}/display_data`);
    if (!r.ok) return null;
    const d = await r.json();
    return d.archer_msg || null;
  } catch {
    return null;
  }
}

// ── Connection Monitor Hook ───────────────────────────────
function useConnectionMonitor(serverUrl) {
  const [isConnected, setIsConnected]       = useState(true);
  const [tier, setTier]                     = useState(null);
  const [consecutiveFails, setConsecFails]  = useState(0);
  const failsRef   = useRef(0);
  const lastMsgRef = useRef('');
  const appState   = useRef(AppState.currentState);

  useEffect(() => {
    const sub = AppState.addEventListener('change', s => { appState.current = s; });
    return () => sub.remove();
  }, []);

  useEffect(() => {
    if (!serverUrl) return;
    let active = true;

    async function poll() {
      if (!active) return;
      const { ok, tier: t } = await pingServer(serverUrl);
      if (!active) return;

      if (ok) {
        failsRef.current = 0;
        setConsecFails(0);
        setIsConnected(true);
        if (t !== null) setTier(t);

        // Check archer message for background notifications
        const msg = await fetchArcherMsg(serverUrl);
        if (msg && msg !== lastMsgRef.current) {
          if (appState.current !== 'active') {
            await Notifications.scheduleNotificationAsync({
              content: { title: 'ARCHER', body: msg },
              trigger: null,
            }).catch(() => {});
          }
          lastMsgRef.current = msg;
        }
      } else {
        failsRef.current += 1;
        setConsecFails(failsRef.current);
        if (failsRef.current >= FAIL_THRESH) setIsConnected(false);
      }
    }

    poll();
    const id = setInterval(poll, POLL_MS);
    return () => { active = false; clearInterval(id); };
  }, [serverUrl]);

  return { isConnected, tier, consecutiveFails };
}

// ── Notification Permission ───────────────────────────────
async function requestNotifPermission() {
  const { status } = await Notifications.requestPermissionsAsync();
  return status === 'granted';
}

// ── Connection Bar ────────────────────────────────────────
function ConnectionBar({ isConnected, tier }) {
  const tierLabel = tier ? `T${tier}` : '—';
  return (
    <View style={[s.connBar, isConnected ? s.connBarOn : s.connBarOff]}>
      <View style={[s.connDot, { backgroundColor: isConnected ? '#00cc44' : '#cc0000' }]} />
      <Text style={s.connTxt}>
        {isConnected ? 'ARCHER CONNECTED' : 'NO CONNECTION'}
      </Text>
      <Text style={s.connTier}>{tierLabel}</Text>
    </View>
  );
}

// ── Offline Overlay ───────────────────────────────────────
function OfflineOverlay({ serverUrl, onRetry }) {
  const [retrying, setRetrying] = useState(false);

  async function retry() {
    setRetrying(true);
    const { ok } = await pingServer(serverUrl);
    setRetrying(false);
    if (ok) onRetry();
  }

  return (
    <View style={s.offlineWrap}>
      <View style={s.offlineBox}>
        <Text style={s.offlineTitleTxt}>ARCHER OFFLINE</Text>
        <Text style={s.offlineBodyTxt}>Can't reach {serverUrl}</Text>
        <Text style={s.offlineBodyTxt}>Make sure you're on the same WiFi.</Text>
        <TouchableOpacity style={s.retryBtn} onPress={retry} disabled={retrying}>
          {retrying
            ? <ActivityIndicator color="#cc0000" />
            : <Text style={s.retryBtnTxt}>RETRY</Text>}
        </TouchableOpacity>
      </View>
    </View>
  );
}

// ── Setup Screen ─────────────────────────────────────────
function SetupScreen({ onConnect }) {
  const [ip, setIp]           = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState('');

  async function connect() {
    const trimmed = ip.trim();
    if (!trimmed) { setError('Enter your server IP or URL'); return; }
    setLoading(true);
    setError('');
    const url = buildUrl(trimmed);
    const { ok } = await pingServer(url);
    setLoading(false);
    if (ok) {
      await SecureStore.setItemAsync(STORE_KEY, trimmed);
      onConnect(url);
    } else {
      setError(`Can't reach ${url}\nMake sure you're on the same WiFi as the server.`);
    }
  }

  return (
    <KeyboardAvoidingView style={s.setup} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ExpoStatusBar style="light" backgroundColor="#000" />
      <Text style={s.archerLogo}>ARCHER</Text>
      <Text style={s.archerSub}>2006 GMC SIERRA 2500HD</Text>
      <Text style={s.archerSub2}>Built by Ayden · Salem MO</Text>

      <View style={s.inputWrap}>
        <Text style={s.inputLbl}>SERVER IP OR ADDRESS</Text>
        <TextInput
          style={s.input}
          value={ip}
          onChangeText={t => { setIp(t); setError(''); }}
          placeholder="192.168.4.1  or  127.0.0.1:7860"
          placeholderTextColor="#333"
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          returnKeyType="go"
          onSubmitEditing={connect}
        />
        {!!error && <Text style={s.errorTxt}>{error}</Text>}
      </View>

      <TouchableOpacity style={s.connectBtn} onPress={connect} disabled={loading}>
        {loading
          ? <ActivityIndicator color="#cc0000" />
          : <Text style={s.connectBtnTxt}>CONNECT</Text>}
      </TouchableOpacity>

      <Text style={s.hint}>
        On truck WiFi: connect to ARCHER-2500HD hotspot, then use 192.168.4.1{'\n'}
        Local testing: use 127.0.0.1:7860
      </Text>
    </KeyboardAvoidingView>
  );
}

// ── Main App ─────────────────────────────────────────────
export default function App() {
  const [serverUrl, setServerUrl] = useState(null);
  const [loading, setLoading]     = useState(true);
  const webRef    = useRef(null);
  const [canGoBack, setCanGoBack] = useState(false);

  const { isConnected, tier, consecutiveFails } = useConnectionMonitor(serverUrl);
  const showOffline = serverUrl && consecutiveFails >= FAIL_THRESH;

  // Request notification permission once
  useEffect(() => { requestNotifPermission(); }, []);

  // Keep screen on while connected
  useEffect(() => {
    if (serverUrl) activateKeepAwakeAsync();
    else           deactivateKeepAwake();
  }, [serverUrl]);

  // Load saved IP on startup
  useEffect(() => {
    SecureStore.getItemAsync(STORE_KEY).then(async saved => {
      if (saved) {
        const url = buildUrl(saved);
        const { ok } = await pingServer(url);
        if (ok) setServerUrl(url);
      }
      setLoading(false);
    });
  }, []);

  // Hardware back button
  useEffect(() => {
    const handler = BackHandler.addEventListener('hardwareBackPress', () => {
      if (canGoBack && webRef.current) { webRef.current.goBack(); return true; }
      if (serverUrl) {
        Alert.alert('Disconnect?', 'Return to server setup?', [
          { text: 'Cancel', style: 'cancel' },
          { text: 'Disconnect', onPress: () => setServerUrl(null) },
        ]);
        return true;
      }
      return false;
    });
    return () => handler.remove();
  }, [canGoBack, serverUrl]);

  const handleRetry = useCallback(() => {
    // Forcing a re-ping is handled by the monitor — just reset visual state
    setServerUrl(prev => prev);
  }, []);

  if (loading) {
    return (
      <View style={s.splash}>
        <ExpoStatusBar style="light" backgroundColor="#000" />
        <Text style={s.archerLogo}>ARCHER</Text>
        <ActivityIndicator color="#cc0000" style={{ marginTop: 24 }} />
      </View>
    );
  }

  if (!serverUrl) return <SetupScreen onConnect={setServerUrl} />;

  const injected = `
    (function() {
      document.body.style.overscrollBehavior = 'none';
      window.ARCHER_APP    = true;
      window.ARCHER_SERVER = '${serverUrl}';
    })();
    true;
  `;

  return (
    <View style={s.root}>
      <ExpoStatusBar style="light" backgroundColor="#000" />
      <ConnectionBar isConnected={isConnected} tier={tier} />
      <View style={s.webWrap}>
        <WebView
          ref={webRef}
          source={{ uri: serverUrl }}
          style={s.web}
          injectedJavaScriptBeforeContentLoaded={injected}
          javaScriptEnabled
          domStorageEnabled
          allowsInlineMediaPlayback
          mediaPlaybackRequiresUserAction={false}
          onNavigationStateChange={nav => setCanGoBack(nav.canGoBack)}
          onError={({ nativeEvent }) => console.warn('WebView error:', nativeEvent.description)}
          renderLoading={() => (
            <View style={s.webLoading}>
              <Text style={s.archerLogo}>ARCHER</Text>
              <ActivityIndicator color="#cc0000" style={{ marginTop: 16 }} />
              <Text style={s.connectingTxt}>Connecting to {serverUrl}…</Text>
            </View>
          )}
          startInLoadingState
        />
        {showOffline && (
          <OfflineOverlay serverUrl={serverUrl} onRetry={handleRetry} />
        )}
      </View>
    </View>
  );
}

// ── Styles ────────────────────────────────────────────────
const RED = '#cc0000';
const BG  = '#000000';
const BG2 = '#0a0a0a';

const s = StyleSheet.create({
  root:    { flex: 1, backgroundColor: BG },
  webWrap: { flex: 1, position: 'relative' },
  web:     { flex: 1, backgroundColor: BG },
  splash:  { flex: 1, backgroundColor: BG, alignItems: 'center', justifyContent: 'center' },

  // Connection bar
  connBar: {
    flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12,
    height: 26, borderBottomWidth: 1,
  },
  connBarOn:  { backgroundColor: '#030a03', borderBottomColor: '#003311' },
  connBarOff: { backgroundColor: '#0a0000', borderBottomColor: '#330000' },
  connDot:  { width: 7, height: 7, borderRadius: 4, marginRight: 7 },
  connTxt:  { flex: 1, fontSize: 8, letterSpacing: 2, color: '#555' },
  connTier: { fontSize: 9, letterSpacing: 2, color: '#333' },

  // Offline overlay
  offlineWrap: {
    ...StyleSheet.absoluteFillObject, backgroundColor: 'rgba(0,0,0,0.88)',
    alignItems: 'center', justifyContent: 'center', zIndex: 99,
  },
  offlineBox: {
    backgroundColor: '#0a0000', borderWidth: 1, borderColor: RED,
    borderRadius: 6, padding: 28, alignItems: 'center', width: '80%',
  },
  offlineTitleTxt: { color: RED, fontSize: 16, letterSpacing: 4, fontWeight: '700', marginBottom: 12 },
  offlineBodyTxt:  { color: '#444', fontSize: 10, letterSpacing: 1, marginBottom: 4, textAlign: 'center' },
  retryBtn: {
    marginTop: 20, borderWidth: 1, borderColor: RED, borderRadius: 4,
    paddingVertical: 10, paddingHorizontal: 32,
  },
  retryBtnTxt: { color: RED, fontSize: 11, letterSpacing: 4 },

  // Setup screen
  setup: { flex: 1, backgroundColor: BG, alignItems: 'center', justifyContent: 'center', padding: 28 },
  archerLogo: {
    fontFamily: Platform.OS === 'android' ? 'monospace' : 'Courier New',
    fontSize: 36, fontWeight: '900', letterSpacing: 10, color: RED, marginBottom: 4,
  },
  archerSub:  { fontSize: 10, letterSpacing: 4, color: '#444', marginBottom: 2 },
  archerSub2: { fontSize: 9,  letterSpacing: 3, color: '#222', marginBottom: 48 },
  inputWrap:  { width: '100%', marginBottom: 20 },
  inputLbl:   { fontSize: 8, letterSpacing: 3, color: '#333', marginBottom: 6 },
  input: {
    backgroundColor: BG2, borderWidth: 1, borderColor: '#1a1a1a', borderRadius: 4,
    color: '#cc0000', padding: 12, fontSize: 14,
    fontFamily: Platform.OS === 'android' ? 'monospace' : 'Courier New',
    letterSpacing: 1,
  },
  errorTxt:      { color: RED, fontSize: 10, marginTop: 8, letterSpacing: 0.5 },
  connectBtn: {
    width: '100%', backgroundColor: '#100000', borderWidth: 1, borderColor: RED,
    borderRadius: 4, padding: 14, alignItems: 'center', marginBottom: 28,
  },
  connectBtnTxt: { color: RED, fontSize: 11, letterSpacing: 4, fontWeight: '700' },
  hint:          { fontSize: 9, color: '#2a2a2a', textAlign: 'center', letterSpacing: 0.5, lineHeight: 16 },
  webLoading: {
    position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: BG,
    alignItems: 'center', justifyContent: 'center',
  },
  connectingTxt: { color: '#333', fontSize: 10, marginTop: 12, letterSpacing: 2 },
});
