package com.ayden.archer

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.material3.*
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                ArcherWearApp()
            }
        }
    }
}

private enum class Screen { STATUS, CONFIRM_START, STARTING, START_RESULT }

@Composable
fun ArcherWearApp() {
    var screen by remember { mutableStateOf(Screen.STATUS) }
    var statusResult by remember { mutableStateOf<ArcherApiClient.StatusResult?>(null) }
    var isLoadingStatus by remember { mutableStateOf(true) }
    var startSucceeded by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    fun refreshStatus() {
        isLoadingStatus = true
        scope.launch {
            statusResult = ArcherApiClient.fetchStatus()
            isLoadingStatus = false
        }
    }

    LaunchedEffect(Unit) { refreshStatus() }

    when (screen) {
        Screen.STATUS -> StatusScreen(
            isLoading = isLoadingStatus,
            result = statusResult,
            onRemoteStartTapped = { screen = Screen.CONFIRM_START },
            onRefresh = { refreshStatus() }
        )

        Screen.CONFIRM_START -> ConfirmStartScreen(
            onConfirm = {
                screen = Screen.STARTING
                scope.launch {
                    val result = ArcherApiClient.triggerRemoteStart()
                    startSucceeded = result.success
                    screen = Screen.START_RESULT
                }
            },
            onCancel = { screen = Screen.STATUS }
        )

        Screen.STARTING -> StartingScreen()

        Screen.START_RESULT -> StartResultScreen(
            succeeded = startSucceeded,
            onDone = { screen = Screen.STATUS }
        )
    }
}

@Composable
fun StatusScreen(
    isLoading: Boolean,
    result: ArcherApiClient.StatusResult?,
    onRemoteStartTapped: () -> Unit,
    onRefresh: () -> Unit
) {
    ScalingLazyColumn(
        modifier = Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        item {
            Text(
                text = "Archer",
                style = MaterialTheme.typography.titleMedium
            )
        }

        item {
            when {
                isLoading -> Text("Checking status...")
                result == null || !result.success -> Text(
                    text = "Can't reach Archer",
                    textAlign = TextAlign.Center
                )
                else -> Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(
                        text = result.status,
                        color = if (result.alert)
                            androidx.compose.ui.graphics.Color(0xFFE8A33D)
                        else
                            androidx.compose.ui.graphics.Color(0xFF3A7BD5)
                    )
                    if (result.message.isNotEmpty()) {
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            text = result.message,
                            style = MaterialTheme.typography.bodySmall,
                            textAlign = TextAlign.Center
                        )
                    }
                    // The actual alert text (e.g. "High coolant temperature"),
                    // not just the color-coded boolean - this was the whole
                    // point of the alertText field fix. Shown prominently,
                    // separate from the general status message above.
                    if (result.alert && !result.alertText.isNullOrEmpty()) {
                        Spacer(modifier = Modifier.height(6.dp))
                        Text(
                            text = "⚠ " + result.alertText,
                            color = androidx.compose.ui.graphics.Color(0xFFE8A33D),
                            style = MaterialTheme.typography.bodySmall,
                            textAlign = TextAlign.Center
                        )
                    }
                }
            }
        }

        item {
            Button(onClick = onRemoteStartTapped) {
                Text("Remote Start")
            }
        }

        item {
            TextButton(onClick = onRefresh) {
                Text("Refresh", style = MaterialTheme.typography.labelSmall)
            }
        }
    }
}

@Composable
fun ConfirmStartScreen(onConfirm: () -> Unit, onCancel: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().padding(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        Text(
            text = "Start the truck?",
            style = MaterialTheme.typography.titleSmall,
            textAlign = TextAlign.Center
        )
        Spacer(modifier = Modifier.height(12.dp))
        Row {
            Button(onClick = onCancel) { Text("Cancel") }
            Spacer(modifier = Modifier.width(8.dp))
            Button(onClick = onConfirm) { Text("Start") }
        }
    }
}

@Composable
fun StartingScreen() {
    Column(
        modifier = Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        CircularProgressIndicator()
        Spacer(modifier = Modifier.height(8.dp))
        Text("Starting...")
    }
}

@Composable
fun StartResultScreen(succeeded: Boolean, onDone: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().padding(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        Text(
            text = if (succeeded) "Started" else "Couldn't start truck",
            textAlign = TextAlign.Center
        )
        Spacer(modifier = Modifier.height(12.dp))
        Button(onClick = onDone) { Text("OK") }
    }
}
