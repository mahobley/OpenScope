# GLS10 emulator communications reference

This is the operational communication reference for the runnable emulator in
[`backend/head_unit_emulator.py`](../backend/head_unit_emulator.py).  It records the
known-good captured baseline, the current generated/minimized behavior, and the
evidence for what is required.  It is intentionally more practical than the
research record: use it when integrating, troubleshooting, or recreating the
network side of the tool.

> **Scope and confidence.** “Required” means required by repeatable tests against
> the GLS10 and firmware used for this project; it is not a Garmin protocol
> specification.  “Optional” means it was changed or omitted in successful
> cold-start and streaming trials.  Values labelled **captured** are observed
> bytes/values whose meaning is not necessarily known.  Do not treat a captured
> value as an immutable product identity.

The full historical evidence, literal payload catalogue, and test notes remain in
the historical protocol research retained in the original development branch.

## At a glance

The emulator acts as the **TCP client** and simulated Garmin head unit.  The GLS10
is the peer and sends sonar data.  The direct wired interface must be configured
before startup; packet capture and neighbour configuration normally require
administrator privileges.

| Item | Current/project value | Requirement |
| --- | --- | --- |
| GLS10 IPv4 | `172.16.3.0` | Required by the tested implementation; retain until dynamic peer discovery is qualified. |
| Head-unit IPv4 | Default minimized run: `172.16.6.1/16`; captured baseline: `172.16.6.0/16` | Either `.0` or `.1` passed. Keep the chosen address consistent everywhere. |
| Head-unit MAC | Physical MAC of the selected Ethernet adapter | Required structurally. The emulator does **not** spoof the captured head MAC. |
| GLS10 MAC | Captured `04:25:e8:68:ef:58`; minimized/default run learns it from peer traffic | A neighbour mapping is needed. Passive learning passed; static captured mapping is the fallback. |
| Sonar receive | UDP multicast `239.254.2.16:50223` | Required to receive images; ignore packets not sourced by the GLS IP. |
| Essential retention | UDP `224.0.0.1:50030`, TCP `50900`, TCP `50224` with 1-second active state, discovered data-sink activation, TCP `50227` for controls | Required in tested lifecycle. |

The selected interface must not own `172.16.3.0`.  It must own the selected head
address with a `/16` mask.  TCP clients bind to the head address with an ephemeral
source port; multicast receivers bind wildcard address plus the selected head
interface.

## Required lifecycle

The processes run concurrently, but the dependencies below matter.  Delays shown
as **captured** can be reduced to zero in qualified profiles unless a more
conservative reproduction is wanted.

```text
Start UDP presence, UDP 50051 discovery, mDNS/DNS-SD browse/register,
50223 multicast receiver, and TCP workers
        |
GLS seen (UDP 50051, DNS-SD sink advert, or 50223 traffic)
  + head identity ready
        |
        +-- captured +550 ms --> TCP 50900 initial exchange; keep socket open
                                      |
                                  captured +620 ms
                                      |
                                 TCP 50224 exchange -> read session base
                                      |
                         send active state now and every 1 second
                                      |
DNS-SD yields data-sink UDP port ---+-> initial sink records / maintenance
                                      |
                               deliver active type-05 sink request
                                      |
                          captured +195 ms --> TCP 50227 exchange
                                      |
                         range / enable / disable controls are now usable
```

The captured conservative schedule waits 5.5 seconds of sink maintenance before
the active request and broadcasts a 50010 stream transition 5.73 seconds later.
Both are optional in qualified minimized profiles.  TCP connection and initial
receive timeout is 3 seconds; an I/O failure retries the affected TCP channel after
2 seconds.  A zero-length TCP read is a closed session.

## Required transports and messages

All byte strings below are hexadecimal octets, sent as binary.  Explicit numeric
fields and the apparent TCP header length field are little-endian.  TCP is a byte
stream: use `sendall`, accumulate reads, and parse the apparent 8-byte
`message-id + payload-length` framing rather than assuming one `recv` is one record.

