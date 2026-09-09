' main.brs — channel entry point

sub Main()
    screen = CreateObject("roSGScreen")
    port   = CreateObject("roMessagePort")
    screen.setMessagePort(port)

    scene = screen.CreateScene("MainScene")
    if scene = invalid
        screen.show()
        return
    end if

    ' Read config — edit config/server.txt and config/token.txt before
    ' sideloading. GetArcherBaseUrl() (source/Config.brs) falls back to the
    ' HF Space URL if server.txt is ever missing/empty rather than leaving
    ' scene.serverUrl blank.
    url   = GetArcherBaseUrl()
    token = ReadAsciiFile("pkg:/config/token.txt").Trim()

    scene.serverUrl = url
    scene.authToken = token

    screen.show()

    while true
        msg = wait(0, port)
        if type(msg) = "roSGScreenEvent"
            if msg.isScreenClosed() then return
        end if
    end while
end sub
