# Corsair Commander Core, Core XT and ST
_Driver API and source code available in [`liquidctl.driver.commander_core`](../liquidctl/driver/commander_core.py)._

_Changed in 1.11.0: the Corsair Commander Core XT is now supported._<br>
_Changed in 1.12.0: the Corsair Commander ST is now supported._<br>

Currently, functionality implemented is listed here. More is planned to be added.

## Initializing the device

The device should be initialized every time it is powered on.

```
# liquidctl initialize
Corsair Commander Core
├── Firmware version            2.6.201
├── AIO LED count                    29
├── RGB port 1 LED count              8
├── RGB port 2 LED count              8
├── RGB port 3 LED count            N/A
├── RGB port 4 LED count            N/A
├── RGB port 5 LED count            N/A
├── RGB port 6 LED count            N/A
├── AIO port connected              Yes
├── Fan port 1 connected            Yes
├── Fan port 2 connected            Yes
├── Fan port 3 connected             No
├── Fan port 4 connected             No
├── Fan port 5 connected             No
├── Fan port 6 connected             No
├── Water temperature sensor        Yes
└── Temperature sensor 1             No
```

## Retrieving the pump speed, fan speeds, and temperatures

The Commander Core and ST currently can retrieve the pump speed, fan speeds, temperature of the water, and
the temperature measured by the probe.

```
# liquidctl status
Corsair Commander Core
├── Pump speed           2356  rpm
├── Fan speed 1           810  rpm
├── Fan speed 2           791  rpm
├── Fan speed 3             0  rpm
├── Fan speed 4             0  rpm
├── Fan speed 5             0  rpm
├── Fan speed 6             0  rpm
└── Water temperature    35.8  °C
```

The Core XT variant of the device is not meant for use with an AIO, so parameters relating to the pump are
not present.

```
Corsair Commander Core XT
├── Fan speed 1    2737  rpm
├── Fan speed 2    2786  rpm
├── Fan speed 3       0  rpm
├── Fan speed 4       0  rpm
├── Fan speed 5       0  rpm
└── Fan speed 6       0  rpm
```


## Lighting

_New in 1.17.0._<br>

The pump/AIO ring and the fan LED channels can be set to static colors.

```
# pump ring
liquidctl set pump color fixed ff0000

# fan port 1
liquidctl set fan1 color fixed 00ff00

# turn the pump ring off
liquidctl set pump color off
```

The valid channels are `pump` and `fan1` to `fan6`.  Static colors support the
`off`, `fixed` and `super-fixed` modes:

- `off` turns the channel off.
- `fixed` applies one color to every LED in the channel.
- `super-fixed` applies one color per LED; missing colors turn those LEDs off.

The device expects a single frame containing every LED of every channel, so
liquidctl remembers the colors it has written per channel in its runtime
storage and merges them into later updates.  Channels that liquidctl has not
configured yet are written as black, which means the first lighting command
after a reboot also turns the other channels off.  Animated effects are not
supported: liquidctl is not a daemon, so it cannot stream frames continuously.

## Programming the pump and fan speeds

### Speed curve profiles

_New in 1.14.0._<br>

The pump or fans speeds can be configured using a speed curve profile with a minimum of 2 or up to 7 curve points.

Each curve point consists of both a temperature (in celsius) and a duty (percentage).


```
liquidctl set fans speed 28 0  35 50  40 75  41 85  42 90  43 95  44 100
              |          |  |
           channel       |  duty
                      temp
```


### Fixed duty cycle

_New in 1.9.0._<br>

The pump or fan speeds can be set to a fixed duty cycle.

```
# liquidctl set fan1 speed 70
                ^^^^       ^^
               channel    duty
```


In iCUE the pump can be set to different modes that correspond to a fixed percent that can be used in liquidctl.
Quiet is 75%, Balanced is 85% and Extreme is 100%.

### Device channels

Valid channel values on the Core (non-XT) and ST are `pump`, `fanN`, where 1 <= N <= 6 is the fan number.
On the Core XT, the `pump` channel is not present. The `fans` channel can be used to simultaneously
configure all fans.

### Notes

- A channel may only be configured with a single fixed duty cycle or a single fan curve profile
  (independently from the other channel configurations).
- The pump and some fans have a limit to how slow they can go and will not stop when set to zero.
  This is a hardware limitation that cannot be changed.
- The cooler's lights flash with every update. Due to limitations in both the device hardware and
  liquidctl, there currently is no way to solve this problem. For more information, see: [#448].

[#448]: https://github.com/liquidctl/liquidctl/issues/448
