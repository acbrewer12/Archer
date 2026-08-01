package com.archer.truck;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.View;
import android.view.WindowManager;
import android.webkit.*;

public class MainActivity extends Activity {

    static final String ARCHER_URL = "ARCHER_URL_PLACEHOLDER";
    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        // Hide system bars — works on all API 26+ without crashing
        getWindow().getDecorView().setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            | View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
        );

        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.webview);
        setupWebView();

        boolean isAssist = Intent.ACTION_ASSIST.equals(getIntent().getAction())
            || Intent.ACTION_VOICE_COMMAND.equals(getIntent().getAction());

        webView.loadUrl(ARCHER_URL + (isAssist ? "/?assist=1" : "/"));
    }

    private void setupWebView() {
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setGeolocationEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        // COMPATIBILITY_MODE blocks active mixed content (scripts, iframes) served over
        // plain HTTP inside an HTTPS page while still allowing legacy passive content
        // (images). ALWAYS_ALLOW had no real justification here and let an on-path
        // attacker inject executable HTTP content into an otherwise-HTTPS session.
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setUserAgentString(s.getUserAgentString() + " ArcherAndroid/2.0");

        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler,
                    android.net.http.SslError error) {
                // Fail closed on every certificate error. This used to compare only the
                // erroring URL's hostname string against ARCHER_URL's hostname and then
                // call handler.proceed() unconditionally — meaning ANY cert (expired,
                // wrong CA, or actively attacker-supplied) was accepted as long as the
                // hostname matched, which defeats TLS validation entirely for anyone who
                // can intercept traffic to that host (e.g. ARP spoofing on the truck's
                // own hotspot).
                //
                // The production ARCHER_URL is https://aydencatman-archer.hf.space (see
                // gradle.properties / build-apk.yml), which carries a real CA-signed cert,
                // so this handler should not fire in normal use. Local-Pi builds can
                // optionally serve HTTPS with a self-signed cert (archer.py
                // _get_tls_context(), gated by USE_TLS) that is generated fresh per
                // device — there is no stable certificate or public key checked into this
                // repo to pin against, so real certificate/public-key pinning isn't
                // implementable here yet. If self-signed local-Pi HTTPS needs to keep
                // working, install that Pi's cert as a user-trusted CA on the device, or
                // add proper pinning once there's a stable cert to pin (e.g. provisioned
                // via a QR/NFC pairing flow, matching the note in helpers.js about
                // ARCHER-2500HD auto-connect trust).
                handler.cancel();
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                // Only grant permissions to the Pi server origin
                android.net.Uri piUri = android.net.Uri.parse(ARCHER_URL);
                String piOrigin = piUri.getScheme() + "://" + piUri.getHost()
                    + (piUri.getPort() != -1 ? ":" + piUri.getPort() : "");
                if (request.getOrigin().toString().startsWith(piOrigin)) {
                    request.grant(request.getResources());
                } else {
                    request.deny();
                }
            }
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin,
                    GeolocationPermissions.Callback callback) {
                callback.invoke(origin, true, false);
            }
        });
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();
        webView.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        webView.onResume();
    }

    @Override
    protected void onDestroy() {
        webView.destroy();
        super.onDestroy();
    }
}
