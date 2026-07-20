' TripListTask.brs — fetches /roku/drives once, with up to 3 retries

sub init()
    m.top.functionName = "runTripListTask"
end sub

sub runTripListTask()
    http = CreateObject("roUrlTransfer")
    http.SetCertificatesFile("common:/certs/ca-bundle.crt")
    http.EnablePeerVerification(false)
    http.SetUrl(m.top.serverUrl + "/roku/drives")
    if m.top.authToken <> ""
        http.AddHeader("Authorization", "Bearer " + m.top.authToken)
    end if

    attempts = 0
    while attempts < 3
        raw = http.GetToString()
        if raw <> ""
            data = ParseJson(raw)
            if data <> invalid and type(data) = "roArray"
                m.top.drives = {list: data}
                return
            end if
        end if
        attempts = attempts + 1
        if attempts < 3 then sleep(2000)
    end while

    m.top.error = "Could not load trip history after 3 attempts"
end sub
