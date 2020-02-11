# Ariston NET remotethermo integration for Home Assistant
Thin integration is a side project and was tested only with 1 zone climate. It logs in Ariston website and fetches/sets data on that site. Due to interaction with boiler it is time consuming process and thus intergation is relatively slow.
You are free to modify and distribute it, but it is distributed as is with no liability (see license file).

## Integration installation
In `/config` folder create `custom_components` folder in load source file in it
In `configuration.yaml` include:
```
ariston:
  username: !secret ariston_username
  password: !secret ariston_password
```
With additional attributes if needed, which are described below.

## Attributes
**username** - user name used in https://www.ariston-net.remotethermo.com/

**password** - password used in https://www.ariston-net.remotethermo.com/
*It is recommended for security purposes to not use your common password just in case.*

**hvac_off** - indicates how to treat `HVAC OFF` action in climate. Options are `off` and `summer`. By default it is `summer`, which means that turning off would keep DHW water heating on (e.g. summer mode). Presets in climate allow switching between `off`, `summer` and `winter`.

**power_on** - indicates which mode would be used for `switch.turn_on` action. Options are `summer` and `winter`. By default it is `summer`.

**max_retries** - number of retries to set the data in boiler. Retries are made in case of communication issues for example, which take place occasionally. By default the value is '1'.

**switches** - lists switches to be defined
  - `power` - turn power off and on (on value is defined by **power_on**)

**sensors** - lists sensors to be defined
  - `mode` - mode of boiler (`off` or `summer` or `winter`)
  - `ch_antifreeze_temperature` - CH antifreeze temperature
  - `ch_mode` - mode of CH (`manual` or `scheduled`)
  - `ch_set_temperature` - set CH temperature
  - `dhw_set_temperature` - set DHW temperature
  - `detected_temperature` - temperature measured by thermostat

**binary_sensors**
  - `online` - online status
  - `holiday_mode` - if holiday mode switch on via application or site
  - `flame` - if boiler is heating water (DHW or CH)
