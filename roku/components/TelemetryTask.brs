' TelemetryTask.brs — fetches /roku/telemetry?id=N once

sub init()
    m.top.functionName = "runTelemetryTask"
end sub

sub runTelemetryTask()
    http = CreateObject("roUrlTransfer")
    http.SetCertificatesFile("common:/certs/ca-bundle.crt")
    ' Peer verification left ON (Roku default) — see StatusTask.brs for why.
    http.SetUrl(m.top.serverUrl + "/roku/telemetry?id=" + stri(m.top.tripId).Trim())
    if m.top.authToken <> ""
        http.AddHeader("Authorization", "Bearer " + m.top.authToken)
    end if

    attempts = 0
    while attempts < 3
        raw = http.GetToString()
        if raw <> ""
            data = ParseJson(raw)
            if data <> invalid and data.DoesExist("date")
                m.top.tripData = data
                return
            end if
        end if
        attempts = attempts + 1
        if attempts < 3 then sleep(1500)
    end while

    m.top.error = "Could not load trip data"
end sub
