/**
 * Archer App — connects to your Archer Flask server on the truck WiFi.
 * Built for Ayden, 13, Salem MO.
 *
 * First launch: enter your server IP (e.g. 192.168.4.1 when on truck hotspot,
 * or 127.0.0.1:7860 for local testing).
 * The app remembers your IP and loads the right tier UI automatically.
 */

import React, { useState, useEffect, useRef } from 'react';
import {
  View, Text, TextInput, TouchableOpacity, StyleSheet,
  StatusBar, ActivityIndicator, Alert, BackHandler,
  KeyboardAvoidingView, Platform,
} from 'react-native';
import { WebView } from 'react-native-webview';
import * as SecureStore from 'expo-secure-store';
import { activateKeepAwakeAsync, deactivateKeepAwake } from 'expo-keep-awake';
import { StatusBar as ExpoStatusBar } from 'expo-status-bar';

const DEFAULT_PORT = '7860';
const STORE_KEY    = 'archer_server_ip';

// ── Helpers ──────────────────────────────────────────────
function buildUrl(ip) {
  // Accept raw IP, IP:port, or full http://... URL
  if (!ip) return null;
  if (ip.startsWith('http')) return ip.replace(/\/$/, '');
  const [host, port] = ip.split(':');
  return `http://${host}:${port || DEFAULT_PORT}`;
}

function pingServer(baseUrl) {
  return fetch(`${baseUrl}/sim/status`, { timeout: 3000 })
    .then(r => r.ok)
    .catch(() => false);
}

// ── Setup Screen ─────────────────────────────────────────
function SetupScreen({ onConnect }) {
  const [ip, setIp]       = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError]   = useState('');

  async function connect() {
    const trimmed = ip.trim();
    if (!trimmed) { setError('Enter your server IP or URL'); return; }
    setLoading(true);
    setError('');
    const url = buildUrl(trimmed);
    const ok  = await pingServer(url);
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
      <StatusBar barStyle="light-content" backgroundColor="#000" />
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
          : <Text style={s.connectBtnTxt}>CONNECT</Text>
        }
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
  const webRef = useRef(null);
  const [canGoBack, setCanGoBack] = useState(false);

  // Keep screen on while connected
  useEffect(() => {
    if (serverUrl) activateKeepAwakeAsync();
    else           deactivateKeepAwake();
  }, [serverUrl]);

  // Load saved IP on startup
  useEffect(() => {
    SecureStore.getItemAsync(STORE_KEY).then(saved => {
      if (saved) {
        const url = buildUrl(saved);
        pingServer(url).then(ok => {
          if (ok) setServerUrl(url);
          setLoading(false);
        });
      } else {
        setLoading(false);
      }
    });
  }, []);

  // Hardware back button — go back in WebView or exit
  useEffect(() => {
    const handler = BackHandler.addEventListener('hardwareBackPress', () => {
      if (canGoBack && webRef.current) {
        webRef.current.goBack();
        return true;
      }
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

  if (loading) {
    return (
      <View style={s.splash}>
        <Text style={s.archerLogo}>ARCHER</Text>
        <ActivityIndicator color="#cc0000" style={{ marginTop: 24 }} />
      </View>
    );
  }

  if (!serverUrl) {
    return <SetupScreen onConnect={setServerUrl} />;
  }

  // Injected JS — tells the WebView to suppress default browser chrome,
  // and lets the app know when navigation changes.
  const injected = `
    (function() {
      // Prevent pull-to-refresh
      document.body.style.overscrollBehavior = 'none';
      // Mark as running inside Archer app
      window.ARCHER_APP = true;
      window.ARCHER_SERVER = '${serverUrl}';
    })();
    true;
  `;

  return (
    <View style={s.root}>
      <ExpoStatusBar style="light" backgroundColor="#000" />
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
        onError={({ nativeEvent }) => {
          console.warn('WebView error:', nativeEvent.description);
        }}
        renderLoading={() => (
          <View style={s.webLoading}>
            <Text style={s.archerLogo}>ARCHER</Text>
            <ActivityIndicator color="#cc0000" style={{ marginTop: 16 }} />
            <Text style={s.connectingTxt}>Connecting to {serverUrl}…</Text>
          </View>
        )}
        startInLoadingState
      />
    </View>
  );
}

// ── Styles ────────────────────────────────────────────────
const RED = '#cc0000';
const BG  = '#000000';
const BG2 = '#0a0a0a';

const s = StyleSheet.create({
  root:         { flex: 1, backgroundColor: BG },
  web:          { flex: 1, backgroundColor: BG },
  splash:       { flex: 1, backgroundColor: BG, alignItems: 'center', justifyContent: 'center' },
  setup: {
    flex: 1, backgroundColor: BG, alignItems: 'center', justifyContent: 'center', padding: 28,
  },
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
    color: '#cc0000', padding: 12, fontSize: 14, fontFamily: Platform.OS === 'android' ? 'monospace' : 'Courier New',
    letterSpacing: 1,
  },
  errorTxt: { color: RED, fontSize: 10, marginTop: 8, letterSpacing: 0.5 },
  connectBtn: {
    width: '100%', backgroundColor: '#100000', borderWidth: 1, borderColor: RED,
    borderRadius: 4, padding: 14, alignItems: 'center', marginBottom: 28,
  },
  connectBtnTxt: { color: RED, fontSize: 11, letterSpacing: 4, fontWeight: '700' },
  hint: { fontSize: 9, color: '#2a2a2a', textAlign: 'center', letterSpacing: 0.5, lineHeight: 16 },
  webLoading: {
    position: 'absolute', inset: 0, backgroundColor: BG,
    alignItems: 'center', justifyContent: 'center', flex: 1,
  },
  connectingTxt: { color: '#333', fontSize: 10, marginTop: 12, letterSpacing: 2 },
});
