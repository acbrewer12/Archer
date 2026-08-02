' Centralized configuration - reads the backend URL from config/server.txt
' at runtime rather than returning a hardcoded string. Reconciled to match
' the roku/config/server.txt pattern already in the real repo (per Claude
' Code) - a plain text file is simpler to edit/script than BrightScript
' source, even though (like the source-based version before it) it still
' requires a rebuild to take effect, since Roku apps can't be edited
' post-install. The real win is editing simplicity, not runtime-without-rebuild.
function GetArcherBaseUrl() as String
    url = ReadAsciiFile("pkg:/config/server.txt")

    if url = invalid or url = ""
        ' Fallback if the file is missing or empty, so the app doesn't
        ' crash outright - still worth fixing the real cause if this ever
        ' actually triggers
        return "https://aydencatman-archer.hf.space"
    end if

    ' Strip trailing newline/whitespace that a plain text file commonly has
    return url.Trim()
end function