| Channel | Direction and endpoint | What must happen | Captured timing / response |
| --- | --- | --- | --- |
| UDP 50030 | Head `:50030` -> `224.0.0.1:50030` | Send the 122-byte `HEAD_50030` presence repeatedly. One startup packet failed; periodic presence is necessary. | Baseline: 10 sends at 1-second intervals, then every 11.75 s. Qualified: immediately then every 30 s also passed. |
| UDP 50051 | Head `:50051` <-> GLS `172.16.3.0:50051` | Send discovery commands until 50224 is ready; reply to GLS identity requests and send the one-time finalizer for `9703`. | Probe set every 2 s. Head identity follows sink maintenance by captured 300 ms. |
| mDNS/DNS-SD | Head registers identity and browses multicast DNS `224.0.0.251:5353` | Browse `_garmin-ds-sink._udp.local.` and use the GLS-advertised sink port. Registration is part of the conservative path. | mDNS TTL is 255. Current default consolidated run omits the head registration only because that omission was qualified. |
| TCP 50900 | Head -> GLS `:50900` | Send 3 initialization records, receive initialization response, then retain and read the socket. Closing it signals head-session loss. | Captured wait 550 ms after gates; 171 bytes observed. |
| TCP 50224 | Head -> GLS `:50224` | Send 4 initialization records, derive live session base from response bytes 12–15, send active state and repeat every second. Send range/state writes here. | Captured wait 620 ms after 50900; 1608 bytes observed. The active repetition is required. |
| Data sink UDP | Head -> DNS-SD-advertised GLS port | Send the bootstrap or periodic maintenance records and deliver an active type-05 request. | Baseline source `51401`, every 100 ms. Qualified alternatives are below. |
| TCP 50227 | Head -> GLS `:50227` | Send 3 initialization records before sonar enable/disable commands. | Captured +195 ms after sink active; 313 bytes observed. Heartbeat is optional. |
| UDP 50223 | GLS `172.16.3.0` -> `239.254.2.16:50223` | Join this multicast group on the head interface and receive/reassemble sonar data. | Continuous; sampled JPEGs were 512×511 px. |

The 171, 1608, and 313-byte initialization totals are observations, not proven
protocol constants.  At minimum, validate that the 50224 response has 16 bytes
before reading its session base.

### UDP 50051 discovery/identity

Until TCP 50224 is ready, send this four-command sequence every two seconds to the
GLS; every command is LE32 command ID plus four zero bytes:

```text
91 03 00 00 00 00 00 00
98 03 00 00 00 00 00 00
96 03 00 00 00 00 00 00
91 03 00 00 00 00 00 00
```

When a GLS datagram begins `91 03`, return the 124-, 179-, and 12-byte head
identity records.  For `96 03`, return the 12-byte record.  On the first `97 03`,
send `9a03000004000000009b760d` once.  The baseline sends
124/179/12/124/179 about 300 ms after the first sink cycle.  A single 124/179/12
sequence with zero delay also passed.

### TCP initialization payloads

Keep the headers, `efbe` marker, and declared lengths.  The opaque payload tokens
are not semantically decoded.  Replacing the two-byte `3f08` and eight-byte `a90a`
payloads with zeroes passed on each port in tests, but removal is not assumed safe
outside the qualified consolidated profile.

| TCP port | Send in order |
| --- | --- |
| 50900 | `3f08efbe02000000d4c6`<br>`a90aefbe0800000090910075c5292f85`<br>`c508efbe00000000` |
| 50224 | `3f08efbe020000005bdf`<br>`a90aefbe08000000ecf7601ca9c046e8`<br>`0409efbe0400000000000400`<br>`0409efbe180000001400010030594137d0107f097a6a4137265943373a5ca446` (inactive state, sent before response) |
| 50227 | `3f08efbe020000002dc4`<br>`a90aefbe08000000e47c30483a89abb6`<br>`d307efbe00000000` |

After 50224 initialization, send this active state immediately and every second:

```text
0409efbe180000001400010030594137d0107f097a6a41372a5943373a5ca446
```

The captured 50227 identity heartbeat is below; it is useful for baseline captures
but was not necessary over the tested roughly two-minute lifecycle:

```text
d107efbe0e0000000401040daaeffa910d12d4222101
```

