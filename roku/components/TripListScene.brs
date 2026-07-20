' TripListScene.brs

sub init()
    m.tripList    = m.top.findNode("tripList")
    m.statusLabel = m.top.findNode("statusLabel")
    m.drives      = []

    m.tripList.observeField("itemSelected", "onItemSelected")

    m.task = m.top.CreateChild("TripListTask")
    if m.task = invalid then return

    m.task.serverUrl = m.top.serverUrl
    m.task.authToken = m.top.authToken
    m.task.observeField("drives", "onDrivesLoaded")
    m.task.observeField("error",  "onLoadError")
    m.task.control = "RUN"
end sub

sub onDrivesLoaded()
    result = m.task.drives
    if result = invalid or not result.DoesExist("list")
        m.statusLabel.text    = "No trips recorded yet."
        m.statusLabel.visible = true
        return
    end if
    m.drives = result.list
    if m.drives.count() = 0
        m.statusLabel.text    = "No trips recorded yet."
        m.statusLabel.visible = true
        return
    end if

    content = CreateObject("roSGNode", "ContentNode")
    for each drive in m.drives
        child = CreateObject("roSGNode", "ContentNode")
        child.title = drive.label
        content.AppendChild(child)
    end for
    m.tripList.content  = content
    m.statusLabel.visible = false
    m.tripList.visible    = true
    m.tripList.setFocus(true)
end sub

sub onLoadError()
    m.statusLabel.text = "Error: " + m.task.error
end sub

sub onItemSelected()
    idx = m.tripList.itemSelected
    if idx >= 0 and idx < m.drives.count()
        m.top.selectedTrip = m.drives[idx]
        m.top.tripSelected = true
    end if
end sub

function onKeyEvent(key as String, press as Boolean) as Boolean
    if press and key = "back"
        m.top.closeRequested = true
        return true
    end if
    return false
end function
