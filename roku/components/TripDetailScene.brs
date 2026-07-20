' TripDetailScene.brs

sub init()
    m.col1    = m.top.findNode("col1")
    m.col2    = m.top.findNode("col2")
    m.extLabel = m.top.findNode("extLabel")

    m.top.observeField("tripData", "onTripData")
end sub

sub onTripData()
    d = m.top.tripData
    if d = invalid then return

    ' Header
    m.top.findNode("headerDate").text = d.date + "  " + d.time
    road = d.road
    if road = invalid or road = "" then road = "Unknown road"
    m.top.findNode("headerRoad").text = road

    ' Build two stat columns
    dist   = d.distance_mi
    dur    = d.duration_min
    rpm    = d.peak_rpm
    boost  = d.peak_boost
    oil    = d.peak_oil_temp
    cool   = d.peak_coolant
    mpg    = d.mpg
    fuel   = d.fuel_gal
    hard   = d.hard_events
    qual   = d.quality
    b060   = d.best_060

    if dist = invalid   then dist  = 0
    if dur = invalid    then dur   = 0
    if rpm = invalid    then rpm   = 0
    if boost = invalid  then boost = 0
    if oil = invalid    then oil   = 0
    if cool = invalid   then cool  = 0
    if mpg = invalid    then mpg   = 0
    if fuel = invalid   then fuel  = 0
    if hard = invalid   then hard  = 0
    if qual = invalid   then qual  = ""

    col1Lines = []
    col1Lines.Push("Distance")
    col1Lines.Push(formatFloat(dist, 1) + " mi")
    col1Lines.Push("")
    col1Lines.Push("Duration")
    col1Lines.Push(formatFloat(dur, 0) + " min")
    col1Lines.Push("")
    col1Lines.Push("Fuel used")
    col1Lines.Push(formatFloat(fuel, 3) + " gal")
    col1Lines.Push("")
    col1Lines.Push("Avg MPG")
    col1Lines.Push(formatFloat(mpg, 1))

    col2Lines = []
    col2Lines.Push("Peak RPM")
    col2Lines.Push(formatInt(rpm))
    col2Lines.Push("")
    col2Lines.Push("Peak boost")
    col2Lines.Push(formatFloat(boost, 1) + " psi")
    col2Lines.Push("")
    col2Lines.Push("Peak oil")
    col2Lines.Push(formatFloat(oil, 0) + "°F")
    col2Lines.Push("")
    col2Lines.Push("Peak coolant")
    col2Lines.Push(formatFloat(cool, 0) + "°F")

    m.col1.text = joinLines(col1Lines)
    m.col2.text = joinLines(col2Lines)

    extParts = []
    if qual <> "" then extParts.Push("Quality: " + qual)
    if hard > 0   then extParts.Push("Hard events: " + stri(hard).Trim())
    if b060 <> invalid and b060 > 0
        extParts.Push("0-60: " + formatFloat(b060, 2) + " s")
    end if
    m.extLabel.text = extParts.Join("   ·   ")

    ' Fetch extended data (fault codes, weather, ethanol) in background
    if d.DoesExist("id")
        m.task = m.top.CreateChild("TelemetryTask")
        if m.task <> invalid
            m.task.serverUrl = m.top.serverUrl
            m.task.authToken = m.top.authToken
            m.task.tripId    = d.id
            m.task.observeField("tripData", "onTelemetryLoaded")
            m.task.control = "RUN"
        end if
    end if
end sub

sub onTelemetryLoaded()
    td = m.task.tripData
    if td = invalid then return

    extParts = []
    q = td.quality
    if q <> invalid and q <> "" then extParts.Push("Quality: " + q)
    h = td.hard_events
    if h <> invalid and h > 0 then extParts.Push("Hard events: " + stri(h).Trim())
    b060 = td.best_060
    if b060 <> invalid and b060 > 0 then extParts.Push("0-60: " + formatFloat(b060, 2) + " s")
    eth = td.ethanol_pct
    if eth <> invalid and eth > 0 then extParts.Push("E" + stri(eth).Trim())
    wx = td.weather
    if wx <> invalid and wx <> "" then extParts.Push(wx)

    codes = td.fault_codes
    if codes <> invalid and codes.count() > 0
        extParts.Push("DTCs: " + codes.Join(", "))
    end if

    m.extLabel.text = extParts.Join("   ·   ")
end sub

function onKeyEvent(key as String, press as Boolean) as Boolean
    if press and key = "back"
        m.top.closeRequested = true
        return true
    end if
    return false
end function


' ── Formatting helpers ────────────────────────────────────────────────────────

function formatFloat(v as Float, decimals as Integer) as String
    if decimals = 0 then return stri(int(v)).Trim()
    factor = 10 ^ decimals
    rounded = int(v * factor + 0.5) / factor
    whole = int(rounded)
    frac  = int((rounded - whole) * factor + 0.5)
    pad   = stri(frac).Trim()
    while pad.Len() < decimals
        pad = "0" + pad
    end while
    return stri(whole).Trim() + "." + pad
end function

function formatInt(v as Integer) as String
    s = stri(v).Trim()
    if v >= 1000
        s = left(s, s.Len() - 3) + "," + right(s, 3)
    end if
    return s
end function

function joinLines(lines as Object) as String
    result = ""
    for each line in lines
        if result <> "" then result = result + chr(10)
        result = result + line
    end for
    return result
end function
