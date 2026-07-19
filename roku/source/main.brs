' main.brs — channel entry point
'
' Creates the single roSGScreen, passes server config to MainScene,
' and runs the event loop.  All navigation is handled inside MainScene.

sub Main()
    screen = CreateObject("roSGScreen")
    m.port = CreateObject("roMessagePort")
    screen.setMessagePort(m.port)

    scene = screen.CreateScene("MainScene")

    ' Edit pkg:/config/server.txt  → Archer server URL  (http://IP:5000)
    ' Edit pkg:/config/token.txt   → Tier 3 archer_auth JWT
    url   = ReadAsciiFile("pkg:/config/server.txt").Trim()
    token = ReadAsciiFile("pkg:/config/token.txt").Trim()
    if url = "" or url = "http://192.168.1.100:5000"
        ' Default — overwrite server.txt with your Pi's IP before sideloading
    end if
    scene.serverUrl  = url
    scene.authToken  = token

    screen.show()

    while true
        msg = wait(0, m.port)
        if type(msg) = "roSGScreenEvent"
            if msg.isScreenClosed() then return
        end if
    end while
end sub