### Data-sink request/maintenance

DNS-SD determines the destination port.  Send these six records in the captured
order for the initial bootstrap:

```text
c505000003000000010100
c505000009000000020701058ce794a803  # one-time initial counter 0xe78c
c505000003000000040100
c505000003000000050100               # inactive type-05
c505000003000000060100
c5050000030000000b0100
```

For the captured recurring cycle, replace the second record with
`c50500000900000002070105` + LE16 counter + `94a803`; begin at `0xe78d`, retain
each counter for 10 cycles, then increment modulo 65536.  After 5.5 seconds,
replace inactive type-05 with the active request:

```text
c50500000e000000050c011709ff9f80d0fa96aa8e01
```

An active type-05 delivery path is required.  Tests prove either an initial
six-record burst **or** immediate periodic maintenance can work; do not remove both.

## Dynamic control messages

All control writes use the existing live TCP sockets.  Do not open competing control
connections.

### Session base and range selection (TCP 50224)

Read `B = LE32(response[12:16])` from the 50224 initialization response.  Captured
range templates are linked to static base `S = 0x42465312`; relocate each session
relative word using `R = (B - S) mod 2^32`.

Range packet layout:

```text
0409efbe1400000010000200 [word@12] [word@16] [selector@20] [word@24]
```

Start from the selected template in [`backend/live_viewer/control.py`](../backend/live_viewer/control.py):
the initial words are `14534642`, `47534642`, selector, `16534642` (each LE32).
Relocate offsets 12, 16, and 24 by normal 32-bit addition.  At offset 20, `min` uses
that same rule; every numeric range uses separate bank-byte and low-24-bit
relocation.  This exception is empirical and should not be “simplified.”

Supported captured selectors are: `min`, 7, 9, 10, 15, 20, 25, 30, 35, 40, 45, 50,
55, 60, 65, 70, 75, 80, 90, 100, 110, 120, 135, 150, 175, and 200 ft.  The UI may
show 300 ft, but no 300-ft protocol selector has been captured.

### Sonar enable/disable

First send this generated 50224 state packet, where all four words are LE32:

```text
0409efbe1400000010000200
  B, B+12, B+(0 enable / 1 disable), B+1
```

Then send the command on TCP 50227:

```text
enable:  d207efbe0a00000001070701020100a90100
disable: d207efbe0a00000001070701020100a90101
```

The conservative order is state then command; enable waits for a 50224 reply or
50 ms, while disable does not wait.  Reverse order and a 500 ms gap also passed in
paired tests.  State-only and command-only variants have not been qualified.

## Captured values versus generated format

Generate values once per process and retain them for that run; do not generate a
new identity per packet.  Repair enclosing record length fields whenever a
variable-length identity field changes.

| Field | Original capture | Generated/current format | Status |
| --- | --- | --- | --- |
| Head address | `172.16.6.0/16` | Current minimized/default run: fixed `172.16.6.1/16` | Both passed; not random. |
| mDNS name | `garmin-j6-mudskipper-3527325610.local.` | `device-j6-<10 lowercase letters>-<10 digits>.local.` | Portable profile; instance/server numeric suffixes are independent. |
| mDNS service | `_garmin-mrn-snr._tcp.local.` | `_device-mrn-snr._tcp.local.` | Tested fixed replacement. |
| Advertised mDNS port | `50221` | `50222` | Tested unused port; no listener was required on it. |
| UDP 50051 model | `ECHOMAP Ultra2 106sv` | 1–40 uppercase ASCII letters/digits | Replace both model occurrences and repair record lengths. |
| Software version | `Software Version 41.05` | Same-length `Software Version DD.DD` | Digits are randomized. |
| Ilitek version | `firmware version is 7.0.0.3.0.0.0.0` | Randomize first decimal digit; keep suffix | Same-length substitution. |
| Map-family text | `VERBMAP BUS001L` | 7 uppercase letters + ` BUS001L` | Same-length substitution. |
| VERSMAP text | `VERSMAP None` | `VERSMAP ` + 4 uppercase letters | Same-length substitution. |
| GLS MAC | `04:25:e8:68:ef:58` | Learn source MAC from a peer IPv4 frame and install scoped neighbour entry | Captured static mapping remains fallback. |
| Head MAC | `04:23:22:9c:0a:78` | Physical adapter MAC | Do not spoof; this passed. |
| Multicast TTL | `1` | `1` in standard code; portable testing also passed `1`, `2`, `255` selected once/launch | This excludes mDNS TTL 255. |
| Sink source port | `51401` | OS-assigned ephemeral port is acceptable | Captured baseline uses `51401`. |
| Sink counter | initial `0xe78d` (`0xe78c` one-time) | Qualified alternative starts `0xe78e`; frozen value also passed | 16-bit LE; counter is not a proven identity. |

