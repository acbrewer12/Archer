package com.ayden.archer

import androidx.wear.protolayout.ResourceBuilders
import androidx.wear.protolayout.TimelineBuilders
import androidx.wear.protolayout.LayoutElementBuilders
import androidx.wear.protolayout.ModifiersBuilders
import androidx.wear.protolayout.ActionBuilders
import androidx.wear.protolayout.ColorBuilders
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import androidx.wear.tiles.TileService
import com.google.common.util.concurrent.ListenableFuture
import com.google.common.util.concurrent.Futures
import kotlinx.coroutines.*

class ArcherTileService : TileService() {

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    override fun onTileRequest(
        requestParams: RequestBuilders.TileRequest
    ): ListenableFuture<TileBuilders.Tile> {
        val future = androidx.concurrent.futures.CallbackToFutureAdapter.getFuture<TileBuilders.Tile> { completer ->
            serviceScope.launch {
                val status = try {
                    withTimeout(8000) { ArcherApiClient.fetchStatus() }
                } catch (e: TimeoutCancellationException) {
                    ArcherApiClient.StatusResult(success = false, errorMessage = "Timed out")
                }
                completer.set(buildTile(status))
            }
            "archer_tile_request"
        }
        return future
    }

    override fun onTileResourcesRequest(
        requestParams: RequestBuilders.ResourcesRequest
    ): ListenableFuture<ResourceBuilders.Resources> {
        return Futures.immediateFuture(
            ResourceBuilders.Resources.Builder().setVersion("1").build()
        )
    }

    private fun buildTile(status: ArcherApiClient.StatusResult): TileBuilders.Tile {
        val statusText = if (status.success) status.status else "OFFLINE"
        val color = if (!status.success) 0xFF5A5A5A.toInt()
            else if (status.alert) 0xFFE8A33D.toInt()
            else 0xFF3A7BD5.toInt()

        // Short alert line for the tile - full text lives in the main app,
        // this is just enough to know something needs attention at a glance
        val alertLine = if (status.alert && !status.alertText.isNullOrEmpty()) {
            status.alertText
        } else null

        val launchAppAction = ModifiersBuilders.Clickable.Builder()
            .setOnClick(
                ActionBuilders.LaunchAction.Builder()
                    .setAndroidActivity(
                        ActionBuilders.AndroidActivity.Builder()
                            .setPackageName(packageName)
                            .setClassName("com.ayden.archer.MainActivity")
                            .build()
                    ).build()
            )
            .setId("launch_app")
            .build()

        val column = LayoutElementBuilders.Column.Builder()
            .addContent(LayoutElementBuilders.Text.Builder().setText("Archer").build())
            .addContent(
                LayoutElementBuilders.Text.Builder()
                    .setText(statusText)
                    .setFontStyle(
                        LayoutElementBuilders.FontStyle.Builder()
                            .setColor(ColorBuilders.argb(color))
                            .build()
                    )
                    .build()
            )

        if (alertLine != null) {
            column.addContent(
                LayoutElementBuilders.Text.Builder()
                    .setText("⚠ $alertLine")
                    .setFontStyle(
                        LayoutElementBuilders.FontStyle.Builder()
                            .setColor(ColorBuilders.argb(0xFFE8A33D.toInt()))
                            .setSize(LayoutElementBuilders.SpProp.Builder().setValue(12f).build())
                            .build()
                    )
                    .build()
            )
        }

        val layout = LayoutElementBuilders.Box.Builder()
            .setModifiers(ModifiersBuilders.Modifiers.Builder().setClickable(launchAppAction).build())
            .addContent(column.build())
            .build()

        val timeline = TimelineBuilders.Timeline.Builder()
            .addTimelineEntry(
                TimelineBuilders.TimelineEntry.Builder()
                    .setLayout(LayoutElementBuilders.Layout.Builder().setRoot(layout).build())
                    .build()
            )
            .build()

        return TileBuilders.Tile.Builder()
            .setResourcesVersion("1")
            .setFreshnessIntervalMillis(5 * 60 * 1000)
            .setTileTimeline(timeline)
            .build()
    }

    override fun onDestroy() {
        super.onDestroy()
        serviceScope.cancel()
    }
}
