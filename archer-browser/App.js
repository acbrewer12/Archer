import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  View, Text, TouchableOpacity, TextInput, Modal, StyleSheet,
  StatusBar, ScrollView, BackHandler, Keyboard, Dimensions, PanResponder,
} from 'react-native';
import { WebView } from 'react-native-webview';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { activateKeepAwake } from 'expo-keep-awake';
import * as NavigationBar from 'expo-navigation-bar';
import { useFonts, ShareTechMono_400Regular } from '@expo-google-fonts/share-tech-mono';

const STORAGE_KEY = 'archer_browser_v1';
const TAB_H       = 54;
const ADDR_H      = 50;

const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2);

const DEFAULT_TABS = [
  { id: uid(), name: 'ARCHER',    homeUrl: 'https://aydencatman-archer.hf.space/display' },
  { id: uid(), name: 'KHLOE',     homeUrl: 'https://aydencatman-archer.hf.space/passenger' },
  { id: uid(), name: 'SIMULATOR', homeUrl: 'https://aydencatman-archer.hf.space/simulator' },
  { id: uid(), name: 'VALET',     homeUrl: 'https://aydencatman-archer.hf.space/valet' },
  { id: uid(), name: 'FANS',      homeUrl: 'https://aydencatman-archer.hf.space/fans' },
  { id: uid(), name: 'GITHUB',    homeUrl: 'https://github.com/acbrewer12/Archer' },
  { id: uid(), name: 'CLAUDE',    homeUrl: 'https://claude.ai' },
];