The normal no-profile command currently enables the qualified consolidated/minimized
behavior.  It uses head IP `.1`, learns the GLS MAC, sends UDP 50030 immediately
then every 30 seconds, retains 50224 active keepalives, and omits the mDNS identity
advertisement plus qualified nonessential traffic.  Explicit portable profiles use
the generated identity formats above.  Use the captured baseline only when
reproducing a historical packet capture or isolating a new compatibility issue.

## Optional traffic retained for reference

These were present in a healthy capture but may be omitted without preventing cold
discovery, TCP establishment, range writes, or image streaming in the tested
lifecycle.  They can still be useful when comparing with a hardware trace.

| Transport | Destination | Captured cadence / key value | Notes |
| --- | --- | --- | --- |
| Raw Ethernet `0x2003` | Ethernet broadcast | every 5 s; payload `040000054f400600` + 38 zero bytes | Optional. The accompanying ARP probe is optional after neighbour is known. |
| UDP 50220 | `239.254.2.1:50220` | every 1 s before sink active, then 100 ms loop | `HEAD_50220` announcement. |
| UDP 50050 | `239.254.2.2:50050` | startup every 1 s; after active, status/capability every 5 s, capability offset by 2.85 s | Startup sequence begins `0x03`; active sequence `0xc6`. |
| UDP 51000 | `239.254.2.11:51000` | every 1 s | Head presence. |
| UDP 51400 | `239.254.2.18:51400` | every 2 s | System service record. |
| UDP 51950 | `239.254.2.22:51950` | short every 2 s from counter `0x08ae`; long every 2.5 s from `0x06d7`, first long at +1 s | LE16 counters increment per send. |
| UDP 50615 | `239.254.2.14:50615` | three-record burst at +5 s, then every 10 s | Optional service burst. |
| UDP 8322 | `239.254.2.14:8322` | with each 50615 burst | Large fixed capabilities payload; optional in minimized profiles. |
| UDP 50010 | broadcast `172.16.255.255:50010` | once, captured +5.73 s after sink active | Payload `7408000000000000`; optional transition. |
| TCP 50227 heartbeat | existing TCP 50227 socket | every 1 s | Payload shown above; optional over tested duration. |

The exact literal definitions for all optional records are deliberately kept as
named constants in [`backend/head_unit_emulator.py`](../backend/head_unit_emulator.py)
(`HEAD_…`) so packet comparisons can use the source of truth without copying a
large, error-prone hexadecimal appendix into a second file.

## Operational checklist and limits

- Use a single wired, sonar-facing adapter; never configure the adapter with the
  GLS IP.
- Start UDP/mDNS receivers and adverts before waiting on TCP.  TCP 50900 must stay
  open for the session.
- Keep the active TCP 50224 record on its one-second cadence.  This is the main
  liveness requirement: stopping it led to stream failure at about 22,000
  image-sized packets in two tests.
- Keep an active data-sink type-05 path.  Initial-versus-periodic maintenance is a
  choice; removing both is untested and unsafe.
- Confirm enable/disable by observing UDP 50223 traffic, not merely a successful
  socket write.  Disable was unreliable in historical trials.
- Treat identity strings, opaque TCP tokens, response lengths, and timing as
  implementation evidence rather than protocol semantics.  Change one variable per
  hardware test and retain a known-good capture.

For a new hardware/firmware combination, record firmware revision, cold/warm boot,
exact packet diff, time to first image, packet rate, and whether streaming survives
at least five minutes.
