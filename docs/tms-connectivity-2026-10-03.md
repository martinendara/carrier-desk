========================================================================
TMS live connectivity diagnostic (read-only)
========================================================================
started  : 2026-10-03T09:20:48.976+00:00
host     : tramway.proxy.rlwy.net:17159
token    : <redacted>  sha256[:12]=57c520936a65  23 chars, 23 bytes, ascii
deadline : connect 5s, read 5s

── 1. AUTH probe (DEBUG_ECHO)
   at       : 2026-10-03T09:20:48.976+00:00
   resolved : 66.33.22.254
   request  : 'CMD:DEBUG_ECHO|AUTH:<redacted>|MSG:diag'
   connect  : ok in 165 ms
   response : 1 line(s) in 171 ms
     +    171 ms  'ERR|CODE:AUTH_FAILED|MSG:Invalid or missing auth token'
   terminator: seen (we closed the socket, not the server)
   status   : ERR:AUTH_FAILED

── 2. same probe with NO AUTH field
   at       : 2026-10-03T09:20:49.318+00:00
   resolved : 66.33.22.254
   request  : 'CMD:DEBUG_ECHO|MSG:diag'
   connect  : ok in 163 ms
   response : 1 line(s) in 163 ms
     +    163 ms  'ERR|CODE:AUTH_FAILED|MSG:Invalid or missing auth token'
   terminator: seen (we closed the socket, not the server)
   status   : ERR:AUTH_FAILED

── 3. unknown command with the real token
   at       : 2026-10-03T09:20:49.648+00:00
   resolved : 66.33.22.254
   request  : 'CMD:NOT_A_REAL_COMMAND|AUTH:<redacted>|MSG:diag'
   connect  : ok in 161 ms
   response : 1 line(s) in 173 ms
     +    173 ms  'ERR|CODE:AUTH_FAILED|MSG:Invalid or missing auth token'
   terminator: seen (we closed the socket, not the server)
   status   : ERR:AUTH_FAILED

── 4. control (deliberately wrong token)
   at       : 2026-10-03T09:20:49.985+00:00
   resolved : 66.33.22.254
   request  : 'CMD:DEBUG_ECHO|AUTH:<redacted>|MSG:diag'
   connect  : ok in 165 ms
   response : 1 line(s) in 166 ms
     +    166 ms  'ERR|CODE:AUTH_FAILED|MSG:Invalid or missing auth token'
   terminator: seen (we closed the socket, not the server)
   status   : ERR:AUTH_FAILED

── 5. framing variant: LF terminator, no CR
   at       : 2026-10-03T09:20:50.320+00:00
   resolved : 66.33.22.254
   request  : 'CMD:DEBUG_ECHO|AUTH:<redacted>'
   connect  : ok in 187 ms
   response : (no bytes) after 5001 ms
   terminator: none before the 5s deadline
   status   : READ_TIMEOUT (connected, no bytes)

── 6. framing variant: AUTH before CMD
   at       : 2026-10-03T09:20:55.512+00:00
   resolved : 66.33.22.254
   request  : 'AUTH:<redacted>|CMD:DEBUG_ECHO'
   connect  : ok in 237 ms
   response : 1 line(s) in 159 ms
     +    159 ms  'ERR|CODE:MALFORMED|MSG:Malformed frame'
   terminator: seen (we closed the socket, not the server)
   status   : ERR:MALFORMED

========================================================================
VERDICT  : ERR:AUTH_FAILED
           The socket connected and the server answered in 165 ms. The transport, the host and the port are fine.
           no AUTH field   -> ERR:AUTH_FAILED
           bogus command   -> ERR:AUTH_FAILED
           wrong token     -> ERR:AUTH_FAILED
           LF terminator, no CR-> READ_TIMEOUT (connected, no bytes)
           AUTH before CMD -> ERR:MALFORMED


           The server answers ERR|CODE:MALFORMED to a frame with
           AUTH before CMD (AUTH before CMD), so it does parse the
           frame and enforce the field order. Our framing — CRLF,
           CMD first — is the shape it wants, and an LF-only frame
           gets no answer at all, which confirms the terminator too.

           So the frame is well formed and the server still refuses
           the AUTH value. A frame with NO AUTH at all is refused
           with the identical sentence, and so is a deliberately
           wrong one: the check happens before CMD is validated and
           reports 'invalid or missing' as one case.

           VERDICT: TOKEN_REJECTED, framing correct. A stale or
           rotated credential is the likely cause; a server that
           accepts no token at all would look the same from here,
           and both are fixed the same way — re-issue the token.
