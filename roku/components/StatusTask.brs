' StatusTask.brs — polls /roku/status every 30 seconds
'
' Sets m.top.status / .message / .alert which MainScene observes.

sub runStatusTask()
    http = CreateObject("roUrlTransfer")
    http.SetCertificatesFile("common:/certs/ca-bundle.crt")
    http.EnablePeerVerification(false)  ' self-signed cert on the Pi is fine
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
                m.top.message = "Could not parse server response"
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
