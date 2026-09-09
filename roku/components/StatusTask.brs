' StatusTask.brs — polls /roku/status every 30 seconds

sub init()
    m.top.functionName = "runStatusTask"
end sub

sub runStatusTask()
    http = CreateObject("roUrlTransfer")
    http.SetCertificatesFile("common:/certs/ca-bundle.crt")
    ' Peer verification left ON (Roku default) — server.txt defaults to plain
    ' http:// so this has no effect on the documented setup, but if the owner
    ' points it at an https:// server (e.g. via Tailscale/Caddy), the bearer
    ' token below should never go out over an unverified TLS connection.
    if m.top.authToken <> ""
        http.AddHeader("Authorization", "Bearer " + m.top.authToken)
    end if

    while true
        http.SetUrl(m.top.serverUrl + "/roku/status")
        raw = http.GetToString()
        if raw <> ""
            data = ParseJson(raw)
            if data <> invalid and data.DoesExist("status")
                m.top.status  = data.status
                m.top.message = data.message
                alert = data.alert
                if alert = invalid then alert = ""
                m.top.alert = alert
            else
                m.top.status  = "error"
                m.top.message = "Bad response from server"
                m.top.alert   = ""
            end if
        else
            m.top.status  = "offline"
            m.top.message = "No response from server"
            m.top.alert   = ""
        end if
        sleep(30000)
    end while
end sub
