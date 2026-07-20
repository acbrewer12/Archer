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

    ' Read config — edit these files before sideloading
    url   = ReadAsciiFile("pkg:/config/server.txt").Trim()
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
