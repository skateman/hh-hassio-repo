# Home Assistant Add-on: audio-powersave

This add-on is a backgrond daemon that [subscribes to PulseAudio events](https://freedesktop.org/software/pulseaudio/doxygen/subscribe.html) and keeps a count of the sink inputs. Initially the number is zero and upon an increase it calls a user-specified API endpoint with a user-specified payload. If the number gets down to zero, it sets up a user-specified timeout and calls another user-specified API endpoint. With the right configuration it's possible turn off an amplifier if it's not used and turn it back on when necessary.

## Installation
The installation of this add-on is pretty straightforward and not different in comparison to installing any other Home Assistant add-on.

1. Enable the Home Hub repository in the Supervisor add-on store.
2. Search for the "audio-powersave" add-on in the Supervisor add-on store and install it.
3. Select your audio output device and hit Save on that as well.
4. Start the "audio-powersave" add-on.
5. Check the logs of the "audio-powersave" to see if everything went well.
6. Ready to go!

## Configuration

**Note**: _Remember to restart the add-on when the configuration is changed._

Example add-on configuration:

```yaml
log_level: info
timeout: 30
on_path: services/switch/turn_on
off_path: services/switch/turn_off
payload:
  entity_id: switch.audio
```

**Note**: _This is just an example, don't copy and paste it! Create your own!_

### Option: `log_level`

The `log_level` option controls the level of log output by the addon and can
be changed to be more or less verbose, which might be useful when you are
dealing with an unknown issue. Possible values are:

- `debug`: Shows detailed debug information.
- `info`: Normal (usually) interesting events.
- `warning`: Exceptional occurrences that are not errors.
- `error`:  Runtime errors that do not require immediate action.

Please note that each level automatically includes log messages from a
more severe level, e.g., `debug` also shows `info` messages. By default,
the `log_level` is set to `info`, which is the recommended setting unless
you are troubleshooting.

### Option: `timeout`

The `timeout` option sets the number of seconds to wait before firing an HTTP
request to the `off_path` if no audio is playing.

### Option: `on_path`

The `on_path` will be used to build the URL of the HTTP request that gets called
when the audio starts playing. It can be any path in the Home Assistant REST API
as it will be substituted into the `http://HASS_URL/api/<on_path>` schema.

### Option: `off_path`

The `off_path` will be used to build the URL of the HTTP request that gets called
when no audio is playing for `timeout` seconds. It can be any path in the Home
Assistant REST API as it will be substituted into the `http://HASS_URL/api/<on_path>`
schema.

### Option: `payload`

The payload for both kind of HTTP requests. Theoretically, it can be any nested
JSON-compatible value transposed into YAML.

## License

MIT License

Copyright (c) 2020 Dávid Halász

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
