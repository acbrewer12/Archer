package com.archer.truck;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.view.View;
import android.view.animation.AlphaAnimation;
import android.view.animation.Animation;
import android.view.animation.ScaleAnimation;
import android.view.animation.AnimationSet;
import android.widget.TextView;
import org.json.JSONObject;
import java.io.*;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.Locale;

public class VoiceActivity extends Activity implements TextToSpeech.OnInitListener {

    private SpeechRecognizer recognizer;
    private TextToSpeech tts;
    private boolean ttsReady = false;
    private boolean listening = false;

    private TextView statusText, transcriptText, responseText;
    private View ring1, ring2, micBtn;

    private static final int MIC_PERMISSION = 101;

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
            // Auto-start listening
            new Handler(Looper.getMainLooper()).postDelayed(this::startListening, 400);
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (code == MIC_PERMISSION && results.length > 0
                && results[0] == PackageManager.PERMISSION_GRANTED) {
            initRecognizer();
            new Handler(Looper.getMainLooper()).postDelayed(this::startListening, 400);
        } else {
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

        AnimationSet a1 = pulse(1.0f, 1.6f, 900);
        AnimationSet a2 = pulse(1.0f, 2.2f, 1300);
        ring1.startAnimation(a1);
        ring2.startAnimation(a2);
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
                // Check for phone-native commands first
                String lower = command.toLowerCase(Locale.US);
                if (handleNativeCommand(lower, command)) return;

                // Send to Archer AI
                URL url = new URL(MainActivity.ARCHER_URL + "/voice_command");
                HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                conn.setRequestMethod("POST");
                conn.setRequestProperty("Content-Type", "application/json");
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

    // Handle a handful of phone-native actions without hitting the server
    private boolean handleNativeCommand(String lower, String original) {
        if (lower.startsWith("call ")) {
            String name = original.substring(5).trim();
            runOnUiThread(() -> {
                responseText.setText("Calling " + name + "...");
                statusText.setText("Opening phone");
                speak("Calling " + name);
            });
            Intent call = new Intent(Intent.ACTION_DIAL,
                Uri.parse("tel:" + Uri.encode(name)));
            call.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(call);
            return true;
        }
        if (lower.contains("navigate to ") || lower.contains("directions to ")) {
            String dest = lower.contains("navigate to ")
                ? original.substring(lower.indexOf("navigate to ") + 12)
                : original.substring(lower.indexOf("directions to ") + 14);
            runOnUiThread(() -> {
                responseText.setText("Navigating to " + dest.trim());
                speak("Navigating to " + dest.trim());
            });
            Intent nav = new Intent(Intent.ACTION_VIEW,
                Uri.parse("google.navigation:q=" + Uri.encode(dest.trim())));
            nav.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try { startActivity(nav); } catch (Exception e) {
                Intent maps = new Intent(Intent.ACTION_VIEW,
                    Uri.parse("https://maps.google.com/?q=" + Uri.encode(dest.trim())));
                maps.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(maps);
            }
            return true;
        }
        return false;
    }

    @Override
    public void onInit(int status) {
        if (status == TextToSpeech.SUCCESS) {
            tts.setLanguage(Locale.US);
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