export default function App() {
  const [fontsLoaded] = useFonts({ ShareTechMono_400Regular });

  const [tabs,    setTabs]    = useState(DEFAULT_TABS);
  const [active,  setActive]  = useState(0);
  const [showBar, setShowBar] = useState(false);
  const [barText, setBarText] = useState('');
  const [modal,   setModal]   = useState(null);
  const [mText,   setMText]   = useState('');
  const [ready,   setReady]   = useState(false);

  const wvRefs    = useRef({});
  const tabsRef   = useRef(tabs);
  const tabBarRef = useRef(null);

  useEffect(() => { tabsRef.current = tabs; }, [tabs]);

  useEffect(() => {
    activateKeepAwake();
    NavigationBar.setVisibilityAsync('hidden').catch(() => {});
    NavigationBar.setBehaviorAsync('overlay-swipe').catch(() => {});
    AsyncStorage.getItem(STORAGE_KEY).then(v => {
      if (v) {
        try {
          const s = JSON.parse(v);
          if (Array.isArray(s.tabs) && s.tabs.length) {
            setTabs(s.tabs);
            setActive(Math.min(s.active || 0, s.tabs.length - 1));
          }
        } catch (_) {}
      }
      setReady(true);
    });
  }, []);

  useEffect(() => {
    if (ready) AsyncStorage.setItem(STORAGE_KEY, JSON.stringify({ tabs, active }));
  }, [tabs, active, ready]);

  useEffect(() => {
    const sub = BackHandler.addEventListener('hardwareBackPress', () => {
      wvRefs.current[tabs[active]?.id]?.goBack();
      return true;
    });
    return () => sub.remove();
  }, [active, tabs]);

  useEffect(() => {
    tabBarRef.current?.scrollTo({ x: active * 84 - 20, animated: true });
  }, [active]);

  const openBar = useCallback(() => {
    const cur = tabsRef.current[active];
    setBarText(cur?.currentUrl || cur?.homeUrl || '');
    setShowBar(true);
  }, [active]);

  const navigate = useCallback(() => {
    let url = barText.trim();
    if (!url) return;
    if (!url.startsWith('http://') && !url.startsWith('https://')) url = 'https://' + url;
    const id = tabsRef.current[active]?.id;
    wvRefs.current[id]?.injectJavaScript(
      `window.location.href = "${url.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"; true;`
    );
    setTabs(ts => ts.map((t, i) => i === active ? { ...t, currentUrl: url } : t));
    setShowBar(false);
    Keyboard.dismiss();
  }, [barText, active]);

  const addTab = useCallback(() => {
    const t = { id: uid(), name: `TAB ${tabsRef.current.length + 1}`, homeUrl: 'https://google.com' };
    setTabs(ts => [...ts, t]);
    setActive(tabsRef.current.length);
  }, []);

  const deleteTab = useCallback((idx) => {
    if (tabsRef.current.length <= 1) return;
    setTabs(ts => ts.filter((_, i) => i !== idx));
    setActive(a => (a >= idx && a > 0 ? a - 1 : a));
    setModal(null);
  }, []);

  const saveRename = useCallback(() => {
    const idx = modal?.idx;
    if (idx == null) return;
    setTabs(ts => ts.map((t, i) => i === idx ? { ...t, name: mText.toUpperCase().slice(0, 14) } : t));
    setModal(null);
  }, [modal, mText]);

  const saveUrl = useCallback(() => {
    const idx = modal?.idx;
    if (idx == null) return;
    let url = mText.trim();
    if (!url.startsWith('http')) url = 'https://' + url;
    if (idx === active) {
      wvRefs.current[tabsRef.current[idx]?.id]?.injectJavaScript(
        `window.location.href = "${url.replace(/"/g, '\\"')}"; true;`
      );
    }
    setTabs(ts => ts.map((t, i) => i === idx ? { ...t, homeUrl: url, currentUrl: url } : t));
    setModal(null);
  }, [modal, mText, active]);

  const leftEdgePan = useRef(PanResponder.create({
    onStartShouldSetPanResponder:       () => true,
    onMoveShouldSetPanResponder: (_, g) => Math.abs(g.dx) > 10 && Math.abs(g.dy) < 35,
    onPanResponderRelease:       (_, g) => {
      if (g.dx < -50) setActive(a => Math.min(a + 1, tabsRef.current.length - 1));
    },
  })).current;

  const rightEdgePan = useRef(PanResponder.create({
    onStartShouldSetPanResponder:       () => true,
    onMoveShouldSetPanResponder: (_, g) => Math.abs(g.dx) > 10 && Math.abs(g.dy) < 35,
    onPanResponderRelease:       (_, g) => {
      if (g.dx > 50) setActive(a => Math.max(a - 1, 0));
    },
  })).current;

  const FONT = fontsLoaded ? 'ShareTechMono_400Regular' : 'monospace';

  if (!ready) return <View style={{ flex: 1, backgroundColor: '#000' }} />;

  return (
    <View style={s.root}>
      <StatusBar hidden />

      {tabs.map((tab, idx) => (
        <View key={tab.id} style={[s.webWrap, idx !== active && s.hidden]}>
          <WebView
            ref={r => { wvRefs.current[tab.id] = r; }}
            source={{ uri: tab.homeUrl }}
            style={{ flex: 1, backgroundColor: '#000' }}
            javaScriptEnabled
            domStorageEnabled
            allowsInlineMediaPlayback
            mediaPlaybackRequiresUserAction={false}
            allowsFullscreenVideo
            mixedContentMode="always"
            geolocationEnabled
            allowFileAccess
            onNavigationStateChange={state =>
              setTabs(ts => ts.map((t, i) =>
                i === idx ? { ...t, currentUrl: state.url } : t
              ))
            }
          />
          <View style={s.edgeL} {...rightEdgePan.panHandlers} />
          <View style={s.edgeR} {...leftEdgePan.panHandlers}  />
        </View>
      ))}

      <TouchableOpacity style={s.topHandle} onPress={openBar} activeOpacity={0.6}>
        <View style={s.handlePill} />
      </TouchableOpacity>

      {showBar && (
        <View style={s.addrWrap}>
          <TextInput
            style={[s.addrInput, { fontFamily: FONT }]}
            value={barText}
            onChangeText={setBarText}
            onSubmitEditing={navigate}
            autoFocus
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
            returnKeyType="go"
            selectTextOnFocus
            onBlur={() => setShowBar(false)}
            placeholderTextColor="#333"
            placeholder="URL or search…"
          />
          <TouchableOpacity onPress={navigate} style={s.goBtn}>
            <Text style={[s.goBtnTxt, { fontFamily: FONT }]}>GO</Text>
          </TouchableOpacity>
        </View>
      )}

      <View style={s.tabBar}>
        <ScrollView
          ref={tabBarRef}
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={s.tabBarInner}
          keyboardShouldPersistTaps="always"
        >
          {tabs.map((tab, idx) => (
            <TouchableOpacity
              key={tab.id}
              style={s.tabBtn}
              onPress={() => setActive(idx)}
              onLongPress={() => { setModal({ type: 'menu', idx }); setMText(''); }}
              delayLongPress={380}
            >
              <Text
                style={[s.tabTxt, { fontFamily: FONT, color: idx === active ? '#fff' : '#444' }]}
                numberOfLines={1}
              >
                {tab.name}
              </Text>
              {idx === active && <View style={s.activeDot} />}
            </TouchableOpacity>
          ))}
          <TouchableOpacity style={s.addBtn} onPress={addTab}>
            <Text style={[s.addTxt, { fontFamily: FONT }]}>+</Text>
          </TouchableOpacity>
        </ScrollView>
      </View>

      <Modal
        visible={!!modal}
        transparent
        animationType="fade"
        onRequestClose={() => setModal(null)}
      >
        <TouchableOpacity style={s.overlay} activeOpacity={1} onPress={() => setModal(null)}>
          <View style={s.modalBox} onStartShouldSetResponder={() => true}>

            {modal?.type === 'menu' && (
              <>
                <Text style={[s.modalTitle, { fontFamily: FONT }]}>
                  {tabs[modal.idx]?.name}
                </Text>
                <TouchableOpacity style={s.menuRow} onPress={() => {
                  setMText(tabs[modal.idx]?.name || '');
                  setModal({ ...modal, type: 'rename' });
                }}>
                  <Text style={[s.menuTxt, { fontFamily: FONT }]}>RENAME</Text>
                </TouchableOpacity>
                <TouchableOpacity style={s.menuRow} onPress={() => {
                  setMText(tabs[modal.idx]?.currentUrl || tabs[modal.idx]?.homeUrl || '');
                  setModal({ ...modal, type: 'url' });
                }}>
                  <Text style={[s.menuTxt, { fontFamily: FONT }]}>CHANGE URL</Text>
                </TouchableOpacity>
                {tabs.length > 1 && (
                  <TouchableOpacity style={[s.menuRow, { borderBottomWidth: 0 }]} onPress={() => deleteTab(modal.idx)}>
                    <Text style={[s.menuTxt, { fontFamily: FONT, color: '#cc0000' }]}>DELETE TAB</Text>
                  </TouchableOpacity>
                )}
              </>
            )}

            {modal?.type === 'rename' && (
              <>
                <Text style={[s.modalTitle, { fontFamily: FONT }]}>RENAME TAB</Text>
                <TextInput
                  style={[s.modalInput, { fontFamily: FONT }]}
                  value={mText}
                  onChangeText={setMText}
                  autoFocus
                  autoCapitalize="characters"
                  maxLength={14}
                  returnKeyType="done"
                  onSubmitEditing={saveRename}
                  placeholderTextColor="#333"
                  placeholder="TAB NAME"
                />
                <TouchableOpacity style={s.modalBtn} onPress={saveRename}>
                  <Text style={[s.modalBtnTxt, { fontFamily: FONT }]}>SAVE</Text>
                </TouchableOpacity>
              </>
            )}

            {modal?.type === 'url' && (
              <>
                <Text style={[s.modalTitle, { fontFamily: FONT }]}>CHANGE URL</Text>
                <TextInput
                  style={[s.modalInput, { fontFamily: FONT }]}
                  value={mText}
                  onChangeText={setMText}
                  autoFocus
                  autoCapitalize="none"
                  autoCorrect={false}
                  keyboardType="url"
                  returnKeyType="done"
                  onSubmitEditing={saveUrl}
                  placeholderTextColor="#333"
                  placeholder="https://"
                />
                <TouchableOpacity style={s.modalBtn} onPress={saveUrl}>
                  <Text style={[s.modalBtnTxt, { fontFamily: FONT }]}>SAVE</Text>
                </TouchableOpacity>
              </>
            )}

          </View>
        </TouchableOpacity>
      </Modal>
    </View>
  );
}

