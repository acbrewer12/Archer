package com.archer.truck;

import android.app.Activity;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.WindowManager;
import android.widget.TextView;
import android.widget.Toast;
import com.google.android.gms.auth.api.signin.GoogleSignIn;
import com.google.android.gms.auth.api.signin.GoogleSignInAccount;
import com.google.android.gms.auth.api.signin.GoogleSignInClient;
import com.google.android.gms.auth.api.signin.GoogleSignInOptions;
import com.google.android.gms.common.SignInButton;
import com.google.android.gms.common.api.ApiException;
import com.google.android.gms.tasks.Task;
import com.google.firebase.auth.AuthCredential;
import com.google.firebase.auth.FirebaseAuth;
import com.google.firebase.auth.FirebaseUser;
import com.google.firebase.auth.GoogleAuthProvider;
import com.google.firebase.messaging.FirebaseMessaging;

public class LoginActivity extends Activity {

    private static final int RC_SIGN_IN = 100;

    private FirebaseAuth auth;
    private GoogleSignInClient googleSignInClient;
    private TextView statusText;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Full screen, same as the rest of the app
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().getDecorView().setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            | View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
        );

        auth = FirebaseAuth.getInstance();

        // Already signed in — go straight to dashboard
        if (auth.getCurrentUser() != null) {
            goToMain();
            return;
        }

        setContentView(R.layout.activity_login);
        statusText = findViewById(R.id.login_status);

        GoogleSignInOptions gso = new GoogleSignInOptions.Builder(GoogleSignInOptions.DEFAULT_SIGN_IN)
            .requestIdToken(getString(R.string.google_web_client_id))
            .requestEmail()
            .build();
        googleSignInClient = GoogleSignIn.getClient(this, gso);

        SignInButton signInBtn = findViewById(R.id.sign_in_button);
        signInBtn.setSize(SignInButton.SIZE_WIDE);
        signInBtn.setOnClickListener(v -> startSignIn());
    }

    private void startSignIn() {
        if (statusText != null) statusText.setText("Signing in...");
        startActivityForResult(googleSignInClient.getSignInIntent(), RC_SIGN_IN);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != RC_SIGN_IN) return;

        Task<GoogleSignInAccount> task = GoogleSignIn.getSignedInAccountFromIntent(data);
        try {
            GoogleSignInAccount account = task.getResult(ApiException.class);
            firebaseAuthWithGoogle(account.getIdToken());
        } catch (ApiException e) {
            String msg = "Sign in failed (code " + e.getStatusCode() + ")";
            if (statusText != null) statusText.setText(msg);
            Toast.makeText(this, msg, Toast.LENGTH_LONG).show();
        }
    }

    private void firebaseAuthWithGoogle(String idToken) {
        AuthCredential credential = GoogleAuthProvider.getCredential(idToken, null);
        auth.signInWithCredential(credential).addOnCompleteListener(this, task -> {
            if (task.isSuccessful()) {
                // Subscribe to Archer FCM topic for broadcast alerts (e.g. truck-wide alerts)
                FirebaseMessaging.getInstance().subscribeToTopic("archer-alerts");
                goToMain();
            } else {
                if (statusText != null) statusText.setText("Authentication failed. Try again.");
                Toast.makeText(this, "Authentication failed", Toast.LENGTH_SHORT).show();
            }
        });
    }

    private void goToMain() {
        startActivity(new Intent(this, MainActivity.class));
        finish();
    }
}
