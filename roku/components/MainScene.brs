' MainScene.brs
'
' State machine:
'   "status"   — status monitor is showing, waiting for OK
'   "menu"     — main menu is focused
'   "triplist" — TripListScene child is active
'   "tripdetail" — TripDetailScene child is active (stacked over trip list)

sub init()
    m.state = "status"
    m.tripListScene   = invalid
    m.tripDetailScene = invalid

    ' UI refs
    m.statusGroup  = m.top.findNode("statusGroup")
    m.menuGroup    = m.top.findNode("menuGroup")
    m.statusDot    = m.top.findNode("statusDot")
    m.statusLabel  = m.top.findNode("statusLabel")
    m.messageLabel = m.top.findNode("messageLabel")
    m.alertLabel   = m.top.findNode("alertLabel")
    m.mainMenu     = m.top.findNode("mainMenu")

    ' Populate main menu
    menuContent = CreateObject("roSGNode", "ContentNode")
    for each item in ["Trip History", "System Status"]
        child = CreateObject("roSGNode", "ContentNode")
        child.title = item
        menuContent.AppendChild(child)
    end for
    m.mainMenu.content = menuContent
    m.mainMenu.observeField("itemSelected", "onMenuSelected")

    ' Start status polling task
    m.statusTask = m.top.CreateChild("StatusTask")
    m.statusTask.serverUrl = m.top.serverUrl
    m.statusTask.authToken = m.top.authToken
    m.statusTask.observeField("status",  "onStatusUpdate")
    m.statusTask.observeField("message", "onStatusUpdate")
    m.statusTask.observeField("alert",   "onStatusUpdate")
    m.statusTask.control = "RUN"

    m.statusGroup.setFocus(true)
end sub


' ── Status task callbacks ─────────────────────────────────────────────────────

sub onStatusUpdate()
    status  = m.statusTask.status
    message = m.statusTask.message
    alert   = m.statusTask.alert

    if status = "online"
        m.statusDot.color   = "#00cc44"
        m.statusLabel.color = "#00cc44"
        m.statusLabel.text  = "Online"
    else if status = "demo"
        m.statusDot.color   = "#ffaa00"
        m.statusLabel.color = "#ffaa00"
        m.statusLabel.text  = "Demo mode"
    else if status = "error"
        m.statusDot.color   = "#ff4040"
        m.statusLabel.color = "#ff4040"
        m.statusLabel.text  = "Error"
    else
        m.statusDot.color   = "#404040"
        m.statusLabel.color = "#7a7a7a"
        m.statusLabel.text  = "Offline"
    end if

    m.messageLabel.text = message

    if alert <> "" and alert <> invalid
        m.alertLabel.text    = "⚠  " + alert
        m.alertLabel.visible = true
    else
        m.alertLabel.visible = false
    end if
end sub


' ── Key handling ──────────────────────────────────────────────────────────────

function onKeyEvent(key as String, press as Boolean) as Boolean
    if not press then return false

    if m.state = "status"
        if key = "OK" or key = "play"
            showMenu()
            return true
        end if

    else if m.state = "menu"
        if key = "back"
            ' Return to status view
            showStatus()
            return true
        end if
    end if

    return false
end function


' ── Navigation helpers ────────────────────────────────────────────────────────

sub showMenu()
    m.state = "menu"
    m.statusGroup.visible = false
    m.menuGroup.visible   = true
    m.mainMenu.setFocus(true)
end sub

sub showStatus()
    m.state = "status"
    m.menuGroup.visible   = false
    m.statusGroup.visible = true
    m.statusGroup.setFocus(true)
end sub


' ── Main menu selection ───────────────────────────────────────────────────────

sub onMenuSelected()
    idx = m.mainMenu.itemSelected
    if idx = 0
        openTripList()
    else if idx = 1
        showStatus()
    end if
end sub


' ── Trip List ─────────────────────────────────────────────────────────────────

sub openTripList()
    m.state = "triplist"
    m.menuGroup.visible = false

    m.tripListScene = m.top.CreateChild("TripListScene")
    m.tripListScene.serverUrl  = m.top.serverUrl
    m.tripListScene.authToken  = m.top.authToken
    m.tripListScene.observeField("closeRequested", "onTripListClose")
    m.tripListScene.observeField("tripSelected",   "onTripSelected")
    m.tripListScene.setFocus(true)
end sub

sub onTripListClose()
    if m.tripListScene <> invalid
        m.top.RemoveChild(m.tripListScene)
        m.tripListScene = invalid
    end if
    m.state = "menu"
    m.menuGroup.visible = true
    m.mainMenu.setFocus(true)
end sub

sub onTripSelected()
    if m.tripListScene = invalid then return
    tripData = m.tripListScene.selectedTrip
    if tripData = invalid then return
    openTripDetail(tripData)
end sub


' ── Trip Detail ───────────────────────────────────────────────────────────────

sub openTripDetail(tripData as Object)
    m.state = "tripdetail"

    m.tripDetailScene = m.top.CreateChild("TripDetailScene")
    m.tripDetailScene.serverUrl  = m.top.serverUrl
    m.tripDetailScene.authToken  = m.top.authToken
    m.tripDetailScene.tripData   = tripData
    m.tripDetailScene.observeField("closeRequested", "onTripDetailClose")
    m.tripDetailScene.setFocus(true)
end sub

sub onTripDetailClose()
    if m.tripDetailScene <> invalid
        m.top.RemoveChild(m.tripDetailScene)
        m.tripDetailScene = invalid
    end if
    m.state = "triplist"
    if m.tripListScene <> invalid
        m.tripListScene.setFocus(true)
    end if
end sub