const s = StyleSheet.create({
  root:       { flex: 1, backgroundColor: '#000' },
  webWrap:    { position: 'absolute', top: 0, left: 0, right: 0, bottom: TAB_H },
  hidden:     { display: 'none' },
  edgeL:      { position: 'absolute', top: 0, left: 0,  width: 28, bottom: 0 },
  edgeR:      { position: 'absolute', top: 0, right: 0, width: 28, bottom: 0 },
  topHandle:  { position: 'absolute', top: 0, left: 0, right: 0, height: 22, zIndex: 20,
                justifyContent: 'center', alignItems: 'center' },
  handlePill: { width: 36, height: 3, borderRadius: 2, backgroundColor: '#cc0000', opacity: 0.45 },
  addrWrap:   { position: 'absolute', top: 22, left: 0, right: 0, height: ADDR_H,
                backgroundColor: '#0a0a0a', borderBottomWidth: 1, borderBottomColor: '#cc0000',
                flexDirection: 'row', alignItems: 'center', paddingHorizontal: 10, zIndex: 30 },
  addrInput:  { flex: 1, color: '#fff', fontSize: 13, backgroundColor: '#111',
                borderRadius: 4, paddingHorizontal: 10, paddingVertical: 7 },
  goBtn:      { paddingHorizontal: 12, paddingVertical: 10 },
  goBtnTxt:   { color: '#cc0000', fontSize: 11, letterSpacing: 2 },
  tabBar:     { position: 'absolute', bottom: 0, left: 0, right: 0, height: TAB_H,
                backgroundColor: '#0a0a0a', borderTopWidth: 1, borderTopColor: '#cc0000', zIndex: 20 },
  tabBarInner:{ alignItems: 'center', paddingHorizontal: 2 },
  tabBtn:     { height: TAB_H, paddingHorizontal: 14, minWidth: 72,
                justifyContent: 'center', alignItems: 'center' },
  tabTxt:     { fontSize: 10, letterSpacing: 1.5 },
  activeDot:  { position: 'absolute', bottom: 0, left: 14, right: 14, height: 2, backgroundColor: '#cc0000' },
  addBtn:     { height: TAB_H, paddingHorizontal: 14, justifyContent: 'center', alignItems: 'center' },
  addTxt:     { color: '#cc0000', fontSize: 24, lineHeight: 26 },
  overlay:    { flex: 1, backgroundColor: 'rgba(0,0,0,0.75)', justifyContent: 'center', alignItems: 'center' },
  modalBox:   { width: 290, backgroundColor: '#0a0a0a', borderWidth: 1, borderColor: '#cc0000',
                borderRadius: 6, padding: 20 },
  modalTitle: { color: '#cc0000', fontSize: 12, letterSpacing: 3, marginBottom: 18, textAlign: 'center' },
  menuRow:    { paddingVertical: 15, borderBottomWidth: 1, borderBottomColor: '#1a1a1a' },
  menuTxt:    { color: '#fff', fontSize: 11, letterSpacing: 3, textAlign: 'center' },
  modalInput: { backgroundColor: '#111', color: '#fff', borderWidth: 1, borderColor: '#222',
                borderRadius: 4, paddingHorizontal: 12, paddingVertical: 9,
                fontSize: 13, marginBottom: 14 },
  modalBtn:   { backgroundColor: '#1a0000', borderWidth: 1, borderColor: '#cc0000',
                borderRadius: 4, padding: 12, alignItems: 'center' },
  modalBtnTxt:{ color: '#cc0000', fontSize: 11, letterSpacing: 3 },
});
