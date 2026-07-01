package com.archer.truck;

import android.Manifest;
import android.app.Activity;
import android.content.ContentResolver;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraManager;
import android.media.AudioManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.AlarmClock;
import android.provider.ContactsContract;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.speech.tts.Voice;
import android.view.View;
import android.view.animation.AlphaAnimation;
import android.view.animation.Animation;
import android.view.animation.AnimationSet;
import android.view.animation.ScaleAnimation;
import android.webkit.CookieManager;
import android.widget.TextView;
import org.json.JSONObject;
import java.io.*;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.Locale;
import java.util.Set;

public class VoiceActivity extends Activity implements TextToSpeech.OnInitListener {

    private SpeechRecognizer recognizer;
    private TextToSpeech tts;
    private boolean ttsReady = false;
    private boolean listening = false;

    private TextView statusText, transcriptText, responseText;
    private View ring1, ring2, micBtn;

    private static final int MIC_PERMISSION     = 101;
    private static final int CONTACTS_PERMISSION = 102;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_voice);

        statusText     = findViewById(R.id.status_text);
        transcriptText = findViewById(R.id.transcript_text);
        responseText   = findViewById(R.id.response_text);
        ring1          = findViewById(R.id.ring1);
        ring2          = findViewById(R.id.ring2);
        micBtn         = findViewById(R.id.mic_btn);

        tts = new TextToSpeech(this, this);

        micBtn.setOnClickListener(v -> {
            if (!listening) startListening();
            else stopListening();
        });
        findViewById(R.id.open_btn).setOnClickListener(v -> {
            startActivity(new Intent(this, MainActivity.class));
            finish();
        });
        findViewById(R.id.dismiss_btn).setOnClickListener(v -> finish());

        // Request mic permission then start
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M &&
                checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                        != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MIC_PERMISSION);
        } else {
            initRecognizer();
            new Handler(Looper.getMainLooper()).postDelayed(this::startListening, 400);
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (code == MIC_PERMISSION && results.length > 0
                && results[0] == PackageManager.PERMISSION_GRANTED) {
            initRecognizer();
            new Handler(Looper.getMainLooper()).postDelayed(this::startListening, 400);
        } else if (code == MIC_PERMISSION) {
            statusText.setText("Microphone permission needed");
        }
    }

    private void initRecognizer() {
        recognizer = SpeechRecognizer.createSpeechRecognizer(this);
        recognizer.setRecognitionListener(new RecognitionListener() {
            @Override
            public void onReadyForSpeech(Bundle p) {
                runOnUiThread(() -> setListeningState(true));
            }
            @Override
            public void onPartialResults(Bundle b) {
                ArrayList<String> m = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
                if (m != null && !m.isEmpty())
                    runOnUiThread(() -> transcriptText.setText(m.get(0)));
            }
            @Override
            public void onResults(Bundle b) {
                ArrayList<String> m = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
                if (m != null && !m.isEmpty()) {
                    String heard = m.get(0);
                    runOnUiThread(() -> {
                        transcriptText.setText(heard);
                        setListeningState(false);
                        statusText.setText("Processing...");
                    });
                    sendToArcher(heard);
                } else {
                    runOnUiThread(() -> { setListeningState(false); statusText.setText("Didn't catch that — tap mic"); });
                }
            }
            @Override
            public void onError(int e) {
                runOnUiThread(() -> { setListeningState(false); statusText.setText("Tap mic to try again"); });
            }
            @Override public void onBeginningOfSpeech() {}
            @Override public void onRmsChanged(float r) {}
            @Override public void onBufferReceived(byte[] b) {}
            @Override public void onEndOfSpeech() {
                runOnUiThread(() -> statusText.setText("Processing..."));
            }
            @Override public void onEvent(int t, Bundle b) {}
        });
    }

    private void startListening() {
        if (recognizer == null) return;
        transcriptText.setText("");
        responseText.setText("");
        statusText.setText("Listening...");

        Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault());
        i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        i.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1);
        recognizer.startListening(i);
        listening = true;
    }

    private void stopListening() {
        if (recognizer != null) recognizer.stopListening();
        setListeningState(false);
        statusText.setText("Tap mic to speak");
        listening = false;
    }

    private void setListeningState(boolean on) {
        listening = on;
        if (on) {
            startPulse();
            micBtn.setAlpha(1f);
        } else {
            stopPulse();
            micBtn.setAlpha(0.6f);
        }
    }

    private void startPulse() {
        ring1.setVisibility(View.VISIBLE);
        ring2.setVisibility(View.VISIBLE);
        ring1.startAnimation(pulse(1.0f, 1.6f, 900));
        ring2.startAnimation(pulse(1.0f, 2.2f, 1300));
    }

    private void stopPulse() {
        ring1.clearAnimation(); ring1.setVisibility(View.INVISIBLE);
        ring2.clearAnimation(); ring2.setVisibility(View.INVISIBLE);
    }

    private AnimationSet pulse(float from, float to, long dur) {
        ScaleAnimation scale = new ScaleAnimation(from, to, from, to,
            Animation.RELATIVE_TO_SELF, 0.5f, Animation.RELATIVE_TO_SELF, 0.5f);
        scale.setDuration(dur);
        scale.setRepeatMode(Animation.REVERSE);
        scale.setRepeatCount(Animation.INFINITE);

        AlphaAnimation fade = new AlphaAnimation(0.5f, 0f);
        fade.setDuration(dur);
        fade.setRepeatMode(Animation.REVERSE);
        fade.setRepeatCount(Animation.INFINITE);

        AnimationSet set = new AnimationSet(true);
        set.addAnimation(scale);
        set.addAnimation(fade);
        return set;
    }

    private void sendToArcher(String command) {
        new Thread(() -> {
            try {
                String lower = command.toLowerCase(Locale.US);
                if (handleNativeCommand(lower, command)) return;

                URL url = new URL(MainActivity.ARCHER_URL + "/voice_command");
                HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                conn.setRequestMethod("POST");
                conn.setRequestProperty("Content-Type", "application/json");

                // Extract archer_auth JWT from the WebView's cookie store and send as
                // Bearer token — bypasses CSRF since native HttpURLConnection has no session
                String jwt = extractArcherJwt();
                if (!jwt.isEmpty()) {
                    conn.setRequestProperty("Authorization", "Bearer " + jwt);
                }

                conn.setDoOutput(true);
                conn.setConnectTimeout(8000);
                conn.setReadTimeout(20000);

                JSONObject body = new JSONObject();
                body.put("command", command);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(body.toString().getBytes("UTF-8"));
                }

                int code = conn.getResponseCode();
                InputStream is = (code >= 200 && code < 300)
                    ? conn.getInputStream() : conn.getErrorStream();

                StringBuilder sb = new StringBuilder();
                try (BufferedReader br = new BufferedReader(new InputStreamReader(is))) {
                    String line;
                    while ((line = br.readLine()) != null) sb.append(line);
                }

                JSONObject resp = new JSONObject(sb.toString());
                String reply = resp.optString("response",
                               resp.optString("text",
                               resp.optString("message", "Done.")));

                runOnUiThread(() -> {
                    responseText.setText(reply);
                    statusText.setText("Tap mic to ask again");
                    speak(reply);
                });

            } catch (Exception e) {
                runOnUiThread(() -> {
                    String msg = "Couldn't reach Archer. Make sure you're connected.";
                    responseText.setText(msg);
                    statusText.setText("Tap mic to try again");
                    speak(msg);
                });
            }
        }).start();
    }

    /** Read the archer_auth JWT from the WebView's shared cookie store. */
    private String extractArcherJwt() {
        try {
            String cookies = CookieManager.getInstance().getCookie(MainActivity.ARCHER_URL);
            if (cookies == null) return "";
            for (String part : cookies.split(";")) {
                String trimmed = part.trim();
                if (trimmed.startsWith("archer_auth=")) {
                    return trimmed.substring("archer_auth=".length());
                }
            }
        } catch (Exception ignored) {}
        return "";
    }

    /** Handle phone-native commands without hitting the server. */
    private boolean handleNativeCommand(String lower, String original) {

        // ── CALL ──────────────────────────────────────────────────────────────
        if (lower.startsWith("call ")) {
            String name = original.substring(5).trim();
            String num  = resolvePhoneNumber(name);
            final String dialTarget = (num != null) ? num : name;
            final String display    = name;
            runOnUiThread(() -> {
                responseText.setText("Calling " + display + "...");
                statusText.setText("Opening phone");
                speak("Calling " + display);
            });
            // Always use ACTION_DIAL so the user confirms before it dials — correct UX in a moving truck.
            // ACTION_CALL would also need a runtime CALL_PHONE permission check.
            Intent call = new Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(dialTarget)));
            call.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(call);
            return true;
        }

        // ── TEXT ──────────────────────────────────────────────────────────────
        if (lower.startsWith("text ") || lower.startsWith("send ")) {
            String raw = lower.startsWith("text ")
                ? original.substring(5).trim()
                : original.replaceFirst("(?i)^send\\s+", "").replaceFirst("(?i)\\s+a\\s+text.*$", "").trim();
            // Strip trailing dictation ("saying hi")
            raw = raw.replaceFirst("(?i)\\s+(saying|that|message|a message).*$", "").trim();
            String num = resolvePhoneNumber(raw);
            final String display = raw;
            if (num != null) {
                final String finalNum = num;
                runOnUiThread(() -> { responseText.setText("Texting " + display + "..."); speak("Texting " + display); });
                Intent sms = new Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + finalNum));
                sms.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(sms);
            } else {
                runOnUiThread(() -> { responseText.setText("Couldn't find " + display + " in contacts"); speak("Couldn't find " + display); });
            }
            return true;
        }

        // ── NAVIGATE ──────────────────────────────────────────────────────────
        if (lower.contains("navigate to ") || lower.contains("directions to ")) {
            String dest = lower.contains("navigate to ")
                ? original.substring(lower.indexOf("navigate to ") + 12)
                : original.substring(lower.indexOf("directions to ") + 14);
            final String d = dest.trim();
            runOnUiThread(() -> { responseText.setText("Navigating to " + d); speak("Navigating to " + d); });
            Intent nav = new Intent(Intent.ACTION_VIEW, Uri.parse("google.navigation:q=" + Uri.encode(d)));
            nav.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try { startActivity(nav); } catch (Exception e) {
                Intent maps = new Intent(Intent.ACTION_VIEW, Uri.parse("https://maps.google.com/?q=" + Uri.encode(d)));
                maps.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(maps);
            }
            return true;
        }

        // ── TIMER / ALARM ─────────────────────────────────────────────────────
        if (lower.contains("timer") || (lower.contains("alarm") && !lower.contains("set alarm for"))) {
            int minutes = 0;
            for (String word : lower.split("\\s+")) {
                try { minutes = Integer.parseInt(word); break; } catch (NumberFormatException ignored) {}
            }
            final int min = minutes;
            runOnUiThread(() -> {
                String msg = min > 0 ? "Timer set for " + min + " minutes" : "Opening timers";
                responseText.setText(msg);
                speak(min > 0 ? "Setting timer for " + min + " minutes" : "Opening timers");
            });
            Intent timer = new Intent(AlarmClock.ACTION_SET_TIMER);
            if (min > 0) {
                timer.putExtra(AlarmClock.EXTRA_LENGTH, min * 60);
                timer.putExtra(AlarmClock.EXTRA_SKIP_UI, true);
            }
            timer.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try { startActivity(timer); } catch (Exception ignored) {}
            return true;
        }

        // ── FLASHLIGHT ────────────────────────────────────────────────────────
        if (lower.contains("flashlight") || lower.contains("torch")) {
            boolean on = !lower.contains("off");
            try {
                CameraManager cm = (CameraManager) getSystemService(CAMERA_SERVICE);
                String camId = cm.getCameraIdList()[0];
                cm.setTorchMode(camId, on);
                final boolean finalOn = on;
                runOnUiThread(() -> {
                    String s = "Flashlight " + (finalOn ? "on" : "off");
                    responseText.setText(s);
                    speak(s);
                });
            } catch (CameraAccessException | ArrayIndexOutOfBoundsException e) {
                runOnUiThread(() -> responseText.setText("Flashlight unavailable"));
            }
            return true;
        }

        // ── VOLUME ────────────────────────────────────────────────────────────
        if (lower.contains("volume")) {
            AudioManager am = (AudioManager) getSystemService(AUDIO_SERVICE);
            int adjust;
            String label;
            if (lower.contains("up") || lower.contains("louder") || lower.contains("raise")) {
                adjust = AudioManager.ADJUST_RAISE; label = "Volume up";
            } else if (lower.contains("down") || lower.contains("lower") || lower.contains("quieter")) {
                adjust = AudioManager.ADJUST_LOWER; label = "Volume down";
            } else if (lower.contains("mute")) {
                adjust = AudioManager.ADJUST_MUTE; label = "Muted";
            } else {
                return false;
            }
            am.adjustStreamVolume(AudioManager.STREAM_MUSIC, adjust, AudioManager.FLAG_SHOW_UI);
            final String finalLabel = label;
            runOnUiThread(() -> { responseText.setText(finalLabel); speak(finalLabel); });
            return true;
        }

        // ── OPEN APP ──────────────────────────────────────────────────────────
        if (lower.startsWith("open ") || lower.startsWith("launch ")) {
            String appName = lower.startsWith("open ") ? lower.substring(5).trim() : lower.substring(7).trim();
            Intent found = findApp(appName);
            if (found != null) {
                final String name = appName;
                runOnUiThread(() -> { responseText.setText("Opening " + name); speak("Opening " + name); });
                startActivity(found);
            } else {
                final String name = appName;
                runOnUiThread(() -> { responseText.setText("App not found: " + name); speak("Couldn't find " + name); });
            }
            return true;
        }

        return false;
    }

    /** Look up a contact's phone number by display name (partial, case-insensitive). */
    private String resolvePhoneNumber(String name) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M &&
                checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED) {
            return null;
        }
        try {
            ContentResolver cr = getContentResolver();
            String[] proj = {ContactsContract.CommonDataKinds.Phone.NUMBER};
            String sel  = "LOWER(" + ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + ") LIKE ?";
            String[] args = {"%" + name.toLowerCase(Locale.US) + "%"};
            String sort = ContactsContract.CommonDataKinds.Phone.IS_SUPER_PRIMARY + " DESC";
            try (Cursor cur = cr.query(
                    ContactsContract.CommonDataKinds.Phone.CONTENT_URI, proj, sel, args, sort)) {
                if (cur != null && cur.moveToFirst()) {
                    return cur.getString(0);
                }
            }
        } catch (Exception ignored) {}
        return null;
    }

    /** Fuzzy-match an app name against installed launcher apps. */
    private Intent findApp(String appName) {
        PackageManager pm = getPackageManager();
        Intent query = new Intent(Intent.ACTION_MAIN, null);
        query.addCategory(Intent.CATEGORY_LAUNCHER);
        for (android.content.pm.ResolveInfo ri : pm.queryIntentActivities(query, 0)) {
            String label = ri.loadLabel(pm).toString().toLowerCase(Locale.US);
            if (label.contains(appName)) {
                Intent launch = pm.getLaunchIntentForPackage(ri.activityInfo.packageName);
                if (launch != null) {
                    launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    return launch;
                }
            }
        }
        return null;
    }

    @Override
    public void onInit(int status) {
        if (status == TextToSpeech.SUCCESS) {
            tts.setLanguage(Locale.US);
            // Prefer a male English voice; engine naming varies by device/OEM
            try {
                Set<Voice> voices = tts.getVoices();
                if (voices != null) {
                    Voice pick = null;
                    for (Voice v : voices) {
                        if (!v.getLocale().getLanguage().equals("en")) continue;
                        String n = v.getName().toLowerCase(Locale.US);
                        // Common patterns: Google TTS male IDs, AOSP names, Piper "ryan"
                        if (n.contains("en-us-x-tpm") || n.contains("en-us-x-iom")
                                || n.contains("en-us-x-sfg") || n.contains("male")
                                || n.contains("ryan") || n.contains("en_us_male")) {
                            pick = v;
                            break;
                        }
                    }
                    if (pick != null) tts.setVoice(pick);
                }
            } catch (Exception ignored) {}
            ttsReady = true;
        }
    }

    private void speak(String text) {
        if (ttsReady && tts != null)
            tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, "archer");
    }

    @Override
    protected void onDestroy() {
        if (recognizer != null) { recognizer.destroy(); recognizer = null; }
        if (tts != null) { tts.stop(); tts.shutdown(); tts = null; }
        super.onDestroy();
    }

    @Override
    public void onBackPressed() { finish(); }
}
